package com.liferadio.sync.ui.screens

import android.app.Application
import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.liferadio.sync.data.local.AppDatabase
import com.liferadio.sync.data.local.ResolvedPlace
import com.liferadio.sync.data.local.SettingsStore
import com.liferadio.sync.data.model.SyncStatus
import com.liferadio.sync.data.remote.CentralEnrollmentClient
import com.liferadio.sync.data.remote.CentralInvitation
import com.liferadio.sync.data.remote.CentralInvitationParser
import com.liferadio.sync.data.remote.EnrollmentResult
import com.liferadio.sync.data.remote.CentralSharedSettingsClient
import com.liferadio.sync.data.remote.CentralSharedSettings
import com.liferadio.sync.data.remote.CentralSharedSettingsValidator
import com.liferadio.sync.data.remote.SharedSettingsFetchResult
import com.liferadio.sync.data.local.DataCollector
import com.liferadio.sync.data.local.LocalActivityClassifier
import com.liferadio.sync.data.model.CentralHealthInfo
import com.liferadio.sync.data.remote.CentralHealthInfoClient
import com.liferadio.sync.data.remote.HealthInfoFetchResult
import com.liferadio.sync.service.SyncService
import com.liferadio.sync.service.LocationTrackingService
import com.liferadio.sync.service.CentralSyncCoordinator
import com.liferadio.sync.service.CentralSyncLoopResult
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

data class UiState(
    // 本机信息
    val hostname: String = Build.MODEL ?: "android-device",
    // 连接状态
    val centralReachable: Boolean? = null,  // null=未检测, true=可达, false=不可达
    val centralLastCheckedAt: Long? = null,
    val centralHealthMessage: String = "",
    // 同步状态
    val syncStatus: SyncStatus = SyncStatus(),
    val pendingEvents: Int = 0,           // 中央送达口径的待同步数
    // 同步进行中的累计进度
    val syncCumulativeConfirmed: Int = 0,
    val syncCumulativeRemaining: Int = 0,
    val syncInProgress: Boolean = false,
    // 设置
    val syncMode: String = SettingsStore.SYNC_MODE_CENTRAL,
    val centralBaseUrl: String = "",
    val centralDeviceId: String = "",
    val centralDeviceName: String = "",
    val centralScope: String = "",
    val centralTokenConfigured: Boolean = false,
    val invitationPreview: InvitationPreview? = null,
    val enrollmentInProgress: Boolean = false,
    val enrollmentMessage: String = "",
    val centralLastStatus: String = "",
    val centralNextRetryAt: Long = 0L,
    val sharedDayStartHour: Int = 0,
    val sharedSettingsLoadedFromCentral: Boolean = false,
    val sharedSettings: CentralSharedSettings? = null,
    val sharedSettingsOffline: Boolean = false,
    val sharedSettingsSaving: Boolean = false,
    val syncIntervalMinutes: Int = 10,
    val usageStatsPermissionGranted: Boolean = false,
    val locationTrackingEnabled: Boolean = false,
    val locationPermissionGranted: Boolean = false,
    val locationServiceRunning: Boolean = false,
    val lastLocationDetectedAt: Long? = null,
    val lastLocation: StoredLocation? = null,
    val todayLocationSummary: TodayLocationSummary = TodayLocationSummary(),
    val todayLocationDetails: TodayLocationDetails = TodayLocationDetails(),
    val isLoadingTodayLocation: Boolean = false,
    // 刷新状态
    val isRefreshing: Boolean = false,
    // 今日数据统计
    val todayCollected: Int = 0,
    val todaySynced: Int = 0,
    val todayUsageSummary: TodayUsageSummary = TodayUsageSummary(),
    val isLoadingTodayUsage: Boolean = false,
    // 健康估算
    val healthInfo: CentralHealthInfo? = null,
    val healthInfoOffline: Boolean = false,
    val healthInfoError: String = "",
    val stepSamples: List<StepSampleDisplay> = emptyList(),
    val stepCounterAvailable: Boolean = false,
    val activityRecognitionGranted: Boolean = false,
    val localActivityIntervals: List<LocalActivityInterval> = emptyList(),
    // 心愿
    val wishes: List<com.liferadio.sync.data.model.Wish> = emptyList(),
    val archivedWishes: List<com.liferadio.sync.data.model.Wish> = emptyList(),
    val wishesLoading: Boolean = false,
    val wishesError: String = "",
    val showWishCreateDialog: Boolean = false,
    /** null means the shared editor is creating; a value means it is editing that central record. */
    val editingWishId: String? = null,
    val editingWishCanEditReminder: Boolean = true,
    val wishCreateText: String = "",
    val wishCreateDuration: Int = 3,
    val wishCreateSending: Boolean = false,
    val wishCreateError: String = "",
    val showWishDeleteConfirm: String? = null,
    val showWishCompleteConfirm: String? = null,
    val wishCompletingId: String? = null,
    val wishDayAssessing: String? = null,
    val showArchivedWishes: Boolean = false,
    val wishesCacheOnly: Boolean = false,
    // 触发器
    val triggerCatalog: List<com.liferadio.sync.data.model.TriggerTypeCatalogItem> = emptyList(),
    val activeTriggers: List<com.liferadio.sync.data.model.EventTrigger> = emptyList(),
    /** wish_id → associated trigger record (enabled or disabled), at most one per wish */
    val wishTriggerMap: Map<String, com.liferadio.sync.data.model.EventTrigger> = emptyMap(),
    val triggerConflictWishIds: Set<String> = emptySet(),
    val triggersOffline: Boolean = false,
    val triggerCatalogsOffline: Boolean = false,
    // 触发器弹窗 state
    val showTriggerDialogForWish: String? = null,   // wish_id or null
    val triggerDialogType: String? = null,           // blacklist_usage_milestone etc
    val triggerDialogParams: Map<String, String> = emptyMap(),  // editable param fields
    val triggerDialogInterval: Int = 60,
    val triggerDialogSending: Boolean = false,
    val triggerDialogError: String = "",
    // 创建心愿时的提醒设置
    val wishCreateTriggerType: String? = null,       // null = no reminder
    val wishCreateTriggerParams: Map<String, String> = emptyMap(),
    val wishCreateTriggerInterval: Int = 60,
    // 时间线
    val timelineEvents: List<com.liferadio.sync.data.model.TimelineEvent> = emptyList(),
    val timelineLoading: Boolean = false,
    val timelineCacheOnly: Boolean = false,
    val eventBackground: com.liferadio.sync.data.model.EventBackgroundResponse? = null,
    val eventBackgroundOffline: Boolean = false
)

data class StepSampleDisplay(
    val hour: Int,                    // 0-23
    val label: String,                // "14:00"
    val steps: Int
)

data class LocalActivityInterval(
    val label: String,
    val startedAt: Long,
    val endedAt: Long
)

data class InvitationPreview(
    val centralBaseUrl: String,
    val permissionLabel: String,
    val expiresAt: String,
    val deviceName: String
)

data class AppUsageItem(
    val app: String,
    val title: String,
    val totalDuration: Long,  // 秒
    val eventCount: Int
)

data class TodayUsageSummary(
    val eventCount: Int = 0,
    val timeRange: String = "暂无数据",
    val apps: List<CollectedAppUsage> = emptyList()
)

data class CollectedAppUsage(
    val packageName: String,
    val appName: String,
    val durationSeconds: Long,
    val eventCount: Int
)

data class StoredLocation(
    val latitude: Double,
    val longitude: Double,
    val accuracyMeters: Float,
    val place: ResolvedPlace? = null
)

data class TodayLocationSummary(
    val eventCount: Int = 0,
    val sampleCount: Int = 0,
    val activeCount: Int = 0,
    val timeRange: String = "暂无有效数据"
)

data class LocationSegment(
    val id: String,
    val kind: String,
    val startedAt: Long,
    val observedUntil: Long,
    val durationSeconds: Int,
    val latitude: Double,
    val longitude: Double,
    val accuracyMeters: Float,
    val placeLabel: String?,
    val isActive: Boolean
)

data class LocationSample(
    val observedAt: Long,
    val latitude: Double,
    val longitude: Double,
    val accuracyMeters: Float,
    val provider: String,
    val motionWindowStartedAt: Long,
    val accelerometerAvailable: Boolean,
    val accelerometerSampleCount: Int,
    val motionTriggerCount: Int,
    val motionThresholdMetersPerSecondSquared: Float,
    val peakMotionDeltaMetersPerSecondSquared: Float
)

data class TodayLocationDetails(
    val syncEventCount: Int = 0,
    val segments: List<LocationSegment> = emptyList(),
    val samples: List<LocationSample> = emptyList()
)

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private var lastHealthInfoRefreshElapsed = Long.MIN_VALUE

    private val _uiState = MutableStateFlow(UiState())
    val uiState: StateFlow<UiState> = _uiState.asStateFlow()

    private val settings = SettingsStore(application)
    private val database = AppDatabase.getInstance(application)
    private val centralSyncCoordinator = CentralSyncCoordinator(database, settings)
    private val centralEnrollmentClient = CentralEnrollmentClient()
    private var pendingInvitation: CentralInvitation? = null

    private val moshi = Moshi.Builder()
        .add(KotlinJsonAdapterFactory())
        .build()

    private var _centralBaseUrl: String = settings.centralBaseUrl
    private var centralWishClient: com.liferadio.sync.data.remote.CentralWishClient? = createWishClient()
    private var _wishCreateRequestId: String? = null  // retained for idempotent retries

    private fun createWishClient(): com.liferadio.sync.data.remote.CentralWishClient? {
        val url = settings.centralBaseUrl
        return if (url.isBlank()) null
        else com.liferadio.sync.data.remote.CentralWishClient(
            baseUrl = url,
            tokenProvider = { settings.getCentralToken() }
        )
    }

    /** Ensure the client is fresh after base URL change or new binding. */
    private fun ensureWishClient(): com.liferadio.sync.data.remote.CentralWishClient? {
        val baseUrlChanged = _centralBaseUrl != settings.centralBaseUrl
        if (baseUrlChanged) {
            settings.clearWishTimelineCaches()
            _uiState.update {
                it.copy(
                    wishes = emptyList(), archivedWishes = emptyList(), wishesCacheOnly = false,
                    timelineEvents = emptyList(), timelineCacheOnly = false
                )
            }
        }
        if (baseUrlChanged || (centralWishClient == null && settings.centralBaseUrl.isNotBlank())) {
            _centralBaseUrl = settings.centralBaseUrl
            centralWishClient = createWishClient()
            _wishCreateRequestId = null
        }
        return centralWishClient
    }

    private var centralTriggerClient: com.liferadio.sync.data.remote.CentralTriggerClient? = createTriggerClient()
    private var _triggerBaseUrl: String = settings.centralBaseUrl
    private var _triggerCreateRequestId: String? = null

    private fun createTriggerClient(): com.liferadio.sync.data.remote.CentralTriggerClient? {
        val url = settings.centralBaseUrl
        return if (url.isBlank()) null
        else com.liferadio.sync.data.remote.CentralTriggerClient(
            baseUrl = url,
            tokenProvider = { settings.getCentralToken() }
        )
    }

    private fun ensureTriggerClient(): com.liferadio.sync.data.remote.CentralTriggerClient? {
        val baseUrlChanged = _triggerBaseUrl != settings.centralBaseUrl
        if (baseUrlChanged || (centralTriggerClient == null && settings.centralBaseUrl.isNotBlank())) {
            _triggerBaseUrl = settings.centralBaseUrl
            centralTriggerClient = createTriggerClient()
            _triggerCreateRequestId = null
            settings.clearTriggerCaches()
            _uiState.update {
                it.copy(
                    triggerCatalog = emptyList(), activeTriggers = emptyList(),
                    wishTriggerMap = emptyMap(), triggerConflictWishIds = emptySet(),
                    triggersOffline = false, triggerCatalogsOffline = false
                )
            }
        }
        return centralTriggerClient
    }

    init {
        // 加载持久化设置
        _uiState.update {
            it.copy(
                syncMode = settings.syncMode,
                centralBaseUrl = settings.centralBaseUrl,
                centralDeviceId = settings.deviceId,
                centralDeviceName = settings.centralDeviceName,
                centralScope = settings.centralScope,
                centralTokenConfigured = settings.isCentralTokenConfigured,
                centralLastStatus = settings.centralLastStatus,
                centralNextRetryAt = settings.centralNextRetryAt,
                sharedDayStartHour = settings.getSharedSettingsCache().dayStartHour,
                sharedSettingsLoadedFromCentral = settings.getSharedSettingsCache().hasCentralValue,
                syncIntervalMinutes = settings.syncIntervalMinutes,
                locationTrackingEnabled = settings.isLocationTrackingEnabled
            )
        }

        // 监听中央送达口径的待同步数据数量
        viewModelScope.launch {
            database.dataEventDao().getCentralPendingCount(
                CentralSyncCoordinator.CENTRAL_TARGET_ID
            ).collect { count ->
                _uiState.update { it.copy(pendingEvents = count) }
            }
        }

        // 监听今日收集/已同步数量
        viewModelScope.launch {
            val todayStart = getTodayStartMillis()
            database.dataEventDao().getTodayCount(todayStart).collect { total ->
                _uiState.update { it.copy(todayCollected = total) }
            }
        }
        viewModelScope.launch {
            val todayStart = getTodayStartMillis()
            database.dataEventDao().getTodaySyncedCount(todayStart).collect { synced ->
                _uiState.update { it.copy(todaySynced = synced) }
            }
        }

        resumeLocationTrackingIfEnabled()

        // 初始刷新
        refreshStatus()
        viewModelScope.launch { refreshSharedSettings() }
        refreshNativeCollectionStatus()
        refreshHealthInfo()
        refreshStepCounter()
        refreshWishes()
        refreshTriggersAndCatalog()
        viewModelScope.launch {
            while (isActive) {
                refreshLocationStatusInternal()
                refreshHealthInfo()
                delay(15_000L)
            }
        }
    }

    private fun getTodayStartMillis(): Long {
        val cal = java.util.Calendar.getInstance()
        cal.set(java.util.Calendar.HOUR_OF_DAY, 0)
        cal.set(java.util.Calendar.MINUTE, 0)
        cal.set(java.util.Calendar.SECOND, 0)
        cal.set(java.util.Calendar.MILLISECOND, 0)
        return cal.timeInMillis
    }

    fun loadTodayUsageSummary() {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoadingTodayUsage = true) }
            val summary = withContext(kotlinx.coroutines.Dispatchers.IO) {
                val zoneId = ZoneId.systemDefault()
                val today = LocalDate.now(zoneId)
                val start = today.atStartOfDay(zoneId).toInstant().toString()
                val end = today.plusDays(1).atStartOfDay(zoneId).toInstant().toString()
                val events = database.dataEventDao().getTodayAppUsageEvents(start, end)
                val appDataAdapter = moshi.adapter(Map::class.java)
                val identifiedEvents = events.map { event ->
                    val payload = runCatching {
                        appDataAdapter.fromJson(event.dataJson)
                    }.getOrNull()
                    UsageEventDisplayParser.identity(payload) to event
                }
                val grouped = identifiedEvents.groupBy { it.first.packageName }.map { (packageName, entries) ->
                    val identity = entries.first().first
                    val appEvents = entries.map { it.second }
                    CollectedAppUsage(
                        packageName = packageName,
                        appName = identity.displayName,
                        durationSeconds = appEvents.sumOf { it.duration.toLong() },
                        eventCount = appEvents.size
                    )
                }.sortedByDescending { it.durationSeconds }
                val eventTimes = events.mapNotNull { event ->
                    runCatching { Instant.parse(event.timestamp) }.getOrNull()
                }
                val formatter = DateTimeFormatter.ofPattern("HH:mm").withZone(zoneId)
                TodayUsageSummary(
                    eventCount = events.size,
                    timeRange = if (eventTimes.isEmpty()) "暂无数据" else {
                        "${formatter.format(eventTimes.min())} - ${formatter.format(eventTimes.max())}"
                    },
                    apps = grouped
                )
            }
            _uiState.update {
                it.copy(todayUsageSummary = summary, isLoadingTodayUsage = false)
            }
        }
    }

    fun refreshLocationStatus() {
        viewModelScope.launch { refreshLocationStatusInternal() }
    }

    private suspend fun refreshLocationStatusInternal() {
        val application = getApplication<Application>()
        val hasPermission = ContextCompat.checkSelfPermission(
            application,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED || ContextCompat.checkSelfPermission(
            application,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        val lastLocation = settings.getLastLocation()?.let { (latitude, longitude, accuracy) ->
            StoredLocation(latitude, longitude, accuracy, settings.getLastResolvedPlace())
        }
        val summary = withContext(kotlinx.coroutines.Dispatchers.IO) {
            val zoneId = ZoneId.systemDefault()
            val today = LocalDate.now(zoneId)
            val start = today.atStartOfDay(zoneId).toInstant().toString()
            val end = today.plusDays(1).atStartOfDay(zoneId).toInstant().toString()
            val startMillis = today.atStartOfDay(zoneId).toInstant().toEpochMilli()
            val endMillis = today.plusDays(1).atStartOfDay(zoneId).toInstant().toEpochMilli()
            val events = database.dataEventDao().getTodayLocationEvents(start, end)
            val samples = database.locationSampleDao().getSamplesBetween(startMillis, endMillis)
            val formatter = DateTimeFormatter.ofPattern("HH:mm").withZone(zoneId)
            val times = if (samples.isNotEmpty()) {
                samples.map { sample -> Instant.ofEpochMilli(sample.observedAt) }
            } else {
                events.mapNotNull { event -> runCatching { Instant.parse(event.timestamp) }.getOrNull() }
            }
            TodayLocationSummary(
                eventCount = events.size,
                sampleCount = samples.size,
                activeCount = events.count { event -> locationPayload(event.dataJson)["is_active"] == true },
                timeRange = if (times.isEmpty()) "暂无有效数据" else {
                    "${formatter.format(times.min())} - ${formatter.format(times.max())}"
                }
            )
        }
        _uiState.update {
            it.copy(
                locationTrackingEnabled = settings.isLocationTrackingEnabled,
                locationPermissionGranted = hasPermission,
                locationServiceRunning = LocationTrackingService.isRunning,
                lastLocationDetectedAt = settings.lastLocationDetectedAt.takeIf { timestamp -> timestamp > 0L },
                lastLocation = lastLocation,
                todayLocationSummary = summary
            )
        }
    }

    fun loadTodayLocationDetails() {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoadingTodayLocation = true) }
            val details = withContext(kotlinx.coroutines.Dispatchers.IO) {
                val zoneId = ZoneId.systemDefault()
                val today = LocalDate.now(zoneId)
                val start = today.atStartOfDay(zoneId).toInstant()
                val end = today.plusDays(1).atStartOfDay(zoneId).toInstant()
                val events = database.dataEventDao().getTodayLocationEvents(start.toString(), end.toString())
                val segments = events.mapNotNull { event ->
                    val startedAt = runCatching { Instant.parse(event.timestamp).toEpochMilli() }.getOrNull()
                        ?: return@mapNotNull null
                    val payload = locationPayload(event.dataJson)
                    if (payload["kind"] !in setOf("sample", "stay")) {
                        return@mapNotNull null
                    }
                    val observedUntil = (payload["observed_until"] as? String)
                        ?.let { value -> runCatching { Instant.parse(value).toEpochMilli() }.getOrNull() }
                        ?: startedAt + event.duration * 1000L
                    val isActive = payload["is_active"] as? Boolean ?: false
                    val latitude = payload.number("current_latitude").takeIf { isActive }
                        ?: payload.number("latitude") ?: return@mapNotNull null
                    val longitude = payload.number("current_longitude").takeIf { isActive }
                        ?: payload.number("longitude") ?: return@mapNotNull null
                    val place = payload["place"] as? Map<*, *>
                    LocationSegment(
                        id = event.id,
                        kind = payload["kind"]?.toString().orEmpty().ifBlank { "sample" },
                        startedAt = startedAt,
                        observedUntil = observedUntil,
                        durationSeconds = event.duration,
                        latitude = latitude,
                        longitude = longitude,
                        accuracyMeters = (payload.number("current_accuracy_m").takeIf { isActive }
                            ?: payload.number("accuracy_m") ?: 0.0).toFloat(),
                        placeLabel = place?.get("display_label")?.toString(),
                        isActive = isActive
                    )
                }
                val samples = database.locationSampleDao()
                    .getSamplesBetween(start.toEpochMilli(), end.toEpochMilli())
                    .map { sample ->
                        LocationSample(
                            observedAt = sample.observedAt,
                            latitude = sample.latitude,
                            longitude = sample.longitude,
                            accuracyMeters = sample.accuracyMeters,
                            provider = sample.provider,
                            motionWindowStartedAt = sample.motionWindowStartedAt,
                            accelerometerAvailable = sample.accelerometerAvailable,
                            accelerometerSampleCount = sample.accelerometerSampleCount,
                            motionTriggerCount = sample.motionTriggerCount,
                            motionThresholdMetersPerSecondSquared = sample.motionThresholdMetersPerSecondSquared,
                            peakMotionDeltaMetersPerSecondSquared = sample.peakMotionDeltaMetersPerSecondSquared
                        )
                    }
                TodayLocationDetails(syncEventCount = events.size, segments = segments, samples = samples)
            }
            _uiState.update {
                it.copy(todayLocationDetails = details, isLoadingTodayLocation = false)
            }
        }
    }

    private fun locationPayload(dataJson: String): Map<*, *> =
        runCatching { moshi.adapter(Map::class.java).fromJson(dataJson) as? Map<*, *> }
            .getOrNull() ?: emptyMap<String, Any>()

    private fun Map<*, *>.number(key: String): Double? = (get(key) as? Number)?.toDouble()

    fun setLocationTrackingEnabled(enabled: Boolean) {
        val application = getApplication<Application>()
        val hasPermission = ContextCompat.checkSelfPermission(
            application,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED || ContextCompat.checkSelfPermission(
            application,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        if (enabled && !hasPermission) {
            refreshLocationStatus()
            return
        }
        settings.isLocationTrackingEnabled = enabled
        if (enabled) {
            LocationTrackingService.start(application)
        } else {
            LocationTrackingService.stop(application)
        }
        refreshLocationStatus()
    }

    private fun resumeLocationTrackingIfEnabled() {
        val application = getApplication<Application>()
        val hasPermission = ContextCompat.checkSelfPermission(
            application,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED || ContextCompat.checkSelfPermission(
            application,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        if (settings.isLocationTrackingEnabled && hasPermission && !LocationTrackingService.isRunning) {
            LocationTrackingService.start(application)
        }
    }

    fun refreshStatus() {
        _uiState.update {
            it.copy(
                isRefreshing = false,
                centralTokenConfigured = settings.isCentralTokenConfigured,
                centralLastStatus = settings.centralLastStatus,
                centralNextRetryAt = settings.centralNextRetryAt
            )
        }
    }

    private suspend fun refreshSharedSettings() {
        if (settings.centralBaseUrl.isBlank() || !settings.isCentralTokenConfigured) return
        val result = runCatching {
            CentralSharedSettingsClient(settings.centralBaseUrl, settings::getCentralToken).fetch()
        }.getOrNull()
        if (result is SharedSettingsFetchResult.Success) {
            val fullJson = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
                .adapter(CentralSharedSettings::class.java).toJson(result.settings)
            val cache = CentralSharedSettingsValidator.toCache(result.settings, System.currentTimeMillis(), fullJson)
            settings.saveSharedSettingsCache(cache)
            _uiState.update {
                it.copy(sharedDayStartHour = cache.dayStartHour, sharedSettingsLoadedFromCentral = true,
                    sharedSettings = result.settings, sharedSettingsOffline = false,
                    centralReachable = true, centralLastCheckedAt = System.currentTimeMillis(),
                    centralHealthMessage = "最近一次中央读取成功")
            }
        } else {
            val cached = settings.getSharedSettingsCache().fullResponseJson.takeIf { it.isNotBlank() }?.let {
                runCatching { Moshi.Builder().add(KotlinJsonAdapterFactory()).build().adapter(CentralSharedSettings::class.java).fromJson(it) }.getOrNull()
            }
            _uiState.update {
                it.copy(
                    sharedSettings = cached ?: it.sharedSettings,
                    sharedSettingsOffline = cached != null,
                    centralReachable = false,
                    centralLastCheckedAt = System.currentTimeMillis(),
                    centralHealthMessage = "最近一次中央读取失败，已保留本机数据"
                )
            }
        }
    }

    fun updateEventSchedule(patch: com.liferadio.sync.data.remote.SharedSettingsPatch) {
        if (_uiState.value.sharedSettingsOffline || _uiState.value.sharedSettingsSaving) return
        viewModelScope.launch {
            _uiState.update { it.copy(sharedSettingsSaving = true) }
            val result = withContext(kotlinx.coroutines.Dispatchers.IO) {
                CentralSharedSettingsClient(settings.centralBaseUrl, settings::getCentralToken).update(patch)
            }
            if (result is SharedSettingsFetchResult.Success) {
                val json = moshi.adapter(CentralSharedSettings::class.java).toJson(result.settings)
                val cache = CentralSharedSettingsValidator.toCache(result.settings, System.currentTimeMillis(), json)
                settings.saveSharedSettingsCache(cache)
                _uiState.update { it.copy(sharedSettings = result.settings, sharedDayStartHour = result.settings.day_start_hour,
                    sharedSettingsLoadedFromCentral = true, sharedSettingsOffline = false, sharedSettingsSaving = false) }
            } else _uiState.update { it.copy(sharedSettingsSaving = false, sharedSettingsOffline = true) }
        }
    }

    fun triggerSync() {
        viewModelScope.launch {
            refreshSharedSettings()
            _uiState.update {
                it.copy(
                    syncStatus = it.syncStatus.copy(
                        isSyncing = true,
                        errorMessage = null
                    ),
                    syncInProgress = true,
                    syncCumulativeConfirmed = 0,
                    syncCumulativeRemaining = 0
                )
            }

            collectBeforeManualSync()
            val result = centralSyncCoordinator.syncLoop(
                force = true,
                maxBatchesPerRun = 20,
                onProgress = { progress ->
                    if (progress is CentralSyncLoopResult.InProgress) {
                        _uiState.update {
                            it.copy(
                                syncCumulativeConfirmed = progress.cumulativeConfirmed,
                                syncCumulativeRemaining = progress.remainingQueue,
                                syncStatus = it.syncStatus.copy(
                                    lastSyncResult = "已确认 ${progress.cumulativeConfirmed} 条，队列剩余 ${progress.remainingQueue} 条"
                                )
                            )
                        }
                    }
                }
            )
            val message = centralLoopOutcomeMessage(result)
            val isError = result is CentralSyncLoopResult.AuthFailed ||
                result is CentralSyncLoopResult.Failed
            val pendingNow = database.dataEventDao().getCentralPendingCountBlocking(
                CentralSyncCoordinator.CENTRAL_TARGET_ID
            )
            val syncReachable = result is CentralSyncLoopResult.Completed ||
                result is CentralSyncLoopResult.Stopped && result.cumulativeConfirmed > 0
            _uiState.update {
                it.copy(
                    syncStatus = SyncStatus(
                        isSyncing = false,
                        lastSyncTime = System.currentTimeMillis(),
                        pendingEvents = pendingNow,
                        errorMessage = message.takeIf { isError },
                        lastSyncResult = message
                    ),
                    syncInProgress = false,
                    centralTokenConfigured = settings.isCentralTokenConfigured,
                    centralLastStatus = settings.centralLastStatus,
                    centralNextRetryAt = settings.centralNextRetryAt,
                    centralReachable = syncReachable,
                    centralLastCheckedAt = System.currentTimeMillis(),
                    centralHealthMessage = if (syncReachable) "最近一次同步成功" else message
                )
            }
            if (result is CentralSyncLoopResult.Completed ||
                result is CentralSyncLoopResult.Stopped && result.cumulativeConfirmed > 0
            ) {
                refreshHealthInfo(force = true)
            }
        }
    }

    fun updateSyncInterval(minutes: Int) {
        settings.syncIntervalMinutes = minutes
        _uiState.update { it.copy(syncIntervalMinutes = minutes) }
    }

    fun updateSyncMode(@Suppress("UNUSED_PARAMETER") mode: String) {
        settings.syncMode = SettingsStore.SYNC_MODE_CENTRAL
        _uiState.update {
            it.copy(
                syncMode = mode,
                centralTokenConfigured = settings.isCentralTokenConfigured,
                centralLastStatus = settings.centralLastStatus,
                centralNextRetryAt = settings.centralNextRetryAt
            )
        }
        refreshStatus()
    }

    fun previewCentralInvitation(code: String) {
        val parsed = runCatching { CentralInvitationParser.parse(code) }
        parsed.onSuccess { invitation ->
            pendingInvitation = invitation
            val deviceName = (Build.MANUFACTURER + " " + Build.MODEL).trim().take(100)
                .ifBlank { "Life Link Android" }
            _uiState.update {
                it.copy(
                    invitationPreview = InvitationPreview(
                        centralBaseUrl = invitation.centralBaseUrl,
                        permissionLabel = invitation.permissionLabel,
                        expiresAt = invitation.expiresAt,
                        deviceName = deviceName
                    ),
                    enrollmentMessage = ""
                )
            }
        }.onFailure { error ->
            pendingInvitation = null
            _uiState.update {
                it.copy(invitationPreview = null, enrollmentMessage = error.message ?: "邀请码无效")
            }
        }
    }

    fun cancelCentralInvitation() {
        pendingInvitation = null
        _uiState.update { it.copy(invitationPreview = null, enrollmentMessage = "") }
    }

    fun confirmCentralInvitation() {
        val invitation = pendingInvitation ?: return
        viewModelScope.launch {
            _uiState.update { it.copy(enrollmentInProgress = true, enrollmentMessage = "") }
            when (val result = centralEnrollmentClient.claim(invitation, settings.deviceId)) {
                is EnrollmentResult.Success -> {
                    val profile = result.profile
                    settings.saveCentralToken(profile.uploadToken)
                    settings.centralBaseUrl = CentralInvitationParser.normalizeBaseUrl(profile.centralBaseUrl)
                    settings.centralScope = invitation.scope
                    settings.centralIssuedAt = profile.issuedAt
                    settings.centralDeviceName = profile.device.displayName
                    settings.syncMode = SettingsStore.SYNC_MODE_CENTRAL
                    settings.centralLastStatus = "中央服务绑定成功"
                    pendingInvitation = null
                    clearInvitationFromClipboard()
                    _uiState.update {
                        it.copy(
                            centralBaseUrl = settings.centralBaseUrl,
                            centralDeviceName = settings.centralDeviceName,
                            centralScope = settings.centralScope,
                            centralTokenConfigured = true,
                            invitationPreview = null,
                            enrollmentInProgress = false,
                            enrollmentMessage = "绑定成功",
                            centralLastStatus = settings.centralLastStatus
                        )
                    }
                    refreshSharedSettings()
                    refreshWishes()
                }
                is EnrollmentResult.Failure -> {
                    _uiState.update {
                        it.copy(enrollmentInProgress = false, enrollmentMessage = result.message)
                    }
                    if (!result.retryable) pendingInvitation = null
                }
            }
        }
    }

    private fun clearInvitationFromClipboard() {
        val application = getApplication<Application>()
        val clipboard = application.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
            as android.content.ClipboardManager
        val current = clipboard.primaryClip?.getItemAt(0)?.coerceToText(application)?.toString().orEmpty()
        if (current.trim().startsWith("LR1.")) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) clipboard.clearPrimaryClip()
            else clipboard.setPrimaryClip(android.content.ClipData.newPlainText("", ""))
        }
    }

    fun updateCentralToken(token: String) {
        runCatching { settings.saveCentralToken(token) }
            .onSuccess {
                _uiState.update {
                    it.copy(
                        centralTokenConfigured = true,
                        centralLastStatus = "中央 Token 已安全保存",
                        centralNextRetryAt = 0L,
                        syncStatus = it.syncStatus.copy(errorMessage = null)
                    )
                }
                refreshWishes()
            }
            .onFailure {
                _uiState.update {
                    it.copy(syncStatus = it.syncStatus.copy(
                        errorMessage = "Token 至少需要 32 个非空白字符",
                        lastSyncResult = "Token 格式无效"
                    ))
                }
            }
    }

    fun clearCentralToken() {
        settings.clearCentralToken()
        _uiState.update {
            it.copy(
                centralTokenConfigured = false,
                centralLastStatus = "中央 Token 已清除",
                centralNextRetryAt = 0L,
                wishesCacheOnly = it.wishes.isNotEmpty() || it.archivedWishes.isNotEmpty(),
                timelineCacheOnly = it.timelineEvents.isNotEmpty()
            )
        }
    }

    private suspend fun collectBeforeManualSync() {
        try {
            val collector = com.liferadio.sync.data.local.DataCollector(getApplication())
            val nativeEvents = collector.collectNativeUsageEvents()
            if (nativeEvents.isNotEmpty()) {
                database.dataEventDao().insertAll(nativeEvents)
                android.util.Log.d("MainViewModel", "triggerSync: collected ${nativeEvents.size} native events")
            }
        } catch (e: Exception) {
            android.util.Log.e("MainViewModel", "triggerSync: collect failed", e)
        }
    }

    private fun centralLoopOutcomeMessage(outcome: CentralSyncLoopResult): String = when (outcome) {
        is CentralSyncLoopResult.InProgress ->
            "已确认 ${outcome.cumulativeConfirmed} 条，队列剩余 ${outcome.remainingQueue} 条"
        is CentralSyncLoopResult.Completed ->
            "同步完成：总计 ${outcome.totalConfirmed} 条（${outcome.batchCount} 批）"
        is CentralSyncLoopResult.Stopped ->
            "已确认 ${outcome.cumulativeConfirmed} 条，${outcome.reason}"
        is CentralSyncLoopResult.AuthFailed ->
            "中央认证失败（HTTP ${outcome.statusCode}），请检查设备 Token"
        is CentralSyncLoopResult.Failed -> {
            val detail = when (outcome.reason) {
                "central connection failed" ->
                    "无法连接 Life Link 中央服务。请确认手机网络、Tailscale 或内网穿透已连接，然后重试"
                else -> outcome.reason
            }
            "同步失败：$detail"
        }
    }

    fun refreshNativeCollectionStatus() {
        val collector = com.liferadio.sync.data.local.DataCollector(getApplication())
        _uiState.update { it.copy(usageStatsPermissionGranted = collector.hasUsageStatsPermission()) }
    }

    private fun refreshHealthInfo(force: Boolean = false) {
        val nowElapsed = android.os.SystemClock.elapsedRealtime()
        if (!force && lastHealthInfoRefreshElapsed != Long.MIN_VALUE &&
            nowElapsed - lastHealthInfoRefreshElapsed < 10 * 60 * 1000L) return
        lastHealthInfoRefreshElapsed = nowElapsed
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                val date = LocalDate.now(ZoneId.of("Asia/Shanghai")).toString()
                val result = if (settings.centralBaseUrl.isBlank() || !settings.isCentralTokenConfigured) {
                    HealthInfoFetchResult.Failure(null, "未绑定中央服务")
                } else runCatching {
                    CentralHealthInfoClient(settings.centralBaseUrl, settings::getCentralToken).fetch(date)
                }.getOrElse { HealthInfoFetchResult.Failure(null, "中央健康信息读取失败") }
                when (result) {
                    is HealthInfoFetchResult.Success -> {
                        settings.saveHealthInfoCache(result.rawResponse)
                        _uiState.update { it.copy(healthInfo = result.healthInfo, healthInfoOffline = false, healthInfoError = "") }
                    }
                    is HealthInfoFetchResult.Failure -> {
                        val cached = settings.getHealthInfoCache()?.let { cacheJson ->
                            runCatching {
                                CentralHealthInfoClient(settings.centralBaseUrl.ifBlank { "https://central.invalid" }, settings::getCentralToken)
                                    .parseValidated(cacheJson)
                            }.getOrNull()
                        }
                        _uiState.update {
                            if (cached != null) it.copy(healthInfo = cached, healthInfoOffline = true, healthInfoError = "")
                            else it.copy(healthInfo = null, healthInfoOffline = false, healthInfoError = result.reason)
                        }
                    }
                }
            }
        }
    }

    fun refreshHealth() {
        refreshHealthInfo(force = true)
        refreshStepCounter()
    }

    fun refreshLocalActivity() = refreshStepCounter()

    private fun refreshStepCounter() {
        viewModelScope.launch {
            val zone = ZoneId.of("Asia/Shanghai")
            val date = LocalDate.now(zone)
            val start = date.atStartOfDay(zone).toInstant().toEpochMilli()
            val end = date.plusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()
            val (samples, locations) = withContext(kotlinx.coroutines.Dispatchers.IO) {
                database.stepObservationDao().getBetween(start, end) to
                    database.locationSampleDao().getSamplesBetween(start - LocalActivityClassifier.BUCKET_MILLIS, end)
            }
            val intervals = LocalActivityClassifier.classifyDay(
                steps = samples.map { LocalActivityClassifier.StepObservation(it.observedAt, it.counterValue, it.counterSessionId) },
                locations = locations.map { LocalActivityClassifier.LocationObservation(it.observedAt, it.latitude, it.longitude, it.accuracyMeters) },
                dayStart = start,
                dayEnd = end
            ).map { LocalActivityInterval(it.label.displayName, it.startedAt, it.endedAt) }
            val permissionGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q ||
                ContextCompat.checkSelfPermission(getApplication(), Manifest.permission.ACTIVITY_RECOGNITION) == PackageManager.PERMISSION_GRANTED
            val sensorAvailable = (getApplication<Application>().getSystemService(Application.SENSOR_SERVICE) as android.hardware.SensorManager)
                .getDefaultSensor(android.hardware.Sensor.TYPE_STEP_COUNTER) != null
            _uiState.update {
                it.copy(
                    stepCounterAvailable = sensorAvailable,
                    activityRecognitionGranted = permissionGranted,
                    stepSamples = samples.map { sample -> StepSampleDisplay(
                        hour = Instant.ofEpochMilli(sample.observedAt).atZone(zone).hour,
                        label = Instant.ofEpochMilli(sample.observedAt).atZone(zone).toLocalTime().toString(),
                        steps = sample.counterValue.coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
                    ) }.reversed(),
                    localActivityIntervals = intervals
                )
            }
        }
    }

    // ==================== 心愿 ====================

    fun refreshWishes() {
        val client = ensureWishClient() ?: run {
            loadWishesFromCache()
            return
        }
        _uiState.update { it.copy(wishesLoading = true, wishesError = "") }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                when (val result = client.listWishes(includeArchived = true)) {
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        val response = result.data as com.liferadio.sync.data.model.WishListResponse
                        val listJson = moshi.adapter(com.liferadio.sync.data.model.WishListResponse::class.java).toJson(response)
                        settings.saveWishCache(listJson, System.currentTimeMillis())
                        applyWishList(response, fromCache = false)
                    }
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        _uiState.update { it.copy(wishesLoading = false, wishesError = result.reason) }
                        loadWishesFromCache()
                    }
                }
            }
        }
    }

    private fun loadWishesFromCache() {
        val json = settings.wishCacheJson ?: run {
            _uiState.update { it.copy(wishesCacheOnly = false) }
            return
        }
        val response = runCatching {
            moshi.adapter(com.liferadio.sync.data.model.WishListResponse::class.java).fromJson(json)
        }.getOrNull() ?: run {
            _uiState.update { it.copy(wishesCacheOnly = false) }
            return
        }
        applyWishList(response, fromCache = true)
    }

    private fun applyWishList(response: com.liferadio.sync.data.model.WishListResponse, fromCache: Boolean) {
        val active = response.wishes.filter { it.status == "active" }
        val archived = response.wishes.filter { it.status != "active" }
        _uiState.update {
            it.copy(
                wishes = active, archivedWishes = archived,
                wishesLoading = false, wishesError = "",
                wishesCacheOnly = fromCache
            )
        }
    }

    fun refreshArchivedWishes() {
        val client = ensureWishClient() ?: run {
            loadWishesFromCache()
            return
        }
        _uiState.update { it.copy(wishesLoading = true) }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                when (val result = client.listWishes(includeArchived = true)) {
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        val response = result.data as com.liferadio.sync.data.model.WishListResponse
                        settings.saveWishCache(
                            moshi.adapter(com.liferadio.sync.data.model.WishListResponse::class.java).toJson(response),
                            System.currentTimeMillis()
                        )
                        applyWishList(response, fromCache = false)
                    }
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        _uiState.update { it.copy(wishesLoading = false) }
                        loadWishesFromCache()
                    }
                }
            }
        }
    }

    fun assessWishDay(wishId: String, businessDate: String, evaluation: String) {
        val client = ensureWishClient() ?: return
        val key = "$wishId:$businessDate"
        _uiState.update { it.copy(wishDayAssessing = key) }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                val assessment = com.liferadio.sync.data.model.WishDayAssessment(evaluation = evaluation)
                when (val result = client.assessWishDay(wishId, businessDate, assessment)) {
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        _uiState.update { it.copy(wishDayAssessing = null) }
                        refreshWishes()
                    }
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        _uiState.update { it.copy(wishDayAssessing = null, wishesError = result.reason) }
                    }
                }
            }
        }
    }

    // --- Timeline ---

    fun refreshTimeline() {
        val client = ensureWishClient() ?: run {
            loadTimelineFromCache()
            return
        }
        _uiState.update { it.copy(timelineLoading = true) }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                val now = Instant.now()
                val dayStartHour = _uiState.value.sharedDayStartHour
                val window = timelineDayWindow(dayStartHour, now)
                when (val result = client.listTimeline(
                    window.fromInclusive.toString(),
                    window.toExclusive.toString(),
                    null
                )) {
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        val resp = result.data as com.liferadio.sync.data.model.TimelineEventListResponse
                        val json = moshi.adapter(com.liferadio.sync.data.model.TimelineEventListResponse::class.java).toJson(resp)
                        settings.saveTimelineCache(json, System.currentTimeMillis())
                        _uiState.update {
                            it.copy(
                                timelineEvents = todayAndYesterdayTimelineEvents(resp.events, dayStartHour, now),
                                timelineLoading = false,
                                timelineCacheOnly = false
                            )
                        }
                    }
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        _uiState.update { it.copy(timelineLoading = false) }
                        loadTimelineFromCache()
                    }
                }
            }
        }
    }

    private fun refreshEventBackground() {
        val client = ensureWishClient() ?: return
        viewModelScope.launch {
            val dayStartHour = _uiState.value.sharedSettings?.day_start_hour
                ?: settings.getSharedSettingsCache().dayStartHour
            val businessDate = com.liferadio.sync.data.model.EventBusinessDay.at(dayStartHour, Instant.now()).toString()
            when (val result = withContext(kotlinx.coroutines.Dispatchers.IO) { client.getEventBackground(businessDate) }) {
                is com.liferadio.sync.data.remote.WishResult.Success -> {
                    val response = result.data as com.liferadio.sync.data.model.EventBackgroundResponse
                    settings.saveEventBackgroundCache(moshi.adapter(com.liferadio.sync.data.model.EventBackgroundResponse::class.java).toJson(response), System.currentTimeMillis())
                    _uiState.update { it.copy(eventBackground = response, eventBackgroundOffline = false) }
                }
                is com.liferadio.sync.data.remote.WishResult.Failure -> settings.getEventBackgroundCache()?.let { json ->
                    runCatching { moshi.adapter(com.liferadio.sync.data.model.EventBackgroundResponse::class.java).fromJson(json) }.getOrNull()?.let { cached ->
                        _uiState.update { it.copy(eventBackground = cached, eventBackgroundOffline = true) }
                    }
                }
            }
        }
    }

    private fun loadTimelineFromCache() {
        val json = settings.timelineCacheJson ?: return
        val resp = runCatching {
            moshi.adapter(com.liferadio.sync.data.model.TimelineEventListResponse::class.java).fromJson(json)
        }.getOrNull() ?: return
        _uiState.update {
            it.copy(
                timelineEvents = todayAndYesterdayTimelineEvents(
                    resp.events,
                    _uiState.value.sharedDayStartHour,
                    Instant.now()
                ),
                timelineLoading = false,
                timelineCacheOnly = true
            )
        }
    }

    fun showHistoryAndTimeline() {
        refreshArchivedWishes()
        _uiState.update { it.copy(showArchivedWishes = true) }
    }

    fun dismissHistory() {
        _uiState.update { it.copy(showArchivedWishes = false) }
    }

    fun showCreateWishDialog() {
        _wishCreateRequestId = null
        _triggerCreateRequestId = null
        _uiState.update {
            it.copy(
                showWishCreateDialog = true, wishCreateText = "", wishCreateDuration = 3,
                editingWishId = null, editingWishCanEditReminder = true,
                wishCreateError = "", wishCreateTriggerType = null,
                wishCreateTriggerParams = emptyMap(), wishCreateTriggerInterval = 60
            )
        }
    }

    fun dismissWishDialog() {
        _wishCreateRequestId = null
        _triggerCreateRequestId = null
        _uiState.update { it.copy(showWishCreateDialog = false, editingWishId = null, editingWishCanEditReminder = true, wishCreateError = "") }
    }

    fun showWishEditor(wishId: String) {
        val state = _uiState.value
        if (!WishEditorPolicy.canWrite(state.wishesCacheOnly)) return
        val wish = (state.wishes + state.archivedWishes).firstOrNull { it.wishId == wishId } ?: return
        if (wishId in state.triggerConflictWishIds) {
            _uiState.update { it.copy(wishesError = "这条心愿存在多个提醒记录，请先在其他客户端处理") }
            return
        }
        val existing = state.wishTriggerMap[wishId]
        _uiState.update {
            it.copy(
                showWishCreateDialog = true, editingWishId = wishId,
                editingWishCanEditReminder = WishEditorPolicy.canEditReminder(wish.status),
                wishCreateText = wish.text, wishCreateDuration = wish.durationDays,
                wishCreateSending = false, wishCreateError = "",
                wishCreateTriggerType = existing?.takeIf { trigger -> trigger.enabled }?.triggerType,
                wishCreateTriggerParams = existing?.parameters?.mapValues { entry -> entry.value.toString() } ?: emptyMap(),
                wishCreateTriggerInterval = existing?.intervalMinutes ?: 60
            )
        }
    }

    fun dismissWishDelete() {
        _uiState.update { it.copy(showWishDeleteConfirm = null) }
    }

    fun showWishDelete(wishId: String) {
        if (WishEditorPolicy.canWrite(_uiState.value.wishesCacheOnly)) _uiState.update { it.copy(showWishDeleteConfirm = wishId) }
    }

    fun showWishComplete(wishId: String) {
        if (WishEditorPolicy.canWrite(_uiState.value.wishesCacheOnly)) {
            _uiState.update { it.copy(showWishCompleteConfirm = wishId, wishesError = "") }
        }
    }

    fun dismissWishComplete() {
        _uiState.update { it.copy(showWishCompleteConfirm = null) }
    }

    fun completeWish(wishId: String) {
        if (!WishEditorPolicy.canWrite(_uiState.value.wishesCacheOnly)) return
        val client = ensureWishClient() ?: return
        _uiState.update { it.copy(wishCompletingId = wishId, wishesError = "") }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                when (val result = client.completeWish(wishId)) {
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        _uiState.update { it.copy(showWishCompleteConfirm = null, wishCompletingId = null) }
                        refreshWishes()
                        refreshTriggersAndCatalog()
                        refreshTimeline()
                    }
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        _uiState.update { it.copy(wishCompletingId = null, wishesError = result.reason) }
                    }
                }
            }
        }
    }

    fun deleteWish(wishId: String) {
        if (!WishEditorPolicy.canWrite(_uiState.value.wishesCacheOnly)) return
        val client = ensureWishClient() ?: return
        _uiState.update { it.copy(showWishDeleteConfirm = null, wishCreateSending = true) }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                when (val result = client.deleteWish(wishId)) {
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        // Do not allow an old read-only cache to resurrect a successfully deleted record
                        // when the following central refresh is temporarily unavailable.
                        removeDeletedWishFromLocalReadModels(wishId)
                        _uiState.update { it.copy(showWishCreateDialog = false, editingWishId = null, editingWishCanEditReminder = true, wishCreateSending = false) }
                        refreshWishes()
                        refreshTriggersAndCatalog()
                    }
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        _uiState.update { it.copy(wishCreateSending = false, wishCreateError = result.reason, wishesError = result.reason) }
                    }
                }
            }
        }
    }

    private fun removeDeletedWishFromLocalReadModels(wishId: String) {
        _uiState.update {
            it.copy(
                wishes = it.wishes.filterNot { wish -> wish.wishId == wishId },
                archivedWishes = it.archivedWishes.filterNot { wish -> wish.wishId == wishId },
                wishTriggerMap = it.wishTriggerMap - wishId,
                triggerConflictWishIds = it.triggerConflictWishIds - wishId
            )
        }
        val wishAdapter = moshi.adapter(com.liferadio.sync.data.model.WishListResponse::class.java)
        settings.wishCacheJson?.let { json ->
            runCatching { wishAdapter.fromJson(json) }.getOrNull()?.let { cached ->
                settings.saveWishCache(wishAdapter.toJson(cached.copy(
                    wishes = cached.wishes.filterNot { wish -> wish.wishId == wishId }
                )), System.currentTimeMillis())
            } ?: settings.clearWishTimelineCaches()
        }
        val triggersAdapter = moshi.adapter(com.liferadio.sync.data.model.EventTriggerListResponse::class.java)
        settings.triggersJson?.let { json ->
            runCatching { triggersAdapter.fromJson(json) }.getOrNull()?.let { cached ->
                settings.saveTriggers(triggersAdapter.toJson(cached.copy(
                    triggers = cached.triggers.filterNot { trigger -> trigger.wishId == wishId }
                )), System.currentTimeMillis())
            } ?: settings.clearTriggerCaches()
        }
    }

    fun updateWishCreateText(text: String) {
        val normalized = text.take(30)
        if (normalized != _uiState.value.wishCreateText) _wishCreateRequestId = null
        _uiState.update { it.copy(wishCreateText = normalized) }
    }

    fun setWishCreateDuration(days: Int) {
        if (days != _uiState.value.wishCreateDuration) _wishCreateRequestId = null
        _uiState.update { it.copy(wishCreateDuration = days) }
    }

    // ==================== 触发器 ====================

    fun refreshTriggersAndCatalog() {
        val client = ensureTriggerClient() ?: run {
            loadTriggersFromCache()
            return
        }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                // Fetch catalog
                when (val result = client.listTriggerTypes()) {
                    is com.liferadio.sync.data.remote.TriggerResult.Success -> {
                        val catalog = result.data as com.liferadio.sync.data.model.TriggerTypeCatalogResponse
                        val json = moshi.adapter(com.liferadio.sync.data.model.TriggerTypeCatalogResponse::class.java).toJson(catalog)
                        settings.saveTriggerCatalog(json, System.currentTimeMillis())
                        _uiState.update { it.copy(triggerCatalog = catalog.triggerTypes, triggerCatalogsOffline = false) }
                    }
                    is com.liferadio.sync.data.remote.TriggerResult.Failure -> loadTriggerCatalogFromCache()
                }
                // Fetch instance list
                when (val result = client.listEventTriggers()) {
                    is com.liferadio.sync.data.remote.TriggerResult.Success -> {
                        val list = result.data as com.liferadio.sync.data.model.EventTriggerListResponse
                        val json = moshi.adapter(com.liferadio.sync.data.model.EventTriggerListResponse::class.java).toJson(list)
                        settings.saveTriggers(json, System.currentTimeMillis())
                        applyTriggers(list.triggers)
                    }
                    is com.liferadio.sync.data.remote.TriggerResult.Failure -> loadTriggerInstancesFromCache()
                }
            }
        }
    }

    private fun loadTriggersFromCache() {
        loadTriggerCatalogFromCache()
        loadTriggerInstancesFromCache()
    }

    private fun loadTriggerCatalogFromCache() {
        _uiState.update { it.copy(triggerCatalogsOffline = true) }
        val cJson = settings.triggerCatalogJson
        if (cJson != null) {
            runCatching {
                moshi.adapter(com.liferadio.sync.data.model.TriggerTypeCatalogResponse::class.java).fromJson(cJson)
            }.getOrNull()?.let {
                _uiState.update { s -> s.copy(triggerCatalog = it.triggerTypes, triggerCatalogsOffline = true) }
            }
        }
    }

    private fun loadTriggerInstancesFromCache() {
        _uiState.update { it.copy(triggersOffline = true) }
        val tJson = settings.triggersJson
        if (tJson != null) {
            runCatching {
                moshi.adapter(com.liferadio.sync.data.model.EventTriggerListResponse::class.java).fromJson(tJson)
            }.getOrNull()?.let { applyTriggers(it.triggers, offline = true) }
        }
    }

    private fun applyTriggers(triggers: List<com.liferadio.sync.data.model.EventTrigger>, offline: Boolean = false) {
        val associated = triggers.filter { it.wishId != null }.groupBy { requireNotNull(it.wishId) }
        val conflicts = associated.filterValues { it.size > 1 }.keys
        // Never silently choose one record when another client has produced duplicates.
        val map = associated.filterValues { it.size == 1 }.mapValues { it.value.single() }
        _uiState.update {
            it.copy(
                activeTriggers = triggers,
                wishTriggerMap = map,
                triggerConflictWishIds = conflicts,
                triggersOffline = offline
            )
        }
    }

    // --- Create wish with optional trigger ---

    /** Refactored create: step 1 = create wish, step 2 = attach trigger if selected. */
    fun createWishWithTrigger() {
        val state = _uiState.value
        if (state.wishCreateSending) return
        val text = state.wishCreateText.trim()
        if (text.isBlank() || text.length > 30) return
        if (state.wishCreateTriggerType != null &&
            (state.triggersOffline || state.triggerCatalogsOffline ||
                state.triggerCatalog.none { it.triggerType == state.wishCreateTriggerType })) return

        val client = ensureWishClient() ?: return
        val requestId = _wishCreateRequestId ?: java.util.UUID.randomUUID().toString()
        _wishCreateRequestId = requestId
        _uiState.update { it.copy(wishCreateSending = true, wishCreateError = "") }

        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                val create = com.liferadio.sync.data.model.WishCreate(
                    requestId = requestId, text = text, durationDays = state.wishCreateDuration
                )
                when (val result = client.createWish(create)) {
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        _wishCreateRequestId = null
                        val wish = result.data as com.liferadio.sync.data.model.Wish
                        // Step 2: attach trigger if selected
                        val triggerType = state.wishCreateTriggerType
                        if (triggerType != null) {
                            val tClient = ensureTriggerClient()
                            if (tClient != null) {
                                val tId = _triggerCreateRequestId ?: java.util.UUID.randomUUID().toString()
                                _triggerCreateRequestId = tId
                                val params = buildTriggerParams(triggerType, state.wishCreateTriggerParams)
                                val interval = normalizedTriggerInterval(triggerType, state.wishCreateTriggerInterval, state.triggerCatalog)
                                val tCreate = com.liferadio.sync.data.model.EventTriggerCreate(
                                    requestId = tId, wishId = wish.wishId, triggerType = triggerType,
                                    configVersion = 1, parameters = params, intervalMinutes = interval
                                )
                                when (val tResult = tClient.createEventTrigger(tCreate)) {
                                    is com.liferadio.sync.data.remote.TriggerResult.Success -> _triggerCreateRequestId = null
                                    is com.liferadio.sync.data.remote.TriggerResult.Failure -> {
                                        _triggerCreateRequestId = null
                                        _uiState.update {
                                            it.copy(wishesError = "心愿已创建，但提醒设置失败：${tResult.reason}")
                                        }
                                    }
                                }
                            } else {
                                _uiState.update { it.copy(wishesError = "心愿已创建，但提醒客户端尚未就绪") }
                            }
                        }
                        _uiState.update {
                            it.copy(showWishCreateDialog = false, wishCreateText = "",
                                wishCreateSending = false, wishCreateTriggerType = null, wishCreateTriggerParams = emptyMap())
                        }
                        refreshWishes()
                        refreshTriggersAndCatalog()
                    }
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        if (!WishEditorPolicy.shouldRetainCreateRequestId(result.statusCode)) _wishCreateRequestId = null
                        _uiState.update { it.copy(wishCreateSending = false, wishCreateError = result.reason) }
                    }
                }
            }
        }
    }

    /** Save the shared create/edit template.  Text is saved first; reminder failure is reported separately. */
    fun saveWishEditor() {
        val state = _uiState.value
        if (state.editingWishId == null) {
            createWishWithTrigger()
            return
        }
        val wishId = state.editingWishId
        val text = state.wishCreateText.trim()
        if (text.isBlank() || text.length > 30 || state.wishCreateSending || !WishEditorPolicy.canWrite(state.wishesCacheOnly)) return
        if (state.editingWishCanEditReminder && state.wishCreateTriggerType != null &&
            (state.triggersOffline || state.triggerCatalogsOffline ||
                state.triggerCatalog.none { it.triggerType == state.wishCreateTriggerType })) return
        val client = ensureWishClient() ?: return
        _uiState.update { it.copy(wishCreateSending = true, wishCreateError = "") }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                when (val textResult = client.updateWish(wishId, com.liferadio.sync.data.model.WishPatch(text))) {
                    is com.liferadio.sync.data.remote.WishResult.Failure -> {
                        _uiState.update { it.copy(wishCreateSending = false, wishCreateError = textResult.reason) }
                    }
                    is com.liferadio.sync.data.remote.WishResult.Success -> {
                        val reminderProblem = if (state.editingWishCanEditReminder) saveEditedWishReminder(state, wishId) else null
                        _uiState.update {
                            if (reminderProblem == null) it.copy(
                                showWishCreateDialog = false, editingWishId = null, editingWishCanEditReminder = true,
                                wishCreateSending = false, wishCreateText = "", wishCreateError = "",
                                wishCreateTriggerType = null, wishCreateTriggerParams = emptyMap(), wishesError = ""
                            ) else it.copy(
                                wishCreateSending = false,
                                wishCreateError = WishEditorPolicy.partialReminderFailureMessage(reminderProblem)
                            )
                        }
                        // Refetch both resources: the central response, not a local optimistic guess, is final.
                        refreshWishes()
                        refreshTriggersAndCatalog()
                    }
                }
            }
        }
    }

    private suspend fun saveEditedWishReminder(state: UiState, wishId: String): String? {
        val existing = state.wishTriggerMap[wishId]
        val chosen = state.wishCreateTriggerType
        if (chosen == null) {
            if (existing?.enabled != true) return null
            val client = ensureTriggerClient() ?: return "提醒客户端尚未就绪"
            return when (val result = client.updateEventTrigger(existing.triggerId,
                com.liferadio.sync.data.model.EventTriggerPatch(enabled = false))) {
                is com.liferadio.sync.data.remote.TriggerResult.Success -> null
                is com.liferadio.sync.data.remote.TriggerResult.Failure -> result.reason
            }
        }
        val client = ensureTriggerClient() ?: return "提醒客户端尚未就绪"
        val params = buildTriggerParams(chosen, state.wishCreateTriggerParams)
        val interval = normalizedTriggerInterval(chosen, state.wishCreateTriggerInterval, state.triggerCatalog)
        if (existing != null && existing.triggerType == chosen) {
            return when (val result = client.updateEventTrigger(existing.triggerId,
                com.liferadio.sync.data.model.EventTriggerPatch(params, interval, enabled = true))) {
                is com.liferadio.sync.data.remote.TriggerResult.Success -> null
                is com.liferadio.sync.data.remote.TriggerResult.Failure -> result.reason
            }
        }
        if (existing != null) {
            when (val result = client.deleteEventTrigger(existing.triggerId)) {
                is com.liferadio.sync.data.remote.TriggerResult.Failure -> return "移除旧提醒失败：${result.reason}"
                is com.liferadio.sync.data.remote.TriggerResult.Success -> Unit
            }
        }
        val requestId = java.util.UUID.randomUUID().toString()
        return when (val result = client.createEventTrigger(com.liferadio.sync.data.model.EventTriggerCreate(
            requestId, wishId, chosen, 1, params, interval
        ))) {
            is com.liferadio.sync.data.remote.TriggerResult.Success -> null
            is com.liferadio.sync.data.remote.TriggerResult.Failure -> result.reason
        }
    }

    // --- Trigger CRUD for an existing wish ---

    fun showTriggerDialog(wishId: String) {
        val state = _uiState.value
        if (state.wishesCacheOnly || state.triggersOffline || state.triggerCatalogsOffline) return
        if (wishId in state.triggerConflictWishIds) {
            _uiState.update { it.copy(wishesError = "这条心愿存在多个提醒记录，请先在其他客户端处理") }
            return
        }
        val existing = state.wishTriggerMap[wishId]
        _uiState.update {
            it.copy(
                showTriggerDialogForWish = wishId,
                triggerDialogType = existing?.triggerType,
                triggerDialogParams = if (existing != null) existing.parameters.mapValues { it.value.toString() } else emptyMap(),
                triggerDialogInterval = existing?.intervalMinutes ?: 60,
                triggerDialogError = "",
                triggerDialogSending = false
            )
        }
    }

    fun dismissTriggerDialog() {
        _triggerCreateRequestId = null
        _uiState.update { it.copy(showTriggerDialogForWish = null, triggerDialogType = null, triggerDialogError = "") }
    }

    fun setTriggerDialogType(type: String?) {
        _triggerCreateRequestId = null
        _uiState.update { it.copy(triggerDialogType = type, triggerDialogError = "") }
    }

    fun setTriggerDialogParam(key: String, value: String) {
        _triggerCreateRequestId = null
        _uiState.update { it.copy(triggerDialogParams = it.triggerDialogParams + (key to value), triggerDialogError = "") }
    }

    fun setTriggerDialogInterval(minutes: Int) {
        _triggerCreateRequestId = null
        _uiState.update { it.copy(triggerDialogInterval = minutes, triggerDialogError = "") }
    }

    fun saveTriggerForWish() {
        val state = _uiState.value
        val wishId = state.showTriggerDialogForWish ?: return
        val type = state.triggerDialogType ?: return
        if (state.triggerDialogSending) return

        val existing = state.wishTriggerMap[wishId]
        val client = ensureTriggerClient() ?: return
        _uiState.update { it.copy(triggerDialogSending = true, triggerDialogError = "") }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                if (existing != null && existing.triggerType == type) {
                    // Same type → update and (re-)enable the retained record.
                    val patch = com.liferadio.sync.data.model.EventTriggerPatch(
                        parameters = buildTriggerParams(type, state.triggerDialogParams),
                        intervalMinutes = normalizedTriggerInterval(type, state.triggerDialogInterval, state.triggerCatalog),
                        enabled = true
                    )
                    when (val r = client.updateEventTrigger(existing.triggerId, patch)) {
                        is com.liferadio.sync.data.remote.TriggerResult.Success -> {
                            _uiState.update { it.copy(showTriggerDialogForWish = null, triggerDialogSending = false) }
                            refreshTriggersAndCatalog()
                        }
                        is com.liferadio.sync.data.remote.TriggerResult.Failure -> _uiState.update {
                            it.copy(triggerDialogSending = false, triggerDialogError = r.reason)
                        }
                    }
                } else {
                    // Different type or new → DELETE existing then POST new
                    if (existing != null) {
                        when (client.deleteEventTrigger(existing.triggerId)) {
                            is com.liferadio.sync.data.remote.TriggerResult.Success -> {} // proceed
                            is com.liferadio.sync.data.remote.TriggerResult.Failure -> {
                                _uiState.update { it.copy(triggerDialogSending = false, triggerDialogError = "移除旧提醒失败") }
                                return@withContext
                            }
                        }
                    }
                    val tId = _triggerCreateRequestId ?: java.util.UUID.randomUUID().toString()
                    _triggerCreateRequestId = tId
                    val create = com.liferadio.sync.data.model.EventTriggerCreate(
                        requestId = tId, wishId = wishId, triggerType = type, configVersion = 1,
                        parameters = buildTriggerParams(type, state.triggerDialogParams),
                        intervalMinutes = normalizedTriggerInterval(type, state.triggerDialogInterval, state.triggerCatalog)
                    )
                    when (val r = client.createEventTrigger(create)) {
                        is com.liferadio.sync.data.remote.TriggerResult.Success -> {
                            _triggerCreateRequestId = null
                            _uiState.update { it.copy(showTriggerDialogForWish = null, triggerDialogSending = false) }
                            refreshTriggersAndCatalog()
                        }
                        is com.liferadio.sync.data.remote.TriggerResult.Failure -> {
                            if (!com.liferadio.sync.data.remote.CentralTriggerClient.isRetryable(r.statusCode))
                                _triggerCreateRequestId = null
                            _uiState.update {
                                it.copy(triggerDialogSending = false, triggerDialogError =
                                if (existing != null) "新提醒设置失败，当前已无提醒" else "设置提醒失败：" + r.reason)
                            }
                        }
                    }
                }
            }
        }
    }

    fun removeTriggerFromWish(wishId: String) {
        val existing = _uiState.value.wishTriggerMap[wishId] ?: return
        val client = ensureTriggerClient() ?: return
        _uiState.update { it.copy(triggerDialogSending = true, triggerDialogError = "") }
        viewModelScope.launch {
            withContext(kotlinx.coroutines.Dispatchers.IO) {
                // "关闭" preserves the trigger record and only disables evaluation.
                // Reopening the same reminder can then PATCH it back to enabled without
                // a delete/create race or a new idempotency key.
                when (client.updateEventTrigger(
                    existing.triggerId,
                    com.liferadio.sync.data.model.EventTriggerPatch(enabled = false)
                )) {
                    is com.liferadio.sync.data.remote.TriggerResult.Success -> {
                        _uiState.update {
                            it.copy(showTriggerDialogForWish = null, triggerDialogSending = false)
                        }
                        refreshTriggersAndCatalog()
                    }
                    is com.liferadio.sync.data.remote.TriggerResult.Failure -> {
                        _uiState.update {
                            it.copy(triggerDialogSending = false, triggerDialogError = "关闭提醒失败")
                        }
                    }
                }
            }
        }
    }

    // --- Create-dialog trigger helpers ---

    fun setWishCreateTrigger(type: String?) {
        _triggerCreateRequestId = null
        _uiState.update { it.copy(wishCreateTriggerType = type) }
    }

    fun setWishCreateTriggerParam(key: String, value: String) {
        _triggerCreateRequestId = null
        _uiState.update { it.copy(wishCreateTriggerParams = it.wishCreateTriggerParams + (key to value)) }
    }

    fun setWishCreateTriggerInterval(minutes: Int) {
        _triggerCreateRequestId = null
        _uiState.update { it.copy(wishCreateTriggerInterval = minutes) }
    }

    private fun buildTriggerParams(type: String, params: Map<String, String>): Map<String, Any?> {
        return when (type) {
            "blacklist_usage_milestone" -> mapOf("platform_scope" to "all")
            "device_usage_milestone" -> mapOf("device_id" to settings.deviceId)
            "late_usage_milestone" -> mapOf(
                "device_id" to "all",
                "start_local_time" to (params["start_local_time"] ?: "23:00")
            )
            "scheduled_reminder" -> mapOf(
                "reminder_local_time" to (params["reminder_local_time"] ?: "22:30")
            )
            else -> params
        }
    }

    private fun normalizedTriggerInterval(
        type: String,
        requested: Int,
        catalog: List<com.liferadio.sync.data.model.TriggerTypeCatalogItem>
    ): Int {
        // The contract retains interval_minutes for a uniform trigger transport shape; a clock
        // reminder has no user-configurable interval, so never expose this implementation value.
        if (type == "scheduled_reminder") return ScheduledReminderPolicy.DUMMY_INTERVAL_MINUTES
        val allowed = catalog.firstOrNull { it.triggerType == type }?.intervalMinutes?.allowedValues
            ?: listOf(15, 30, 60, 120)
        return requested.takeIf { it in allowed } ?: allowed.firstOrNull { it == 60 } ?: allowed.first()
    }
}
