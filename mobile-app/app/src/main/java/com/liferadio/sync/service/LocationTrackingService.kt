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
import android.location.LocationListener
import android.location.LocationManager
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationAvailability
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import com.liferadio.sync.LifeRadioApp
import com.liferadio.sync.MainActivity
import com.liferadio.sync.R
import com.liferadio.sync.data.local.AppDatabase
import com.liferadio.sync.data.local.LocationEventCollector
import com.liferadio.sync.data.local.MotionWindowSnapshot
import com.liferadio.sync.data.local.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sqrt

class LocationTrackingService : Service(), SensorEventListener {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private lateinit var locationManager: LocationManager
    private lateinit var settings: SettingsStore
    private lateinit var collector: LocationEventCollector
    private lateinit var sensorManager: SensorManager
    private var locationUpdatesRequested = false
    private var locationWatchdog: Job? = null
    private var lastRecoveryAttemptAt = 0L
    private var nativeFallbackRequested = false
    @Volatile private var immediateRequestInFlight = false
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

        override fun onLocationAvailability(availability: LocationAvailability) {
            val message = if (availability.isLocationAvailable) {
                "系统定位源可用，等待有效定位"
            } else {
                "系统定位源暂不可用，正在等待恢复"
            }
            recordDiagnostic(message)
        }
    }

    private val nativeLocationListener = LocationListener { location ->
        handleLocations(listOf(location))
    }

    override fun onCreate() {
        super.onCreate()
        settings = SettingsStore(this)
        collector = LocationEventCollector(this, AppDatabase.getInstance(this), settings)
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        locationManager = getSystemService(LOCATION_SERVICE) as LocationManager
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
        recordDiagnostic("定位服务已启动，等待首次有效定位")
        startMotionMonitoring()
        serviceScope.launch { collector.flushActiveCluster() }
        requestLocationUpdates()
        startLocationWatchdog()
        scheduleInitialRecoveryIfStale()
        return START_STICKY
    }

    private fun handleLocations(locations: List<Location>) {
        val receivedAt = System.currentTimeMillis()
        var rejectionReason: String? = null
        val acceptedLocations = locations
            .asSequence()
            .filter { location ->
                val reason = unacceptableLocationReason(location, receivedAt)
                if (reason != null) {
                    rejectionReason = reason
                    false
                } else {
                    true
                }
            }
            .distinctBy { location ->
                "${location.time / 1000L}|${location.latitude}|${location.longitude}|${location.provider}"
            }
            .sortedBy { location -> location.time }
            .toList()
        if (acceptedLocations.isEmpty()) {
            recordDiagnostic(rejectionReason ?: "定位回调未包含有效数据")
            return
        }

        stopNativeLocationFallback()
        recordDiagnostic("已收到有效定位")
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

    private fun unacceptableLocationReason(location: Location, receivedAt: Long): String? {
        if (location.accuracy > MAX_ACCEPTED_ACCURACY_METERS) {
            return "定位精度过低（约 ${location.accuracy.toInt()} 米）"
        }
        if (location.time <= 0L) {
            return "定位结果缺少有效时间"
        }
        val ageMillis = receivedAt - location.time
        return if (ageMillis !in -MAX_FUTURE_LOCATION_OFFSET_MILLIS..MAX_LOCATION_AGE_MILLIS) {
            "定位结果时间过期"
        } else {
            null
        }
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

    private fun requestLocationUpdates(recordRegistration: Boolean = true) {
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
                .setWaitForAccurateLocation(false)
                .build()
            fusedLocationClient.requestLocationUpdates(request, locationCallback, Looper.getMainLooper())
                .addOnSuccessListener {
                    locationUpdatesRequested = true
                    if (recordRegistration) {
                        recordDiagnostic("定位请求已注册，等待系统回调")
                    }
                }
                .addOnFailureListener { error ->
                    locationUpdatesRequested = false
                    recordDiagnostic("定位请求失败：${error.javaClass.simpleName}")
                    updateNotification("定位请求失败，请检查系统定位")
                }
        } catch (_: SecurityException) {
            recordDiagnostic("位置权限不可用")
            settings.isLocationTrackingEnabled = false
            stopTracking(flush = true)
        }
    }

    private fun startLocationWatchdog() {
        locationWatchdog?.cancel()
        locationWatchdog = serviceScope.launch {
            while (isActive) {
                delay(WATCHDOG_INTERVAL_MILLIS)
                val now = System.currentTimeMillis()
                if (
                    LocationTrackingHealthPolicy.isStale(
                        enabled = settings.isLocationTrackingEnabled,
                        lastAcceptedAt = settings.lastLocationDetectedAt,
                        now = now
                    ) && now - lastRecoveryAttemptAt >= RECOVERY_MIN_INTERVAL_MILLIS
                ) {
                    recoverLocationRequest(now)
                }
            }
        }
    }

    private fun scheduleInitialRecoveryIfStale() {
        serviceScope.launch {
            delay(INITIAL_RECOVERY_DELAY_MILLIS)
            val now = System.currentTimeMillis()
            if (
                LocationTrackingHealthPolicy.isStale(
                    enabled = settings.isLocationTrackingEnabled,
                    lastAcceptedAt = settings.lastLocationDetectedAt,
                    now = now
                ) && now - lastRecoveryAttemptAt >= RECOVERY_MIN_INTERVAL_MILLIS
            ) {
                recoverLocationRequest(now)
            }
        }
    }

    private fun recoverLocationRequest(now: Long) {
        lastRecoveryAttemptAt = now
        recordDiagnostic("超过 15 分钟未收到有效定位，正在重新请求")
        updateNotification("定位停滞，正在重新请求")
        locationUpdatesRequested = false
        fusedLocationClient.removeLocationUpdates(locationCallback).addOnCompleteListener {
            requestLocationUpdates(recordRegistration = false)
            requestImmediateLocation()
        }
        requestNativeLocationFallback()
    }

    private fun requestImmediateLocation() {
        if (!settings.isLocationTrackingEnabled || !hasLocationPermission()) {
            return
        }
        try {
            val requestedAt = System.currentTimeMillis()
            immediateRequestInFlight = true
            val cancellation = CancellationTokenSource()
            fusedLocationClient.getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, cancellation.token)
                .addOnSuccessListener { location ->
                    immediateRequestInFlight = false
                    if (location == null) {
                        recordDiagnostic("系统没有返回即时定位，请检查系统位置服务与后台限制")
                    } else {
                        handleLocations(listOf(location))
                    }
                }
                .addOnFailureListener { error ->
                    immediateRequestInFlight = false
                    recordDiagnostic("即时定位失败：${error.javaClass.simpleName}")
                }
            serviceScope.launch {
                delay(IMMEDIATE_LOCATION_TIMEOUT_MILLIS)
                if (immediateRequestInFlight && settings.lastLocationDetectedAt < requestedAt) {
                    immediateRequestInFlight = false
                    cancellation.cancel()
                    recordDiagnostic("即时定位等待超时，系统定位备用通道仍在监听")
                }
            }
        } catch (_: SecurityException) {
            immediateRequestInFlight = false
            recordDiagnostic("即时定位缺少可用权限")
        }
    }

    private fun requestNativeLocationFallback() {
        if (nativeFallbackRequested || !settings.isLocationTrackingEnabled || !hasLocationPermission()) {
            return
        }
        val providers = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .filter { provider -> runCatching { locationManager.isProviderEnabled(provider) }.getOrDefault(false) }
        if (providers.isEmpty()) {
            recordDiagnostic("系统定位总开关未开启，或没有可用的定位源")
            return
        }
        var registered = 0
        providers.forEach { provider ->
            try {
                locationManager.requestLocationUpdates(
                    provider,
                    COLLECTION_INTERVAL_MILLIS,
                    0f,
                    nativeLocationListener,
                    Looper.getMainLooper()
                )
                registered++
            } catch (_: SecurityException) {
                recordDiagnostic("系统定位备用通道缺少可用权限")
            } catch (error: IllegalArgumentException) {
                Log.w(TAG, "Native location provider unavailable: $provider", error)
            }
        }
        nativeFallbackRequested = registered > 0
        if (nativeFallbackRequested) {
            recordDiagnostic("融合定位无回调，已启用系统定位备用通道")
        }
    }

    private fun stopNativeLocationFallback() {
        if (!nativeFallbackRequested) return
        locationManager.removeUpdates(nativeLocationListener)
        nativeFallbackRequested = false
    }

    private fun recordDiagnostic(message: String) {
        Log.i(TAG, message)
        settings.recordLocationDiagnostic(message)
    }

    private fun hasLocationPermission(): Boolean =
        ActivityCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
            ActivityCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

    private fun stopTracking(flush: Boolean) {
        locationWatchdog?.cancel()
        locationWatchdog = null
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
        locationWatchdog?.cancel()
        locationWatchdog = null
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
        stopNativeLocationFallback()
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
        private const val WATCHDOG_INTERVAL_MILLIS = 60 * 1000L
        private const val INITIAL_RECOVERY_DELAY_MILLIS = 5 * 1000L
        private const val RECOVERY_MIN_INTERVAL_MILLIS = 15 * 60 * 1000L
        private const val IMMEDIATE_LOCATION_TIMEOUT_MILLIS = 30 * 1000L
        private const val MOTION_TRIGGER_THRESHOLD_METERS_PER_SECOND_SQUARED = 0.7f
        private const val MOTION_TRIGGER_RESET_THRESHOLD_METERS_PER_SECOND_SQUARED = 0.4f
        private const val GRAVITY_FILTER_ALPHA = 0.9f
        private const val TAG = "LifeLinkLocation"

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
