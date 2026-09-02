package com.liferadio.sync.service

import android.os.Build
import androidx.room.withTransaction
import com.liferadio.sync.data.local.AppDatabase
import com.liferadio.sync.data.local.EventDeliveryEntity
import com.liferadio.sync.data.local.SettingsStore
import com.liferadio.sync.data.model.CentralDevice
import com.liferadio.sync.data.model.CentralEventBatch
import com.liferadio.sync.data.remote.CentralBatchItem
import com.liferadio.sync.data.remote.CentralEventMapper
import com.liferadio.sync.data.remote.CentralConfirmationSelector
import com.liferadio.sync.data.remote.CentralRetryPolicy
import com.liferadio.sync.data.remote.CentralSyncClient
import com.liferadio.sync.data.remote.CentralUploadResult
import com.liferadio.sync.data.remote.CentralUploader
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

sealed interface CentralSyncOutcome {
    data object Disabled : CentralSyncOutcome
    data object MissingToken : CentralSyncOutcome
    data class Deferred(val retryAt: Long) : CentralSyncOutcome
    data object Idle : CentralSyncOutcome
    data class Completed(
        val confirmedCount: Int,
        val rejectedCount: Int,
        val unconfirmedCount: Int
    ) : CentralSyncOutcome
    data class AuthenticationRequired(val statusCode: Int) : CentralSyncOutcome
    data class RetryScheduled(val retryAt: Long, val reason: String) : CentralSyncOutcome
    data class Failed(val reason: String) : CentralSyncOutcome
}

sealed interface CentralSyncLoopResult {
    data class InProgress(
        val cumulativeConfirmed: Int,
        val cumulativeRejected: Int,
        val remainingQueue: Int,
        val batchCount: Int
    ) : CentralSyncLoopResult
    data class Completed(
        val totalConfirmed: Int,
        val totalRejected: Int,
        val batchCount: Int
    ) : CentralSyncLoopResult
    data class Stopped(
        val cumulativeConfirmed: Int,
        val remainingQueue: Int,
        val reason: String
    ) : CentralSyncLoopResult
    data class AuthFailed(val statusCode: Int) : CentralSyncLoopResult
    data class Failed(val reason: String) : CentralSyncLoopResult
}

class CentralSyncCoordinator(
    private val database: AppDatabase,
    private val settings: SettingsStore,
    private val uploader: CentralUploader? = null,
    private val nowProvider: () -> Long = System::currentTimeMillis
) {
    private val mapper = CentralEventMapper(
        Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
    )
    private val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("UTC")
    }

    // ---- continuous sync loop ----

    /**
     * Continuously drain the pending queue, uploading up to MAX_BATCH_SIZE per HTTP request.
     * Calls [onProgress] after every batch with cumulative statistics.
     *
     * Stop conditions:
     *  - queue fully drained (Completed)
     *  - auth failure 401/403 (AuthFailed) — credentials invalid
     *  - permanent upload failure (Failed)
     *  - retryable failure: stops loop, schedules retry, reports Stopped
     *  - reaches [maxBatchesPerRun] safety cap: schedules short-interval follow-up
     *
     * [force] bypasses the retry-backoff gate for the *first* batch only;
     * if a mid-loop retryable failure occurs, the loop still stops.
     */
    suspend fun syncLoop(
        force: Boolean = false,
        maxBatchesPerRun: Int = 20,
        onProgress: (CentralSyncLoopResult) -> Unit = {}
    ): CentralSyncLoopResult = syncMutex.withLock {
        // ---- pre-flight checks ----
        if (settings.syncMode != SettingsStore.SYNC_MODE_CENTRAL) {
            return@withLock CentralSyncLoopResult.Stopped(0, 0, "中央模式未启用")
        }
        if (!settings.isCentralTokenConfigured) {
            settings.centralLastStatus = "中央 Token 未配置"
            return@withLock CentralSyncLoopResult.Stopped(0, 0, "中央 Token 未配置")
        }
        if (settings.centralBaseUrl.isBlank()) {
            settings.centralLastStatus = "中央地址未配置，请重新粘贴邀请码"
            return@withLock CentralSyncLoopResult.Stopped(0, 0, "中央地址未配置")
        }
        if (settings.centralAuthBlocked) {
            return@withLock CentralSyncLoopResult.AuthFailed(401)
        }
        val now = nowProvider()
        if (!force && settings.centralNextRetryAt > now) {
            return@withLock CentralSyncLoopResult.Stopped(
                0, getCentralQueueSize(), "等待退避重试"
            )
        }

        // ---- loop ----
        var cumulativeConfirmed = 0
        var cumulativeRejected = 0
        var batchCount = 0

        while (batchCount < maxBatchesPerRun) {
            val remaining = getCentralQueueSize()
            if (remaining == 0) {
                clearRetryState("已同步全部数据", now)
                return@withLock CentralSyncLoopResult.Completed(
                    totalConfirmed = cumulativeConfirmed,
                    totalRejected = cumulativeRejected,
                    batchCount = batchCount
                )
            }

            val activeUploader = uploader ?: CentralSyncClient(
                baseUrl = settings.centralBaseUrl,
                tokenProvider = settings::getCentralToken
            )

            // read one batch from the delivery-based pending query
            val localEvents = database.dataEventDao().getEventsPendingForTargetLimited(
                CENTRAL_TARGET_ID,
                MAX_BATCH_SIZE
            )
            if (localEvents.isEmpty()) {
                clearRetryState("已同步全部数据", now)
                return@withLock CentralSyncLoopResult.Completed(
                    totalConfirmed = cumulativeConfirmed,
                    totalRejected = cumulativeRejected,
                    batchCount = batchCount
                )
            }

            val items = localEvents.map(mapper::map)
            val batch = CentralEventBatch(
                batchId = UUID.randomUUID().toString(),
                device = CentralDevice(
                    deviceId = settings.deviceId,
                    displayName = Build.MODEL ?: "Life Link Android"
                ),
                sentAt = dateFormat.format(Date(nowProvider())),
                events = items.map(CentralBatchItem::event)
            )

            when (val result = activeUploader.upload(batch)) {
                is CentralUploadResult.Success -> {
                    val acknowledgementResult = applyAcknowledgement(items, result)
                    val confirmed = acknowledgementResult.confirmed
                    val rejected = acknowledgementResult.rejected
                    val unconfirmed = acknowledgementResult.unconfirmed
                    cumulativeConfirmed += confirmed
                    cumulativeRejected += rejected
                    batchCount++

                    val newRemaining = getCentralQueueSize()
                    val progress = CentralSyncLoopResult.InProgress(
                        cumulativeConfirmed = cumulativeConfirmed,
                        cumulativeRejected = cumulativeRejected,
                        remainingQueue = newRemaining,
                        batchCount = batchCount
                    )
                    onProgress(progress)
                    clearRetryState(
                        "已确认 ${cumulativeConfirmed} 条，队列剩余 ${newRemaining} 条", now
                    )

                    // rejection without progress: stop to avoid tight loop
                    if (confirmed == 0 && unconfirmed > 0) {
                        val reason = if (rejected > 0) {
                            val detail = acknowledgementResult.rejectionSummary?.let { "：$it" }.orEmpty()
                            "当前批次全部未确认（服务端拒绝 ${rejected} 条$detail），暂停等待修正"
                        } else {
                            "当前批次全部未确认，暂停以等待服务端状态更新"
                        }
                        return@withLock CentralSyncLoopResult.Stopped(
                            cumulativeConfirmed, newRemaining,
                            reason
                        )
                    }
                    // rejected events: record but continue (not a reason to stop)
                    // empty remaining: will exit at top of next iteration
                }
                is CentralUploadResult.AuthFailure -> {
                    settings.centralAuthBlocked = true
                    settings.centralNextRetryAt = 0L
                    settings.centralLastStatus = "中央认证失败（HTTP ${result.statusCode}），请检查设备 Token"
                    return@withLock CentralSyncLoopResult.AuthFailed(result.statusCode)
                }
                is CentralUploadResult.RetryableFailure -> {
                    val attempt = settings.centralRetryAttempt + 1
                    val delay = result.retryAfterMillis ?: CentralRetryPolicy.backoffMillis(attempt)
                    val retryAt = nowProvider() + delay
                    settings.centralRetryAttempt = attempt
                    settings.centralNextRetryAt = retryAt
                    settings.centralLastStatus = "${result.reason}，已确认 ${cumulativeConfirmed} 条，其余等待重试"
                    return@withLock CentralSyncLoopResult.Stopped(
                        cumulativeConfirmed, getCentralQueueSize(),
                        result.reason
                    )
                }
                is CentralUploadResult.InvalidAcknowledgement -> {
                    val attempt = settings.centralRetryAttempt + 1
                    val retryAt = nowProvider() + CentralRetryPolicy.backoffMillis(attempt)
                    settings.centralRetryAttempt = attempt
                    settings.centralNextRetryAt = retryAt
                    settings.centralLastStatus = "${result.reason}，已确认 ${cumulativeConfirmed} 条，等待重试"
                    return@withLock CentralSyncLoopResult.Stopped(
                        cumulativeConfirmed, getCentralQueueSize(),
                        result.reason
                    )
                }
                is CentralUploadResult.PermanentFailure -> {
                    settings.centralLastStatus = result.reason
                    return@withLock CentralSyncLoopResult.Failed(result.reason)
                }
            }
        }

        // hit maxBatchesPerRun safety cap: schedule short follow-up
        if (getCentralQueueSize() > 0) {
            scheduleShortRetry(nowProvider())
            val remaining = getCentralQueueSize()
            settings.centralLastStatus = "单次运行已达 ${batchCount} 批（${cumulativeConfirmed} 条），剩余 ${remaining} 条，稍后继续"
            return@withLock CentralSyncLoopResult.Stopped(
                cumulativeConfirmed, remaining,
                "安全上限，稍后自动继续"
            )
        }

        // queue drained within the batch limit
        clearRetryState("已同步全部数据", now)
        return@withLock CentralSyncLoopResult.Completed(
            totalConfirmed = cumulativeConfirmed,
            totalRejected = cumulativeRejected,
            batchCount = batchCount
        )
    }

    // ---- helpers ----

    private data class AckResult(
        val confirmed: Int,
        val rejected: Int,
        val unconfirmed: Int,
        val rejectionSummary: String?
    )

    private suspend fun applyAcknowledgement(
        items: List<CentralBatchItem>,
        result: CentralUploadResult.Success
    ): AckResult {
        val acknowledgement = result.acknowledgement
        val confirmedItems = CentralConfirmationSelector.confirmedItems(items, acknowledgement)
        if (confirmedItems.isNotEmpty()) {
            database.withTransaction {
                database.eventDeliveryDao().markDelivered(
                    confirmedItems.map { item ->
                        EventDeliveryEntity(
                            eventId = item.localEventId,
                            targetId = CENTRAL_TARGET_ID,
                            deliveredRevision = item.localRevision
                        )
                    }
                )
                confirmedItems.forEach { item ->
                    database.dataEventDao().markSyncedRevision(
                        item.localEventId,
                        item.localRevision
                    )
                }
            }
        }
        val rejectedCount = acknowledgement.eventResults.count { it.status == "rejected" }
        val unconfirmedCount = items.size - confirmedItems.size
        val rejectionSummary = acknowledgement.eventResults
            .asSequence()
            .filter { it.status == "rejected" }
            .mapNotNull { it.code?.takeIf(String::isNotBlank) }
            .distinct()
            .take(3)
            .joinToString(", ")
            .ifBlank { null }
        return AckResult(confirmedItems.size, rejectedCount, unconfirmedCount, rejectionSummary)
    }

    private fun scheduleShortRetry(now: Long) {
        // schedule a brief retry to continue draining
        val shortDelay = 30_000L // 30 seconds
        settings.centralRetryAttempt = 0
        settings.centralNextRetryAt = now + shortDelay
    }

    private fun clearRetryState(status: String, now: Long) {
        settings.centralRetryAttempt = 0
        settings.centralNextRetryAt = 0L
        settings.centralLastSyncAt = now
        settings.centralLastStatus = status
    }

    private suspend fun getCentralQueueSize(): Int =
        database.dataEventDao().getCentralPendingCountBlocking(CENTRAL_TARGET_ID)

    // ---- legacy single-batch entry (kept for backward compat, delegates to loop) ----

    @Deprecated("Use syncLoop() instead", ReplaceWith("syncLoop(force)"))
    suspend fun syncOnce(force: Boolean = false): CentralSyncOutcome = syncMutex.withLock {
        // bridge to loop result
        val result = syncLoop(force = force, maxBatchesPerRun = 1) { /* no intermediate progress */ }
        when (result) {
            is CentralSyncLoopResult.Stopped -> {
                if (result.reason == "中央模式未启用") CentralSyncOutcome.Disabled
                else if (result.reason == "中央 Token 未配置") {
                    settings.centralLastStatus = "中央 Token 未配置"
                    CentralSyncOutcome.MissingToken
                } else if (result.reason == "中央地址未配置") {
                    settings.centralLastStatus = "中央地址未配置，请重新粘贴邀请码"
                    CentralSyncOutcome.MissingToken
                } else CentralSyncOutcome.RetryScheduled(
                    settings.centralNextRetryAt, result.reason
                )
            }
            is CentralSyncLoopResult.Completed -> CentralSyncOutcome.Completed(
                confirmedCount = result.totalConfirmed,
                rejectedCount = result.totalRejected,
                unconfirmedCount = 0
            )
            is CentralSyncLoopResult.AuthFailed -> CentralSyncOutcome.AuthenticationRequired(
                result.statusCode
            )
            is CentralSyncLoopResult.Failed -> CentralSyncOutcome.Failed(result.reason)
            // InProgress shouldn't happen with maxBatchesPerRun=1, but handle gracefully
            is CentralSyncLoopResult.InProgress -> CentralSyncOutcome.Completed(
                confirmedCount = result.cumulativeConfirmed,
                rejectedCount = result.cumulativeRejected,
                unconfirmedCount = result.remainingQueue
            )
        }
    }

    companion object {
        const val CENTRAL_TARGET_ID = "central"
        const val MAX_BATCH_SIZE = 500
        private val syncMutex = Mutex()
    }
}
