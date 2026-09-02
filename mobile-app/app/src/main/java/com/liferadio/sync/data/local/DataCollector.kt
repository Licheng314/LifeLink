package com.liferadio.sync.data.local

import android.app.usage.UsageStatsManager
import android.app.usage.UsageEvents
import android.app.AppOpsManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Process
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.*
import java.nio.charset.StandardCharsets

/**
 * 数据收集器 — 基于 Android UsageStats 系统事件采集前台应用会话。
 */
class DataCollector(private val context: Context) {

    private val moshi = Moshi.Builder()
        .add(KotlinJsonAdapterFactory())
        .build()

    private val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("UTC")
    }

    /**
     * 从 Android UsageStats 的系统事件流收集前台应用会话。
     *
     * 每个事件 ID 根据包名、Activity 和开始时间稳定生成，
     * 因此重复扫描不会生成重复记录。正在进行中的会话会等到下次切换或后台事件后入库。
     */
    suspend fun collectNativeUsageEvents(): List<DataEventEntity> = withContext(Dispatchers.IO) {
        if (!hasUsageStatsPermission()) return@withContext emptyList()

        val settings = SettingsStore(context)
        val endTime = System.currentTimeMillis()
        val todayStart = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, 0)
            set(Calendar.MINUTE, 0)
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
        }.timeInMillis
        val startTime = settings.nativeUsageLastCollectedAt
            .takeIf { it in todayStart until endTime }
            ?: todayStart

        if (startTime >= endTime) return@withContext emptyList()

        try {
            val usageStatsManager = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
            val usageEvents = usageStatsManager.queryEvents(startTime, endTime)
            val nativeEvents = mutableListOf<DataEventEntity>()
            var activeSession = settings.getNativeUsageActiveSession()
            val usageEvent = UsageEvents.Event()

            while (usageEvents.hasNextEvent()) {
                usageEvents.getNextEvent(usageEvent)
                val packageName = usageEvent.packageName ?: continue
                val eventTime = usageEvent.timeStamp

                when (usageEvent.eventType) {
                    UsageEvents.Event.MOVE_TO_FOREGROUND -> {
                        activeSession?.let { session ->
                            toDataEvent(session, eventTime)?.let(nativeEvents::add)
                        }
                        activeSession = NativeUsageSession(
                            packageName = packageName,
                            className = usageEvent.className.orEmpty(),
                            startedAt = eventTime
                        )
                    }
                    UsageEvents.Event.MOVE_TO_BACKGROUND -> {
                        val session = activeSession
                        if (session != null && session.packageName == packageName) {
                            toDataEvent(session, eventTime)?.let(nativeEvents::add)
                            activeSession = null
                        }
                    }
                }
            }

            if (activeSession == null) {
                settings.clearNativeUsageActiveSession()
            } else {
                settings.saveNativeUsageActiveSession(activeSession)
            }
            settings.nativeUsageLastCollectedAt = endTime
            nativeEvents
        } catch (e: Exception) {
            emptyList()
        }
    }

    fun hasUsageStatsPermission(): Boolean {
        val appOps = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        return appOps.checkOpNoThrow(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            Process.myUid(),
            context.packageName
        ) == AppOpsManager.MODE_ALLOWED
    }

    private fun toDataEvent(session: NativeUsageSession, endedAt: Long): DataEventEntity? {
        val durationSeconds = ((endedAt - session.startedAt) / 1000L).toInt()
        if (durationSeconds <= 0) return null

        val data = mapOf(
            "app" to mapOf(
                "display_name" to getAppName(session.packageName),
                "package_name" to session.packageName
            ),
            "classname" to session.className
        )
        val idInput = "native_usage|${session.packageName}|${session.className}|${session.startedAt}"
        val eventId = UUID.nameUUIDFromBytes(idInput.toByteArray(StandardCharsets.UTF_8)).toString()

        return DataEventEntity(
            id = eventId,
            source = "android_usage_events",
            sourceType = "android",
            dataType = "app_usage",
            timestamp = dateFormat.format(Date(session.startedAt)),
            duration = durationSeconds,
            dataJson = moshi.adapter(Map::class.java).toJson(data),
            synced = false
        )
    }

    private fun getAppName(packageName: String): String {
        return try {
            val pm = context.packageManager
            val appInfo = pm.getApplicationInfo(packageName, 0)
            pm.getApplicationLabel(appInfo).toString()
        } catch (e: Exception) {
            packageName.substringAfterLast(".")
        }
    }
}
