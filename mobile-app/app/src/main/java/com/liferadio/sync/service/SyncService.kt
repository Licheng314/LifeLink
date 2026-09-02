package com.liferadio.sync.service

import android.app.AlarmManager
import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import com.liferadio.sync.LifeRadioApp
import com.liferadio.sync.MainActivity
import com.liferadio.sync.R
import com.liferadio.sync.data.local.AppDatabase
import com.liferadio.sync.data.local.DataCollector
import com.liferadio.sync.data.local.SettingsStore
import com.liferadio.sync.data.local.StepCounterCollector
import com.liferadio.sync.data.remote.CentralSharedSettingsClient
import com.liferadio.sync.data.remote.CentralSharedSettingsValidator
import com.liferadio.sync.data.remote.CentralWishClient
import com.liferadio.sync.data.remote.SharedSettingsFetchResult
import com.liferadio.sync.data.remote.WishResult
import java.time.Instant
import kotlinx.coroutines.*

class SyncService : Service() {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var database: AppDatabase
    private lateinit var settings: SettingsStore
    private lateinit var centralSyncCoordinator: CentralSyncCoordinator
    private lateinit var stepCounter: StepCounterCollector
    private var wakeLock: PowerManager.WakeLock? = null

    private var isRunning = false
    private var isForeground = false
    // track cumulative sync stats across the loop
    private var loopConfirmed = 0
    private var loopRemaining = 0
    private var loopBatches = 0

    companion object {
        private const val TAG = "SyncService"
        const val ACTION_START = "com.liferadio.sync.START"
        const val ACTION_STOP = "com.liferadio.sync.STOP"
        const val ACTION_SYNC_NOW = "com.liferadio.sync.SYNC_NOW"
        const val ACTION_COLLECT_NOW = "com.liferadio.sync.COLLECT_NOW"
        const val ACTION_STEP_PERMISSION_CHANGED = "com.liferadio.sync.STEP_PERMISSION_CHANGED"

        private const val NOTIFICATION_ID = 1
        private const val IMPORTANT_EVENT_NOTIFICATION_ID = 2

        fun start(context: Context) {
            val intent = Intent(context, SyncService::class.java).apply { action = ACTION_START }
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, SyncService::class.java).apply { action = ACTION_STOP }
            context.startService(intent)
        }

        fun refreshStepPermission(context: Context) {
            val intent = Intent(context, SyncService::class.java).apply {
                action = ACTION_STEP_PERMISSION_CHANGED
            }
            context.startForegroundService(intent)
        }
    }

    override fun onCreate() {
        super.onCreate()
        database = AppDatabase.getInstance(this)
        settings = SettingsStore(this)
        centralSyncCoordinator = CentralSyncCoordinator(database, settings)
        stepCounter = StepCounterCollector(this, database, settings)

        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "LifeRadio::SyncWakeLock"
        ).apply { setReferenceCounted(false) }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        android.util.Log.d(TAG, "onStartCommand: action=${intent?.action}, isRunning=$isRunning, isForeground=$isForeground")

        when (intent?.action) {
            ACTION_STOP -> {
                isRunning = false
                cancelAlarms()
                serviceScope.cancel()
                wakeLock?.release()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_SYNC_NOW -> {
                // ensure foreground service is started even if woken by alarm
                ensureForeground()
                serviceScope.launch { performSync() }
                return START_STICKY
            }
            ACTION_COLLECT_NOW -> {
                ensureForeground()
                serviceScope.launch { collectData() }
                return START_STICKY
            }
            ACTION_STEP_PERMISSION_CHANGED -> {
                ensureForeground()
                val wasRunning = isRunning
                isRunning = true
                stepCounter.start()
                serviceScope.launch { collectData() }
                if (!wasRunning) scheduleAlarms()
                return START_STICKY
            }
        }

        // initial start or manual restart
        if (isRunning) {
            android.util.Log.d(TAG, "Service already running, skip init")
            return START_STICKY
        }

        ensureForeground()
        isRunning = true
        stepCounter.start()

        // bootstrap: collect + sync after short delay
        serviceScope.launch {
            delay(3_000L)
            collectData()
            delay(5_000L)
            performSync()
        }

        scheduleAlarms()

        return START_STICKY
    }

    private fun ensureForeground() {
        if (!isForeground) {
            startForeground(NOTIFICATION_ID, createNotification("Life Link", "服务运行中"))
            isForeground = true
        }
    }

    private fun scheduleAlarms() {
        scheduleNextCollect()
        scheduleNextSync()
    }

    private fun cancelAlarms() {
        val alarmManager = getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val collectIntent = Intent(this, SyncService::class.java).apply { action = ACTION_COLLECT_NOW }
        val syncIntent = Intent(this, SyncService::class.java).apply { action = ACTION_SYNC_NOW }
        alarmManager.cancel(PendingIntent.getService(this, 1002, collectIntent, PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE))
        alarmManager.cancel(PendingIntent.getService(this, 1001, syncIntent, PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE))
    }

    private fun scheduleNextCollect() {
        val alarmManager = getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val collectIntent = Intent(this, SyncService::class.java).apply { action = ACTION_COLLECT_NOW }
        val collectPending = PendingIntent.getService(
            this, 1002, collectIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        alarmManager.setAndAllowWhileIdle(
            AlarmManager.ELAPSED_REALTIME_WAKEUP,
            SystemClock.elapsedRealtime() + 5 * 60 * 1000L,
            collectPending
        )
        android.util.Log.d(TAG, "Collect alarm scheduled in 5 min")
    }

    private fun scheduleNextSync() {
        val alarmManager = getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val regularIntervalMs = settings.syncIntervalMinutes * 60 * 1000L
        val retryDelayMs = (settings.centralNextRetryAt - System.currentTimeMillis()).coerceAtLeast(0L)
        val syncIntervalMs = if (
            settings.syncMode == SettingsStore.SYNC_MODE_CENTRAL && retryDelayMs > 0L
        ) {
            minOf(retryDelayMs, regularIntervalMs.coerceAtLeast(60_000L))
        } else {
            regularIntervalMs
        }
        val syncIntent = Intent(this, SyncService::class.java).apply { action = ACTION_SYNC_NOW }
        val syncPending = PendingIntent.getService(
            this, 1001, syncIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        alarmManager.setAndAllowWhileIdle(
            AlarmManager.ELAPSED_REALTIME_WAKEUP,
            SystemClock.elapsedRealtime() + syncIntervalMs.coerceAtLeast(5_000L),
            syncPending
        )
        android.util.Log.d(TAG, "Sync alarm scheduled in ${syncIntervalMs / 1000}s")
    }

    private suspend fun collectData() {
        try {
            android.util.Log.d(TAG, "collectData() started")
            val collector = DataCollector(this@SyncService)
            val nativeEvents = collector.collectNativeUsageEvents()
            android.util.Log.d(TAG, "collectData: got ${nativeEvents.size} native usage events")
            if (nativeEvents.isNotEmpty()) {
                database.dataEventDao().insertAll(nativeEvents)
            }
            stepCounter.pollSample()
            val pending = database.dataEventDao().getCentralPendingCountBlocking(
                CentralSyncCoordinator.CENTRAL_TARGET_ID
            )
            updateNotification("Life Link", "待同步: $pending 条")
        } catch (e: Exception) {
            android.util.Log.e(TAG, "collectData failed", e)
        }
        scheduleNextCollect()
    }

    private suspend fun performSync() {
        android.util.Log.d(TAG, "performSync() started")
        try {
            wakeLock?.acquire(120_000L) // allow up to 2 min
        } catch (e: Exception) {
            android.util.Log.w(TAG, "wakeLock acquire failed", e)
        }

        try {
            performCentralSync()
        } catch (e: Exception) {
            android.util.Log.e(TAG, "performSync error", e)
            updateNotification("Life Link", "同步错误: ${e.message}")
        } finally {
            try { wakeLock?.release() } catch (_: Exception) {}
            scheduleNextSync()
        }
    }

    private suspend fun performCentralSync() {
        // reset local tracking
        loopConfirmed = 0
        loopRemaining = 0
        loopBatches = 0

        val result = centralSyncCoordinator.syncLoop(
            force = false,
            maxBatchesPerRun = 20,
            onProgress = { progress ->
                if (progress is CentralSyncLoopResult.InProgress) {
                    loopConfirmed = progress.cumulativeConfirmed
                    loopRemaining = progress.remainingQueue
                    loopBatches = progress.batchCount
                    updateNotification(
                        "Life Link",
                        "同步中：已确认 ${loopConfirmed} 条，队列剩余 ${loopRemaining} 条"
                    )
                }
            }
        )

        when (result) {
            is CentralSyncLoopResult.Completed ->
                updateNotification(
                    "Life Link",
                    "同步完成：总计 ${result.totalConfirmed} 条（${result.batchCount} 批）"
                )
            is CentralSyncLoopResult.Stopped ->
                updateNotification(
                    "Life Link",
                    "同步暂停：已确认 ${result.cumulativeConfirmed} 条，${result.reason}"
                )
            is CentralSyncLoopResult.AuthFailed ->
                updateNotification(
                    "Life Link",
                    "认证失败（${result.statusCode}），请检查设备 Token"
                )
            is CentralSyncLoopResult.Failed ->
                updateNotification(
                    "Life Link",
                    if (result.reason == "central connection failed") {
                        "同步失败：无法连接中央服务，请检查网络、Tailscale 或内网穿透"
                    } else {
                        "同步失败：${result.reason}"
                    }
                )
            is CentralSyncLoopResult.InProgress -> {
                // shouldn't happen as final result, but handle gracefully
            }
        }
        // Do not make settings I/O part of the event upload critical path.
        serviceScope.launch { refreshSharedSettings() }
        serviceScope.launch { notifyNewTimelineEvents() }
    }

    /** Central remains the only event authority; the phone only remembers which alerts it has shown. */
    private suspend fun notifyNewTimelineEvents() {
        if (settings.centralBaseUrl.isBlank() || !settings.isCentralTokenConfigured) return
        val now = Instant.now()
        val result = CentralWishClient(settings.centralBaseUrl, settings::getCentralToken)
            .listTimeline(now.minusSeconds(48 * 60 * 60).toString(), now.toString())
        if (result !is WishResult.Success) return
        val events = (result.data as? com.liferadio.sync.data.model.TimelineEventListResponse)
            ?.events.orEmpty()
            .filter { TimelineNotificationPolicy.shouldNotify(it.importance) }
            .sortedBy { it.occurredAt }
        val newIds = settings.recordNewAlertableTimelineEventIds(events.map { it.timelineEventId })
        if (newIds.isEmpty() || !canPostNotifications()) return
        val latest = events.lastOrNull { it.timelineEventId in newIds } ?: return
        val count = newIds.size
        val text = if (count == 1) latest.detail?.takeIf(String::isNotBlank) ?: latest.title
            else "检测到 $count 条新事件：${latest.title}"
        val titlePrefix = TimelineNotificationPolicy.titlePrefix(latest.importance)
        val pendingIntent = PendingIntent.getActivity(
            this, IMPORTANT_EVENT_NOTIFICATION_ID, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = NotificationCompat.Builder(this, LifeRadioApp.IMPORTANT_EVENTS_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_lucide_circle_alert)
            .setContentTitle("$titlePrefix：${latest.title}")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()
        (getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager)
            .notify(IMPORTANT_EVENT_NOTIFICATION_ID, notification)
    }

    private fun canPostNotifications(): Boolean =
        android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) == android.content.pm.PackageManager.PERMISSION_GRANTED

    /** Settings refresh is best-effort and intentionally cannot affect event upload state. */
    private suspend fun refreshSharedSettings() {
        if (settings.centralBaseUrl.isBlank() || !settings.isCentralTokenConfigured) return
        val result = runCatching {
            CentralSharedSettingsClient(settings.centralBaseUrl, settings::getCentralToken).fetch()
        }.getOrNull()
        if (result is SharedSettingsFetchResult.Success) {
            settings.saveSharedSettingsCache(
                CentralSharedSettingsValidator.toCache(result.settings, System.currentTimeMillis())
            )
        }
    }

    private fun createNotification(title: String, content: String): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, LifeRadioApp.CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(content)
            .setSmallIcon(R.drawable.ic_sync)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(title: String, content: String) {
        val notification = createNotification(title, content)
        val nm = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.notify(NOTIFICATION_ID, notification)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        android.util.Log.d(TAG, "onDestroy")
        isRunning = false
        isForeground = false
        serviceScope.cancel()
        stepCounter.stop()
        try { wakeLock?.release() } catch (_: Exception) {}
    }

    // ---- Boot receiver ----

    class BootReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
                android.util.Log.d(TAG, "Boot completed, starting SyncService")
                SyncService.start(context)
            }
        }
    }
}
