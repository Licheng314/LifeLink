package com.liferadio.sync.data.local

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.Handler
import android.provider.Settings
import android.os.HandlerThread
import androidx.core.content.ContextCompat
import androidx.room.withTransaction
import java.time.Instant
import java.util.UUID

/** Owns the sensor listener and persists each sampled cumulative observation. */
class StepCounterCollector(
    private val context: Context,
    private val database: AppDatabase,
    private val settings: SettingsStore
) {
    private val manager = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val sensor = manager.getDefaultSensor(Sensor.TYPE_STEP_COUNTER)
    @Volatile private var latest: Long? = null
    private var thread: HandlerThread? = null

    val isSensorAvailable get() = sensor != null
    val hasPermission get() = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACTIVITY_RECOGNITION) == PackageManager.PERMISSION_GRANTED

    private val listener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            if (event.sensor.type == Sensor.TYPE_STEP_COUNTER) latest = event.values[0].toLong()
        }
        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
    }

    fun start(): Boolean {
        if (!hasPermission || sensor == null || thread != null) return false
        val candidate = HandlerThread("LifeRadioStepCounter").also { it.start() }
        val registered = manager.registerListener(
            listener, sensor, SensorManager.SENSOR_DELAY_NORMAL, Handler(candidate.looper)
        )
        if (!registered) {
            candidate.quitSafely()
            return false
        }
        thread = candidate
        return true
    }

    suspend fun pollSample(now: Instant = Instant.now()): Boolean {
        val value = latest ?: return false
        val previous = settings.stepCounterLastValue
        val bootCount = Settings.Global.getInt(context.contentResolver, Settings.Global.BOOT_COUNT, -1)
        val previousBootCount = settings.stepCounterBootCount
        var sessionId = settings.stepCounterSessionId
        if (
            runCatching { UUID.fromString(sessionId) }.isFailure ||
            (previous >= 0 && value < previous) ||
            (bootCount >= 0 && previousBootCount >= 0 && bootCount != previousBootCount)
        ) {
            sessionId = UUID.randomUUID().toString()
        }
        // Persist the session before the observation transaction so a process
        // death cannot make the next reading invent a second session.
        settings.stepCounterSessionId = sessionId
        if (bootCount >= 0) settings.stepCounterBootCount = bootCount
        val occurredAt = now.toEpochMilli()
        val eventId = UUID.nameUUIDFromBytes("health.steps_observation|$sessionId|$occurredAt|$value".toByteArray()).toString()
        val timestamp = now.toString()
        val payload = """{"counter_value":$value,"counter_session_id":"$sessionId","sensor_type":"android.step_counter"}"""
        database.withTransaction {
            database.stepObservationDao().insert(StepObservationEntity(eventId, occurredAt, value, sessionId))
            database.dataEventDao().insert(DataEventEntity(
                id = eventId, source = "android_step_counter", sourceType = "android",
                dataType = "health.steps_observation", timestamp = timestamp, duration = 0, dataJson = payload
            ))
        }
        settings.stepCounterLastValue = value
        return true
    }

    fun stop() {
        manager.unregisterListener(listener)
        thread?.quitSafely()
        thread = null
    }
}
