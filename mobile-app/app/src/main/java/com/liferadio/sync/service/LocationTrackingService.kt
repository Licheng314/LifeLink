package com.liferadio.sync.service

import android.Manifest
import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.os.IBinder
import android.os.Looper
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.liferadio.sync.LifeRadioApp
import com.liferadio.sync.MainActivity
import com.liferadio.sync.R
import com.liferadio.sync.data.local.AppDatabase
import com.liferadio.sync.data.local.LocationEventCollector
import com.liferadio.sync.data.local.MotionWindowSnapshot
import com.liferadio.sync.data.local.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sqrt

class LocationTrackingService : Service(), SensorEventListener {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private lateinit var settings: SettingsStore
    private lateinit var collector: LocationEventCollector
    private lateinit var sensorManager: SensorManager
    private var locationUpdatesRequested = false
    private var motionMonitoringStarted = false
    private var accelerometerAvailable = false
    private var motionWindowStartedAt = 0L
    private var accelerometerSampleCount = 0
    private var motionTriggerCount = 0
    private var peakMotionDeltaMetersPerSecondSquared = 0f
    private var filteredGravityMetersPerSecondSquared = SensorManager.GRAVITY_EARTH
    private var motionThresholdExceeded = false

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            handleLocations(result.locations)
        }
    }

    override fun onCreate() {
        super.onCreate()
        settings = SettingsStore(this)
        collector = LocationEventCollector(this, AppDatabase.getInstance(this), settings)
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        sensorManager = getSystemService(SENSOR_SERVICE) as SensorManager
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                settings.isLocationTrackingEnabled = false
                stopTracking(flush = true)
                return START_NOT_STICKY
            }
        }

        if (!hasLocationPermission()) {
            settings.isLocationTrackingEnabled = false
            stopSelf()
            return START_NOT_STICKY
        }

        settings.isLocationTrackingEnabled = true
        isRunning = true
        startForeground(NOTIFICATION_ID, createNotification("正在检测位置"))
        startMotionMonitoring()
        serviceScope.launch { collector.flushActiveCluster() }
        requestLocationUpdates()
        return START_STICKY
    }

    private fun handleLocations(locations: List<Location>) {
        val receivedAt = System.currentTimeMillis()
        val acceptedLocations = locations
            .asSequence()
            .filter { location -> isAcceptableLocation(location, receivedAt) }
            .distinctBy { location ->
                "${location.time / 1000L}|${location.latitude}|${location.longitude}|${location.provider}"
            }
            .sortedBy { location -> location.time }
            .toList()
        if (acceptedLocations.isEmpty()) {
            return
        }

        val motionWindow = finishMotionWindow(receivedAt)
        serviceScope.launch {
            acceptedLocations.forEachIndexed { index, location ->
                val locationMotionWindow = if (index == acceptedLocations.lastIndex) {
                    motionWindow
                } else {
                    MotionWindowSnapshot(
                        startedAt = location.time,
                        endedAt = location.time,
                        accelerometerAvailable = false,
                        sensorSampleCount = 0,
                        triggerCount = 0,
                        thresholdMetersPerSecondSquared = MOTION_TRIGGER_THRESHOLD_METERS_PER_SECOND_SQUARED,
                        peakDeltaMetersPerSecondSquared = 0f
                    )
                }
                collector.record(location, locationMotionWindow, receivedAt)
            }
            val batchText = if (acceptedLocations.size > 1) {
                " · 补记 ${acceptedLocations.size} 条"
            } else {
                ""
            }
            updateNotification("正在检测位置 · 5 分钟高精度$batchText")
        }
    }

    private fun isAcceptableLocation(location: Location, receivedAt: Long): Boolean {
        if (location.accuracy > MAX_ACCEPTED_ACCURACY_METERS || location.time <= 0L) {
            return false
        }
        val ageMillis = receivedAt - location.time
        return ageMillis in -MAX_FUTURE_LOCATION_OFFSET_MILLIS..MAX_LOCATION_AGE_MILLIS
    }

    private fun startMotionMonitoring() {
        if (motionMonitoringStarted) {
            return
        }
        motionMonitoringStarted = true
        motionWindowStartedAt = System.currentTimeMillis()
        val accelerometer = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        accelerometerAvailable = accelerometer != null &&
            sensorManager.registerListener(this, accelerometer, SensorManager.SENSOR_DELAY_NORMAL)
    }

    private fun finishMotionWindow(observedAt: Long): MotionWindowSnapshot {
        val snapshot = MotionWindowSnapshot(
            startedAt = motionWindowStartedAt.takeIf { it > 0L } ?: observedAt,
            endedAt = observedAt,
            accelerometerAvailable = accelerometerAvailable,
            sensorSampleCount = accelerometerSampleCount,
            triggerCount = motionTriggerCount,
            thresholdMetersPerSecondSquared = MOTION_TRIGGER_THRESHOLD_METERS_PER_SECOND_SQUARED,
            peakDeltaMetersPerSecondSquared = peakMotionDeltaMetersPerSecondSquared
        )
        motionWindowStartedAt = observedAt
        accelerometerSampleCount = 0
        motionTriggerCount = 0
        peakMotionDeltaMetersPerSecondSquared = 0f
        motionThresholdExceeded = false
        return snapshot
    }

    override fun onSensorChanged(event: SensorEvent?) {
        if (event?.sensor?.type != Sensor.TYPE_ACCELEROMETER || event.values.size < 3) {
            return
        }
        val magnitude = sqrt(
            event.values[0] * event.values[0] +
                event.values[1] * event.values[1] +
                event.values[2] * event.values[2]
        )
        filteredGravityMetersPerSecondSquared =
            GRAVITY_FILTER_ALPHA * filteredGravityMetersPerSecondSquared +
                (1f - GRAVITY_FILTER_ALPHA) * magnitude
        val motionDelta = abs(magnitude - filteredGravityMetersPerSecondSquared)
        accelerometerSampleCount++
        peakMotionDeltaMetersPerSecondSquared = max(peakMotionDeltaMetersPerSecondSquared, motionDelta)

        if (!motionThresholdExceeded && motionDelta >= MOTION_TRIGGER_THRESHOLD_METERS_PER_SECOND_SQUARED) {
            motionTriggerCount++
            motionThresholdExceeded = true
        } else if (
            motionThresholdExceeded &&
            motionDelta < MOTION_TRIGGER_RESET_THRESHOLD_METERS_PER_SECOND_SQUARED
        ) {
            motionThresholdExceeded = false
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    private fun requestLocationUpdates() {
        if (!settings.isLocationTrackingEnabled || !hasLocationPermission()) {
            return
        }
        if (locationUpdatesRequested) {
            return
        }
        try {
            val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, COLLECTION_INTERVAL_MILLIS)
                .setMinUpdateIntervalMillis(COLLECTION_INTERVAL_MILLIS)
                .setMaxUpdateDelayMillis(0L)
                .setMaxUpdateAgeMillis(0L)
                .setWaitForAccurateLocation(true)
                .build()
            fusedLocationClient.requestLocationUpdates(request, locationCallback, Looper.getMainLooper())
            locationUpdatesRequested = true
        } catch (_: SecurityException) {
            settings.isLocationTrackingEnabled = false
            stopTracking(flush = true)
        }
    }

    private fun hasLocationPermission(): Boolean =
        ActivityCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
            ActivityCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

    private fun stopTracking(flush: Boolean) {
        removeLocationUpdates()
        stopMotionMonitoring()
        isRunning = false
        if (flush) {
            runBlocking(Dispatchers.IO) { collector.flushActiveCluster() }
        }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        removeLocationUpdates()
        stopMotionMonitoring()
        isRunning = false
        serviceScope.cancel()
        super.onDestroy()
    }

    private fun removeLocationUpdates() {
        if (locationUpdatesRequested) {
            fusedLocationClient.removeLocationUpdates(locationCallback)
            locationUpdatesRequested = false
        }
    }

    private fun stopMotionMonitoring() {
        if (motionMonitoringStarted) {
            sensorManager.unregisterListener(this)
            motionMonitoringStarted = false
        }
    }

    private fun createNotification(content: String): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, LifeRadioApp.CHANNEL_ID)
            .setContentTitle("Life Link 位置采集")
            .setContentText(content)
            .setSmallIcon(R.drawable.ic_sync)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(content: String) {
        val notificationManager = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        notificationManager.notify(NOTIFICATION_ID, createNotification(content))
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val ACTION_START = "com.liferadio.sync.location.START"
        const val ACTION_STOP = "com.liferadio.sync.location.STOP"
        private const val NOTIFICATION_ID = 2
        private const val MAX_ACCEPTED_ACCURACY_METERS = 500f
        private const val COLLECTION_INTERVAL_MILLIS = 5 * 60 * 1000L
        private const val MAX_LOCATION_AGE_MILLIS = 10 * 60 * 1000L
        private const val MAX_FUTURE_LOCATION_OFFSET_MILLIS = 2 * 60 * 1000L
        private const val MOTION_TRIGGER_THRESHOLD_METERS_PER_SECOND_SQUARED = 0.7f
        private const val MOTION_TRIGGER_RESET_THRESHOLD_METERS_PER_SECOND_SQUARED = 0.4f
        private const val GRAVITY_FILTER_ALPHA = 0.9f

        @Volatile
        var isRunning: Boolean = false
            private set

        fun start(context: Context) {
            val intent = Intent(context, LocationTrackingService::class.java).apply { action = ACTION_START }
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, LocationTrackingService::class.java).apply { action = ACTION_STOP }
            context.startService(intent)
        }
    }
}
