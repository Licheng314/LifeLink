package com.liferadio.sync.data.local

import android.content.Context
import android.content.SharedPreferences
import android.location.Location
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.UUID

data class NativeUsageSession(
    val packageName: String,
    val className: String,
    val startedAt: Long
)

data class ActiveLocationCluster(
    val latitude: Double,
    val longitude: Double,
    val accuracyMeters: Float,
    val startedAt: Long,
    val lastSeenAt: Long,
    val provider: String,
    val activeEventId: String? = null
)

data class FrequentPlace(
    val id: String,
    val label: String,
    val latitude: Double,
    val longitude: Double,
    val visitCount: Int,
    val totalStaySeconds: Int
) {
    val isFrequent: Boolean
        get() = visitCount >= FREQUENT_PLACE_MIN_VISITS || totalStaySeconds >= FREQUENT_PLACE_MIN_STAY_SECONDS

    companion object {
        const val FREQUENT_PLACE_MIN_VISITS = 3
        const val FREQUENT_PLACE_MIN_STAY_SECONDS = 4 * 60 * 60
    }
}

/** Last valid central response. This is deliberately a read-only cache, never local authority. */
data class SharedSettingsCache(
    val timezone: String,
    val dayStartHour: Int,
    val settingsVersion: Int,
    val updatedAt: String,
    val lastSuccessfulRefreshAt: Long,
    val fullResponseJson: String = ""
) {
    val hasCentralValue: Boolean
        get() = settingsVersion >= 1

    fun isCompleteCentralCache(): Boolean =
        timezone == "Asia/Shanghai" &&
            dayStartHour in 0..23 &&
            settingsVersion >= 1 &&
            updatedAt.endsWith("Z") &&
            runCatching { Instant.parse(updatedAt) }.isSuccess &&
            lastSuccessfulRefreshAt > 0L

    companion object {
        val SafeFallback = SharedSettingsCache(
            timezone = "Asia/Shanghai",
            dayStartHour = 0,
            settingsVersion = 0,
            updatedAt = "",
            lastSuccessfulRefreshAt = 0L
        )
    }
}

/**
 * 设置持久化 - 使用 SharedPreferences
 */
class SettingsStore(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("liferadio_settings", Context.MODE_PRIVATE)
    private val secureTokenStore = SecureTokenStore(context.applicationContext)

    var syncIntervalMinutes: Int
        get() = prefs.getInt(KEY_SYNC_INTERVAL, 10)
        set(value) = prefs.edit().putInt(KEY_SYNC_INTERVAL, value.coerceIn(1, 60)).apply()

    var nativeUsageLastCollectedAt: Long
        get() = prefs.getLong(KEY_NATIVE_USAGE_LAST_COLLECTED_AT, 0L)
        set(value) = prefs.edit().putLong(KEY_NATIVE_USAGE_LAST_COLLECTED_AT, value).apply()

    var isLocationTrackingEnabled: Boolean
        get() = prefs.getBoolean(KEY_LOCATION_TRACKING_ENABLED, false)
        set(value) = prefs.edit().putBoolean(KEY_LOCATION_TRACKING_ENABLED, value).apply()

    var lastLocationDetectedAt: Long
        get() = prefs.getLong(KEY_LOCATION_LAST_DETECTED_AT, 0L)
        set(value) = prefs.edit().putLong(KEY_LOCATION_LAST_DETECTED_AT, value).apply()

    fun saveLastLocation(latitude: Double, longitude: Double, accuracyMeters: Float) {
        prefs.edit()
            .putString(KEY_LOCATION_LAST_LATITUDE, latitude.toString())
            .putString(KEY_LOCATION_LAST_LONGITUDE, longitude.toString())
            .putFloat(KEY_LOCATION_LAST_ACCURACY, accuracyMeters)
            .apply()
    }

    fun getLastLocation(): Triple<Double, Double, Float>? {
        val latitude = prefs.getString(KEY_LOCATION_LAST_LATITUDE, null)?.toDoubleOrNull() ?: return null
        val longitude = prefs.getString(KEY_LOCATION_LAST_LONGITUDE, null)?.toDoubleOrNull() ?: return null
        return Triple(latitude, longitude, prefs.getFloat(KEY_LOCATION_LAST_ACCURACY, 0f))
    }

    fun saveLastResolvedPlace(place: ResolvedPlace, latitude: Double, longitude: Double) {
        prefs.edit()
            .putString(KEY_LOCATION_PLACE_COUNTRY, place.country)
            .putString(KEY_LOCATION_PLACE_ADMIN_AREA, place.adminArea)
            .putString(KEY_LOCATION_PLACE_CITY, place.city)
            .putString(KEY_LOCATION_PLACE_DISTRICT, place.district)
            .putString(KEY_LOCATION_PLACE_ROAD_OR_POI, place.roadOrPoi)
            .putString(KEY_LOCATION_PLACE_LABEL, place.displayLabel)
            .putString(KEY_LOCATION_PLACE_FULL_ADDRESS, place.fullAddress)
            .putString(KEY_LOCATION_PLACE_PRECISION, place.precision)
            .putLong(KEY_LOCATION_PLACE_RESOLVED_AT, place.resolvedAt)
            .putString(KEY_LOCATION_PLACE_LATITUDE, latitude.toString())
            .putString(KEY_LOCATION_PLACE_LONGITUDE, longitude.toString())
            .apply()
    }

    fun getLastResolvedPlace(): ResolvedPlace? {
        val resolvedAt = prefs.getLong(KEY_LOCATION_PLACE_RESOLVED_AT, 0L)
        if (resolvedAt <= 0L) return null
        return ResolvedPlace(
            country = prefs.getString(KEY_LOCATION_PLACE_COUNTRY, null),
            adminArea = prefs.getString(KEY_LOCATION_PLACE_ADMIN_AREA, null),
            city = prefs.getString(KEY_LOCATION_PLACE_CITY, null),
            district = prefs.getString(KEY_LOCATION_PLACE_DISTRICT, null),
            roadOrPoi = prefs.getString(KEY_LOCATION_PLACE_ROAD_OR_POI, null),
            displayLabel = prefs.getString(KEY_LOCATION_PLACE_LABEL, null),
            fullAddress = prefs.getString(KEY_LOCATION_PLACE_FULL_ADDRESS, null),
            precision = prefs.getString(KEY_LOCATION_PLACE_PRECISION, "coordinates") ?: "coordinates",
            resolvedAt = resolvedAt
        )
    }

    fun getLastResolvedPlaceCoordinates(): Pair<Double, Double>? {
        val latitude = prefs.getString(KEY_LOCATION_PLACE_LATITUDE, null)?.toDoubleOrNull() ?: return null
        val longitude = prefs.getString(KEY_LOCATION_PLACE_LONGITUDE, null)?.toDoubleOrNull() ?: return null
        return latitude to longitude
    }

    fun clearLastResolvedPlace() {
        prefs.edit()
            .remove(KEY_LOCATION_PLACE_COUNTRY)
            .remove(KEY_LOCATION_PLACE_ADMIN_AREA)
            .remove(KEY_LOCATION_PLACE_CITY)
            .remove(KEY_LOCATION_PLACE_DISTRICT)
            .remove(KEY_LOCATION_PLACE_ROAD_OR_POI)
            .remove(KEY_LOCATION_PLACE_LABEL)
            .remove(KEY_LOCATION_PLACE_FULL_ADDRESS)
            .remove(KEY_LOCATION_PLACE_PRECISION)
            .remove(KEY_LOCATION_PLACE_RESOLVED_AT)
            .remove(KEY_LOCATION_PLACE_LATITUDE)
            .remove(KEY_LOCATION_PLACE_LONGITUDE)
            .apply()
    }

    fun registerStayAtPlace(latitude: Double, longitude: Double, durationSeconds: Int): FrequentPlace {
        val places = loadFrequentPlaces()
        val matchIndex = places.indexOfFirst { place ->
            distanceMeters(place.latitude, place.longitude, latitude, longitude) <= FREQUENT_PLACE_RADIUS_METERS
        }
        val updated = if (matchIndex >= 0) {
            val current = places[matchIndex]
            val visits = current.visitCount + 1
            current.copy(
                latitude = (current.latitude * current.visitCount + latitude) / visits,
                longitude = (current.longitude * current.visitCount + longitude) / visits,
                visitCount = visits,
                totalStaySeconds = current.totalStaySeconds + durationSeconds
            ).also { places[matchIndex] = it }
        } else {
            FrequentPlace(
                id = UUID.randomUUID().toString(),
                label = "常驻点${places.size + 1}",
                latitude = latitude,
                longitude = longitude,
                visitCount = 1,
                totalStaySeconds = durationSeconds
            ).also { places += it }
        }
        saveFrequentPlaces(places)
        return updated
    }

    fun getActiveLocationCluster(): ActiveLocationCluster? {
        val startedAt = prefs.getLong(KEY_LOCATION_CLUSTER_STARTED_AT, 0L)
        if (startedAt <= 0L) return null
        val latitude = prefs.getString(KEY_LOCATION_CLUSTER_LATITUDE, null)?.toDoubleOrNull() ?: return null
        val longitude = prefs.getString(KEY_LOCATION_CLUSTER_LONGITUDE, null)?.toDoubleOrNull() ?: return null
        return ActiveLocationCluster(
            latitude = latitude,
            longitude = longitude,
            accuracyMeters = prefs.getFloat(KEY_LOCATION_CLUSTER_ACCURACY, 0f),
            startedAt = startedAt,
            lastSeenAt = prefs.getLong(KEY_LOCATION_CLUSTER_LAST_SEEN_AT, startedAt),
            provider = prefs.getString(KEY_LOCATION_CLUSTER_PROVIDER, "fused").orEmpty(),
            activeEventId = prefs.getString(KEY_LOCATION_CLUSTER_EVENT_ID, null)
        )
    }

    fun saveActiveLocationCluster(cluster: ActiveLocationCluster) {
        prefs.edit()
            .putString(KEY_LOCATION_CLUSTER_LATITUDE, cluster.latitude.toString())
            .putString(KEY_LOCATION_CLUSTER_LONGITUDE, cluster.longitude.toString())
            .putFloat(KEY_LOCATION_CLUSTER_ACCURACY, cluster.accuracyMeters)
            .putLong(KEY_LOCATION_CLUSTER_STARTED_AT, cluster.startedAt)
            .putLong(KEY_LOCATION_CLUSTER_LAST_SEEN_AT, cluster.lastSeenAt)
            .putString(KEY_LOCATION_CLUSTER_PROVIDER, cluster.provider)
            .putString(KEY_LOCATION_CLUSTER_EVENT_ID, cluster.activeEventId)
            .apply()
    }

    fun clearActiveLocationCluster() {
        prefs.edit()
            .remove(KEY_LOCATION_CLUSTER_LATITUDE)
            .remove(KEY_LOCATION_CLUSTER_LONGITUDE)
            .remove(KEY_LOCATION_CLUSTER_ACCURACY)
            .remove(KEY_LOCATION_CLUSTER_STARTED_AT)
            .remove(KEY_LOCATION_CLUSTER_LAST_SEEN_AT)
            .remove(KEY_LOCATION_CLUSTER_PROVIDER)
            .remove(KEY_LOCATION_CLUSTER_EVENT_ID)
            .apply()
    }

    fun getNativeUsageActiveSession(): NativeUsageSession? {
        val packageName = prefs.getString(KEY_NATIVE_USAGE_ACTIVE_PACKAGE, "").orEmpty()
        val startedAt = prefs.getLong(KEY_NATIVE_USAGE_ACTIVE_STARTED_AT, 0L)
        if (packageName.isBlank() || startedAt <= 0L) return null
        return NativeUsageSession(
            packageName = packageName,
            className = prefs.getString(KEY_NATIVE_USAGE_ACTIVE_CLASS_NAME, "").orEmpty(),
            startedAt = startedAt
        )
    }

    fun saveNativeUsageActiveSession(session: NativeUsageSession) {
        prefs.edit()
            .putString(KEY_NATIVE_USAGE_ACTIVE_PACKAGE, session.packageName)
            .putString(KEY_NATIVE_USAGE_ACTIVE_CLASS_NAME, session.className)
            .putLong(KEY_NATIVE_USAGE_ACTIVE_STARTED_AT, session.startedAt)
            .apply()
    }

    fun clearNativeUsageActiveSession() {
        prefs.edit()
            .remove(KEY_NATIVE_USAGE_ACTIVE_PACKAGE)
            .remove(KEY_NATIVE_USAGE_ACTIVE_CLASS_NAME)
            .remove(KEY_NATIVE_USAGE_ACTIVE_STARTED_AT)
            .apply()
    }

    var syncMode: String
        get() = SYNC_MODE_CENTRAL
        set(value) {
            require(value == SYNC_MODE_CENTRAL)
            prefs.edit().putString(KEY_SYNC_MODE, value).apply()
        }

    var centralBaseUrl: String
        get() = prefs.getString(KEY_CENTRAL_BASE_URL, "") ?: ""
        set(value) = prefs.edit().putString(KEY_CENTRAL_BASE_URL, value.trimEnd('/')).apply()

    var centralScope: String
        get() = prefs.getString(KEY_CENTRAL_SCOPE, "") ?: ""
        set(value) = prefs.edit().putString(KEY_CENTRAL_SCOPE, value).apply()

    var centralIssuedAt: String
        get() = prefs.getString(KEY_CENTRAL_ISSUED_AT, "") ?: ""
        set(value) = prefs.edit().putString(KEY_CENTRAL_ISSUED_AT, value).apply()

    var centralDeviceName: String
        get() = prefs.getString(KEY_CENTRAL_DEVICE_NAME, "") ?: ""
        set(value) = prefs.edit().putString(KEY_CENTRAL_DEVICE_NAME, value.take(100)).apply()

    val deviceId: String
        get() {
            prefs.getString(KEY_DEVICE_ID, null)?.takeIf { it.isNotBlank() }?.let { return it }
            return synchronized(prefs) {
                prefs.getString(KEY_DEVICE_ID, null)?.takeIf { it.isNotBlank() }
                    ?: "android-install-${UUID.randomUUID()}".also { generated ->
                        prefs.edit().putString(KEY_DEVICE_ID, generated).commit()
                    }
            }
        }

    val isCentralTokenConfigured: Boolean
        get() = secureTokenStore.readToken().isNotBlank()

    fun getCentralToken(): String = secureTokenStore.readToken()

    fun saveCentralToken(token: String) {
        secureTokenStore.saveToken(token)
        centralAuthBlocked = false
        centralRetryAttempt = 0
        centralNextRetryAt = 0L
    }

    fun clearCentralToken() {
        secureTokenStore.clearToken()
        centralBaseUrl = ""
        centralScope = ""
        centralIssuedAt = ""
        centralDeviceName = ""
        centralAuthBlocked = false
        centralRetryAttempt = 0
        centralNextRetryAt = 0L
    }

    var centralRetryAttempt: Int
        get() = prefs.getInt(KEY_CENTRAL_RETRY_ATTEMPT, 0)
        set(value) = prefs.edit().putInt(KEY_CENTRAL_RETRY_ATTEMPT, value.coerceAtLeast(0)).apply()

    var centralNextRetryAt: Long
        get() = prefs.getLong(KEY_CENTRAL_NEXT_RETRY_AT, 0L)
        set(value) = prefs.edit().putLong(KEY_CENTRAL_NEXT_RETRY_AT, value.coerceAtLeast(0L)).apply()

    var centralAuthBlocked: Boolean
        get() = prefs.getBoolean(KEY_CENTRAL_AUTH_BLOCKED, false)
        set(value) = prefs.edit().putBoolean(KEY_CENTRAL_AUTH_BLOCKED, value).apply()

    var centralLastStatus: String
        get() = prefs.getString(KEY_CENTRAL_LAST_STATUS, "") ?: ""
        set(value) = prefs.edit().putString(KEY_CENTRAL_LAST_STATUS, value.take(300)).apply()

    var centralLastSyncAt: Long
        get() = prefs.getLong(KEY_CENTRAL_LAST_SYNC_AT, 0L)
        set(value) = prefs.edit().putLong(KEY_CENTRAL_LAST_SYNC_AT, value.coerceAtLeast(0L)).apply()

    fun getSharedSettingsCache(): SharedSettingsCache {
        val cache = SharedSettingsCache(
            timezone = prefs.getString(KEY_SHARED_SETTINGS_TIMEZONE, null) ?: "",
            dayStartHour = prefs.getInt(KEY_SHARED_SETTINGS_DAY_START_HOUR, -1),
            settingsVersion = prefs.getInt(KEY_SHARED_SETTINGS_VERSION, 0),
            updatedAt = prefs.getString(KEY_SHARED_SETTINGS_UPDATED_AT, null) ?: "",
            lastSuccessfulRefreshAt = prefs.getLong(KEY_SHARED_SETTINGS_LAST_REFRESH_AT, 0L),
            fullResponseJson = prefs.getString(KEY_SHARED_SETTINGS_FULL_RESPONSE, "").orEmpty()
        )
        return cache.takeIf(SharedSettingsCache::isCompleteCentralCache) ?: SharedSettingsCache.SafeFallback
    }

    /** Persists one validated central response atomically; callers must not construct local values. */
    fun saveSharedSettingsCache(cache: SharedSettingsCache) {
        require(cache.isCompleteCentralCache()) { "shared settings cache is incomplete or invalid" }
        check(prefs.edit()
            .putString(KEY_SHARED_SETTINGS_TIMEZONE, cache.timezone)
            .putInt(KEY_SHARED_SETTINGS_DAY_START_HOUR, cache.dayStartHour)
            .putInt(KEY_SHARED_SETTINGS_VERSION, cache.settingsVersion)
            .putString(KEY_SHARED_SETTINGS_UPDATED_AT, cache.updatedAt)
            .putLong(KEY_SHARED_SETTINGS_LAST_REFRESH_AT, cache.lastSuccessfulRefreshAt)
            .putString(KEY_SHARED_SETTINGS_FULL_RESPONSE, cache.fullResponseJson)
            .commit()) { "failed to persist shared settings cache" }
    }

    /** One complete, validated central response is committed as a single read-only cache value. */
    fun saveHealthInfoCache(responseJson: String) {
        require(responseJson.isNotBlank()) { "health info response must not be blank" }
        check(prefs.edit().putString(KEY_HEALTH_INFO_RESPONSE, responseJson).commit()) {
            "failed to persist health info cache"
        }
    }

    fun getHealthInfoCache(): String? = prefs.getString(KEY_HEALTH_INFO_RESPONSE, null)

    var stepCounterSessionId: String
        get() = prefs.getString(KEY_STEP_COUNTER_SESSION_ID, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_STEP_COUNTER_SESSION_ID, value).apply()

    var stepCounterLastValue: Long
        get() = prefs.getLong(KEY_STEP_COUNTER_LAST_VALUE, -1L)
        set(value) = prefs.edit().putLong(KEY_STEP_COUNTER_LAST_VALUE, value.coerceAtLeast(-1L)).apply()

    var stepCounterBootCount: Int
        get() = prefs.getInt(KEY_STEP_COUNTER_BOOT_COUNT, -1)
        set(value) = prefs.edit().putInt(KEY_STEP_COUNTER_BOOT_COUNT, value).apply()

    companion object {
        private const val KEY_SYNC_INTERVAL = "sync_interval"
        private const val KEY_NATIVE_USAGE_LAST_COLLECTED_AT = "native_usage_last_collected_at"
        private const val KEY_NATIVE_USAGE_ACTIVE_PACKAGE = "native_usage_active_package"
        private const val KEY_NATIVE_USAGE_ACTIVE_CLASS_NAME = "native_usage_active_class_name"
        private const val KEY_NATIVE_USAGE_ACTIVE_STARTED_AT = "native_usage_active_started_at"
        private const val KEY_LOCATION_TRACKING_ENABLED = "location_tracking_enabled"
        private const val KEY_LOCATION_LAST_DETECTED_AT = "location_last_detected_at"
        private const val KEY_LOCATION_LAST_LATITUDE = "location_last_latitude"
        private const val KEY_LOCATION_LAST_LONGITUDE = "location_last_longitude"
        private const val KEY_LOCATION_LAST_ACCURACY = "location_last_accuracy"
        private const val KEY_LOCATION_PLACE_COUNTRY = "location_place_country"
        private const val KEY_LOCATION_PLACE_ADMIN_AREA = "location_place_admin_area"
        private const val KEY_LOCATION_PLACE_CITY = "location_place_city"
        private const val KEY_LOCATION_PLACE_DISTRICT = "location_place_district"
        private const val KEY_LOCATION_PLACE_ROAD_OR_POI = "location_place_road_or_poi"
        private const val KEY_LOCATION_PLACE_LABEL = "location_place_label"
        private const val KEY_LOCATION_PLACE_FULL_ADDRESS = "location_place_full_address"
        private const val KEY_LOCATION_PLACE_PRECISION = "location_place_precision"
        private const val KEY_LOCATION_PLACE_RESOLVED_AT = "location_place_resolved_at"
        private const val KEY_LOCATION_PLACE_LATITUDE = "location_place_latitude"
        private const val KEY_LOCATION_PLACE_LONGITUDE = "location_place_longitude"
        private const val KEY_LOCATION_CLUSTER_LATITUDE = "location_cluster_latitude"
        private const val KEY_LOCATION_CLUSTER_LONGITUDE = "location_cluster_longitude"
        private const val KEY_LOCATION_CLUSTER_ACCURACY = "location_cluster_accuracy"
        private const val KEY_LOCATION_CLUSTER_STARTED_AT = "location_cluster_started_at"
        private const val KEY_LOCATION_CLUSTER_LAST_SEEN_AT = "location_cluster_last_seen_at"
        private const val KEY_LOCATION_CLUSTER_PROVIDER = "location_cluster_provider"
        private const val KEY_LOCATION_CLUSTER_EVENT_ID = "location_cluster_event_id"
        private const val KEY_FREQUENT_PLACES = "frequent_places"
        private const val KEY_SYNC_MODE = "sync_mode"
        private const val KEY_DEVICE_ID = "central_device_id"
        private const val KEY_CENTRAL_RETRY_ATTEMPT = "central_retry_attempt"
        private const val KEY_CENTRAL_NEXT_RETRY_AT = "central_next_retry_at"
        private const val KEY_CENTRAL_AUTH_BLOCKED = "central_auth_blocked"
        private const val KEY_CENTRAL_LAST_STATUS = "central_last_status"
        private const val KEY_CENTRAL_LAST_SYNC_AT = "central_last_sync_at"
        private const val KEY_CENTRAL_BASE_URL = "central_base_url"
        private const val KEY_CENTRAL_SCOPE = "central_scope"
        private const val KEY_CENTRAL_ISSUED_AT = "central_issued_at"
        private const val KEY_CENTRAL_DEVICE_NAME = "central_device_name"
        private const val KEY_SHARED_SETTINGS_TIMEZONE = "shared_settings_timezone"
        private const val KEY_SHARED_SETTINGS_DAY_START_HOUR = "shared_settings_day_start_hour"
        private const val KEY_SHARED_SETTINGS_VERSION = "shared_settings_version"
        private const val KEY_SHARED_SETTINGS_UPDATED_AT = "shared_settings_updated_at"
        private const val KEY_SHARED_SETTINGS_LAST_REFRESH_AT = "shared_settings_last_refresh_at"
        private const val KEY_SHARED_SETTINGS_FULL_RESPONSE = "shared_settings_full_response"
        private const val KEY_EVENT_BACKGROUND_CACHE = "event_background_cache"
        private const val KEY_EVENT_BACKGROUND_CACHE_AT = "event_background_cache_at"
        private const val KEY_HEALTH_INFO_RESPONSE = "health_info_response"
        private const val KEY_STEP_COUNTER_SESSION_ID = "step_counter_session_id"
        private const val KEY_STEP_COUNTER_LAST_VALUE = "step_counter_last_value"
        private const val KEY_STEP_COUNTER_BOOT_COUNT = "step_counter_boot_count"
        private const val KEY_WISH_CACHE = "wish_cache_json"
        private const val KEY_WISH_CACHE_AT = "wish_cache_refreshed_at"
        private const val KEY_TIMELINE_CACHE = "timeline_cache_json"
        private const val KEY_TIMELINE_CACHE_AT = "timeline_cache_refreshed_at"
        private const val KEY_NOTIFIED_IMPORTANT_TIMELINE_IDS = "notified_important_timeline_ids"
        private const val KEY_IMPORTANT_TIMELINE_BASELINE_READY = "important_timeline_baseline_ready"
        private const val KEY_NOTIFIED_ALERTABLE_TIMELINE_IDS = "notified_alertable_timeline_ids_v2"
        private const val KEY_ALERTABLE_TIMELINE_BASELINE_READY = "alertable_timeline_baseline_ready_v2"
        private const val KEY_TRIGGER_CATALOG_CACHE = "trigger_catalog_json"
        private const val KEY_TRIGGER_CATALOG_CACHE_AT = "trigger_catalog_cache_at"
        private const val KEY_TRIGGERS_CACHE = "triggers_cache_json"
        private const val KEY_TRIGGERS_CACHE_AT = "triggers_cache_at"
        private const val FREQUENT_PLACE_RADIUS_METERS = 200f

        const val SYNC_MODE_CENTRAL = "central"
    }

    private fun loadFrequentPlaces(): MutableList<FrequentPlace> = runCatching {
        val values = JSONArray(prefs.getString(KEY_FREQUENT_PLACES, "[]"))
        buildList {
            for (index in 0 until values.length()) {
                val value = values.optJSONObject(index) ?: continue
                add(
                    FrequentPlace(
                        id = value.getString("id"),
                        label = value.getString("label"),
                        latitude = value.getDouble("latitude"),
                        longitude = value.getDouble("longitude"),
                        visitCount = value.getInt("visit_count"),
                        totalStaySeconds = value.getInt("total_stay_seconds")
                    )
                )
            }
        }.toMutableList()
    }.getOrDefault(mutableListOf())

    private fun saveFrequentPlaces(places: List<FrequentPlace>) {
        val values = JSONArray()
        places.forEach { place ->
            values.put(
                JSONObject()
                    .put("id", place.id)
                    .put("label", place.label)
                    .put("latitude", place.latitude)
                    .put("longitude", place.longitude)
                    .put("visit_count", place.visitCount)
                    .put("total_stay_seconds", place.totalStaySeconds)
            )
        }
        prefs.edit().putString(KEY_FREQUENT_PLACES, values.toString()).apply()
    }

    private fun distanceMeters(
        firstLatitude: Double,
        firstLongitude: Double,
        secondLatitude: Double,
        secondLongitude: Double
    ): Float {
        val result = FloatArray(1)
        Location.distanceBetween(firstLatitude, firstLongitude, secondLatitude, secondLongitude, result)
        return result[0]
    }

    // --- Wish & Timeline read-only caches ---

    /** Last successfully fetched wish list response JSON, or null if no cache. */
    var wishCacheJson: String?
        get() = prefs.getString(KEY_WISH_CACHE, null)
        private set(value) { prefs.edit().putString(KEY_WISH_CACHE, value).apply() }

    var wishCacheRefreshedAt: Long
        get() = prefs.getLong(KEY_WISH_CACHE_AT, 0L)
        private set(value) { prefs.edit().putLong(KEY_WISH_CACHE_AT, value).apply() }

    /** Atomically save a validated wish list cache. */
    fun saveWishCache(json: String, refreshedAt: Long) {
        check(prefs.edit()
            .putString(KEY_WISH_CACHE, json)
            .putLong(KEY_WISH_CACHE_AT, refreshedAt)
            .commit()) { "failed to persist wish cache" }
    }

    /** Last successfully fetched timeline response JSON. */
    var timelineCacheJson: String?
        get() = prefs.getString(KEY_TIMELINE_CACHE, null)
        private set(value) { prefs.edit().putString(KEY_TIMELINE_CACHE, value).apply() }

    var timelineCacheRefreshedAt: Long
        get() = prefs.getLong(KEY_TIMELINE_CACHE_AT, 0L)
        private set(value) { prefs.edit().putLong(KEY_TIMELINE_CACHE_AT, value).apply() }

    fun saveTimelineCache(json: String, refreshedAt: Long) {
        check(prefs.edit()
            .putString(KEY_TIMELINE_CACHE, json)
            .putLong(KEY_TIMELINE_CACHE_AT, refreshedAt)
            .commit()) { "failed to persist timeline cache" }
    }

    /**
     * Returns high/normal timeline IDs not seen by this device before. The v2 baseline is separate
     * from the former high-only baseline so enabling normal alerts never replays recent history.
     */
    fun recordNewAlertableTimelineEventIds(eventIds: List<String>): List<String> {
        val known = prefs.getStringSet(KEY_NOTIFIED_ALERTABLE_TIMELINE_IDS, emptySet()).orEmpty()
        if (!prefs.getBoolean(KEY_ALERTABLE_TIMELINE_BASELINE_READY, false)) {
            prefs.edit()
                .putStringSet(KEY_NOTIFIED_ALERTABLE_TIMELINE_IDS, eventIds.takeLast(100).toSet())
                .putBoolean(KEY_ALERTABLE_TIMELINE_BASELINE_READY, true)
                .apply()
            return emptyList()
        }
        val newIds = eventIds.filterNot(known::contains)
        if (newIds.isNotEmpty()) {
            prefs.edit()
                .putStringSet(KEY_NOTIFIED_ALERTABLE_TIMELINE_IDS, (known + eventIds).toList().takeLast(100).toSet())
                .apply()
        }
        return newIds
    }

    /** Prevent data from a previously configured central server leaking into a new binding. */
    fun clearWishTimelineCaches() {
        check(prefs.edit()
            .remove(KEY_WISH_CACHE)
            .remove(KEY_WISH_CACHE_AT)
            .remove(KEY_TIMELINE_CACHE)
            .remove(KEY_TIMELINE_CACHE_AT)
            .remove(KEY_EVENT_BACKGROUND_CACHE)
            .remove(KEY_EVENT_BACKGROUND_CACHE_AT)
            .remove(KEY_NOTIFIED_IMPORTANT_TIMELINE_IDS)
            .remove(KEY_IMPORTANT_TIMELINE_BASELINE_READY)
            .remove(KEY_NOTIFIED_ALERTABLE_TIMELINE_IDS)
            .remove(KEY_ALERTABLE_TIMELINE_BASELINE_READY)
            .commit()) { "failed to clear wish and timeline caches" }
    }

    /** Central derived content; kept only as a read-only fallback while offline. */
    fun saveEventBackgroundCache(json: String, refreshedAt: Long) {
        require(json.isNotBlank() && refreshedAt > 0L)
        check(prefs.edit().putString(KEY_EVENT_BACKGROUND_CACHE, json)
            .putLong(KEY_EVENT_BACKGROUND_CACHE_AT, refreshedAt).commit()) { "failed to persist event background cache" }
    }

    fun getEventBackgroundCache(): String? = prefs.getString(KEY_EVENT_BACKGROUND_CACHE, null)

    // --- Trigger caches ---

    var triggerCatalogJson: String?
        get() = prefs.getString(KEY_TRIGGER_CATALOG_CACHE, null)
        private set(value) { prefs.edit().putString(KEY_TRIGGER_CATALOG_CACHE, value).apply() }

    var triggerCatalogAt: Long
        get() = prefs.getLong(KEY_TRIGGER_CATALOG_CACHE_AT, 0L)
        private set(value) { prefs.edit().putLong(KEY_TRIGGER_CATALOG_CACHE_AT, value).apply() }

    fun saveTriggerCatalog(json: String, at: Long) {
        check(prefs.edit().putString(KEY_TRIGGER_CATALOG_CACHE, json)
            .putLong(KEY_TRIGGER_CATALOG_CACHE_AT, at).commit()) { "save trigger catalog failed" }
    }

    var triggersJson: String?
        get() = prefs.getString(KEY_TRIGGERS_CACHE, null)
        private set(value) { prefs.edit().putString(KEY_TRIGGERS_CACHE, value).apply() }

    var triggersAt: Long
        get() = prefs.getLong(KEY_TRIGGERS_CACHE_AT, 0L)
        private set(value) { prefs.edit().putLong(KEY_TRIGGERS_CACHE_AT, value).apply() }

    fun saveTriggers(json: String, at: Long) {
        check(prefs.edit().putString(KEY_TRIGGERS_CACHE, json)
            .putLong(KEY_TRIGGERS_CACHE_AT, at).commit()) { "save triggers failed" }
    }

    fun clearTriggerCaches() {
        check(prefs.edit()
            .remove(KEY_TRIGGER_CATALOG_CACHE).remove(KEY_TRIGGER_CATALOG_CACHE_AT)
            .remove(KEY_TRIGGERS_CACHE).remove(KEY_TRIGGERS_CACHE_AT)
            .commit()) { "clear trigger caches failed" }
    }
}
