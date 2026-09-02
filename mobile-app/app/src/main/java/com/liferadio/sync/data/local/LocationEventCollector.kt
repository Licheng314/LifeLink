package com.liferadio.sync.data.local

import android.content.Context
import android.location.Location
import androidx.core.location.LocationCompat
import androidx.room.withTransaction
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import java.nio.charset.StandardCharsets
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlin.math.pow

data class MotionWindowSnapshot(
    val startedAt: Long,
    val endedAt: Long,
    val accelerometerAvailable: Boolean,
    val sensorSampleCount: Int,
    val triggerCount: Int,
    val thresholdMetersPerSecondSquared: Float,
    val peakDeltaMetersPerSecondSquared: Float
)

internal enum class SegmentAction { START, UPDATE, FINALIZE_AND_START }

internal data class SegmentPlan(val action: SegmentAction, val kind: String)

internal object LocationSegmentationPolicy {
    fun acceptsForActiveSegment(observedAt: Long, lastSeenAt: Long): Boolean = observedAt > lastSeenAt

    fun plan(hasActive: Boolean, distanceMeters: Float, durationSeconds: Int): SegmentPlan = when {
        !hasActive -> SegmentPlan(SegmentAction.START, "sample")
        distanceMeters > 150f -> SegmentPlan(SegmentAction.FINALIZE_AND_START, "sample")
        durationSeconds >= 15 * 60 -> SegmentPlan(SegmentAction.UPDATE, "stay")
        else -> SegmentPlan(SegmentAction.UPDATE, "sample")
    }

    fun finalRevision(lastActiveRevision: Long): Long {
        require(lastActiveRevision < Long.MAX_VALUE)
        return lastActiveRevision + 1L
    }
}

class LocationEventCollector(
    context: Context,
    private val database: AppDatabase,
    private val settings: SettingsStore
) {

    private val moshi = Moshi.Builder()
        .add(KotlinJsonAdapterFactory())
        .build()
    private val mapAdapter = moshi.adapter(Map::class.java)
    private val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("UTC")
    }
    private val placeResolver = LocationPlaceResolver(context)
    private val segmentationMutex = Mutex()
    private val recordMutex = Mutex()

    suspend fun record(
        location: Location,
        motionWindow: MotionWindowSnapshot,
        observedAt: Long = System.currentTimeMillis()
    ) = recordMutex.withLock {
        val locationTime = location.time.takeIf { it > 0L } ?: observedAt
        val eventId = sampleId(location, locationTime)
        if (database.dataEventDao().containsEvent(eventId)) {
            settings.lastLocationDetectedAt = observedAt
            return@withLock
        }
        settings.lastLocationDetectedAt = observedAt
        settings.saveLastLocation(location.latitude, location.longitude, location.accuracy)
        val point = location.toCluster(locationTime)
        val place = resolvePlace(point, observedAt)
        if (place == null) {
            settings.clearLastResolvedPlace()
        }

        val provider = location.provider.orEmpty().ifBlank { "fused" }
        val payload = mapOf(
            "kind" to "observation",
            "latitude" to location.latitude.roundTo(5),
            "longitude" to location.longitude.roundTo(5),
            "coordinate_precision_digits" to 5,
            "accuracy_m" to location.accuracy,
            "speed_mps" to location.speed.takeIf { location.hasSpeed() },
            "provider" to provider,
            "is_mock" to LocationCompat.isMock(location),
            "observed_at" to dateFormat.format(Date(observedAt)),
            "location_time" to dateFormat.format(Date(locationTime)),
            "place" to place?.toPayload(),
            "motion_window" to mapOf(
                "started_at" to dateFormat.format(Date(motionWindow.startedAt)),
                "ended_at" to dateFormat.format(Date(motionWindow.endedAt)),
                "accelerometer_available" to motionWindow.accelerometerAvailable,
                "sensor_sample_count" to motionWindow.sensorSampleCount,
                "motion_triggered" to (motionWindow.triggerCount > 0),
                "trigger_count" to motionWindow.triggerCount,
                "threshold_mps2" to motionWindow.thresholdMetersPerSecondSquared,
                "peak_delta_mps2" to motionWindow.peakDeltaMetersPerSecondSquared
            )
        )
        database.withTransaction {
            database.locationSampleDao().insert(
                LocationSampleEntity(
                    id = eventId,
                    observedAt = locationTime,
                    latitude = location.latitude,
                    longitude = location.longitude,
                    accuracyMeters = location.accuracy,
                    provider = provider,
                    motionWindowStartedAt = motionWindow.startedAt,
                    accelerometerAvailable = motionWindow.accelerometerAvailable,
                    accelerometerSampleCount = motionWindow.sensorSampleCount,
                    motionTriggerCount = motionWindow.triggerCount,
                    motionThresholdMetersPerSecondSquared = motionWindow.thresholdMetersPerSecondSquared,
                    peakMotionDeltaMetersPerSecondSquared = motionWindow.peakDeltaMetersPerSecondSquared
                )
            )
            database.dataEventDao().insert(
                DataEventEntity(
                    id = eventId,
                    source = "android_fused_location",
                    sourceType = "android",
                    dataType = "location",
                    timestamp = dateFormat.format(Date(locationTime)),
                    duration = 0,
                    dataJson = mapAdapter.toJson(payload),
                    synced = false,
                    readyToSync = true,
                    revision = observedAt
                )
            )
        }
        updateActiveCluster(location, locationTime)
    }

    /**
     * Keep one durable, revisioned segment while fixes remain within the stay radius. Moving
     * outside the radius finalizes the old segment and starts a new one. Replacing the same
     * Room row is intentional: central delivery tracks the delivered revision, so an updated
     * active segment is queued again without inventing a second event identity.
     */
    private suspend fun updateActiveCluster(location: Location, observedAt: Long) {
        segmentationMutex.withLock {
            val active = settings.getActiveLocationCluster()
            if (active != null && !LocationSegmentationPolicy.acceptsForActiveSegment(observedAt, active.lastSeenAt)) {
                return@withLock
            }
            val distance = active?.let {
                distanceMeters(it.latitude, it.longitude, location.latitude, location.longitude)
            } ?: 0f
            val duration = active?.let { durationSeconds(it.startedAt, observedAt) } ?: 0
            val plan = LocationSegmentationPolicy.plan(active != null, distance, duration)
            when (plan.action) {
            SegmentAction.START -> {
                val created = location.toCluster(observedAt).withStableEventId()
                settings.saveActiveLocationCluster(created)
                database.dataEventDao().insert(
                    createEvent(created, observedAt, "sample", isActive = true, currentLocation = location)
                )
                return@withLock
            }
            SegmentAction.UPDATE -> {
                requireNotNull(active)
                val updated = active.copy(
                    accuracyMeters = minOf(active.accuracyMeters, location.accuracy),
                    lastSeenAt = maxOf(active.lastSeenAt, observedAt)
                )
                settings.saveActiveLocationCluster(updated)
                database.dataEventDao().insert(
                    createEvent(updated, updated.lastSeenAt, plan.kind, isActive = true, currentLocation = location)
                )
                return@withLock
            }
            SegmentAction.FINALIZE_AND_START -> {
            requireNotNull(active)
            finalizeCluster(active, active.lastSeenAt)
            val created = location.toCluster(observedAt).withStableEventId()
            settings.saveActiveLocationCluster(created)
            database.dataEventDao().insert(
                createEvent(created, observedAt, "sample", isActive = true, currentLocation = location)
            )
            }
            }
        }
    }

    suspend fun flushActiveCluster() {
        segmentationMutex.withLock {
            settings.getActiveLocationCluster()?.let { cluster ->
                finalizeCluster(cluster, cluster.lastSeenAt)
                settings.clearActiveLocationCluster()
            }
        }
    }

    private suspend fun finalizeCluster(cluster: ActiveLocationCluster, observedAt: Long) {
        val durationSeconds = durationSeconds(cluster.startedAt, observedAt)
        val kind = if (durationSeconds >= MIN_STAY_SECONDS) "stay" else "sample"
        database.dataEventDao().insert(
            createEvent(cluster, observedAt, kind, isActive = false, currentLocation = null)
        )
    }

    private suspend fun createEvent(
        cluster: ActiveLocationCluster,
        observedAt: Long,
        kind: String,
        isActive: Boolean,
        currentLocation: Location?
    ): DataEventEntity {
        val eventId = cluster.activeEventId ?: stableSegmentId(cluster.startedAt)
        val place = resolvePlace(cluster, observedAt)
        val frequentPlace = if (kind == "stay" && !isActive) {
            settings.registerStayAtPlace(
                cluster.latitude,
                cluster.longitude,
                durationSeconds(cluster.startedAt, observedAt)
            ).takeIf { it.isFrequent }
        } else {
            null
        }
        val payload = mapOf(
            "kind" to kind,
            "latitude" to cluster.latitude.roundTo(5),
            "longitude" to cluster.longitude.roundTo(5),
            "coordinate_precision_digits" to 5,
            "accuracy_m" to cluster.accuracyMeters,
            "speed_mps" to 0.0,
            "provider" to cluster.provider,
            "is_mock" to false,
            "observed_until" to dateFormat.format(Date(observedAt)),
            "is_active" to isActive,
            "latest_observed_at" to dateFormat.format(Date(observedAt)),
            "current_latitude" to currentLocation?.latitude?.roundTo(5),
            "current_longitude" to currentLocation?.longitude?.roundTo(5),
            "current_accuracy_m" to currentLocation?.accuracy,
            "place" to place?.toPayload(),
            "frequent_place" to frequentPlace?.let {
                mapOf(
                    "id" to it.id,
                    "label" to it.label,
                    "visit_count" to it.visitCount,
                    "total_stay_seconds" to it.totalStaySeconds
                )
            }
        )
        return DataEventEntity(
            id = eventId,
            source = "android_fused_location",
            sourceType = "android",
            dataType = "location",
            timestamp = dateFormat.format(Date(cluster.startedAt)),
            duration = durationSeconds(cluster.startedAt, observedAt),
            dataJson = mapAdapter.toJson(payload),
            synced = false,
            readyToSync = true,
            // A final payload differs from the last active payload and therefore must advance
            // the revision even though its semantic end time remains exactly lastSeenAt.
            revision = if (isActive) observedAt else LocationSegmentationPolicy.finalRevision(observedAt)
        )
    }

    private fun Location.toCluster(observedAt: Long) = ActiveLocationCluster(
        latitude = latitude,
        longitude = longitude,
        accuracyMeters = accuracy,
        startedAt = observedAt,
        lastSeenAt = observedAt,
        provider = provider.orEmpty().ifBlank { "fused" }
    )

    private fun ActiveLocationCluster.withStableEventId() = copy(
        activeEventId = stableSegmentId(startedAt)
    )

    private fun durationSeconds(startedAt: Long, observedAt: Long): Int =
        ((observedAt - startedAt).coerceAtLeast(0L) / 1000L).toInt()

    private suspend fun resolvePlace(cluster: ActiveLocationCluster, observedAt: Long): ResolvedPlace? {
        val cachedPlace = settings.getLastResolvedPlace()
        val cachedCoordinates = settings.getLastResolvedPlaceCoordinates()
        if (
            cachedPlace != null &&
            cachedCoordinates != null &&
            observedAt - cachedPlace.resolvedAt <= PLACE_CACHE_MILLIS &&
            distanceMeters(cachedCoordinates.first, cachedCoordinates.second, cluster.latitude, cluster.longitude) <= STAY_RADIUS_METERS
        ) {
            return cachedPlace
        }

        val location = Location(cluster.provider).apply {
            latitude = cluster.latitude
            longitude = cluster.longitude
            accuracy = cluster.accuracyMeters
        }
        return placeResolver.resolve(location, observedAt)?.also { place ->
            settings.saveLastResolvedPlace(place, cluster.latitude, cluster.longitude)
        }
    }

    private fun distanceMeters(
        firstLatitude: Double,
        firstLongitude: Double,
        secondLatitude: Double,
        secondLongitude: Double
    ): Float {
        val results = FloatArray(1)
        Location.distanceBetween(firstLatitude, firstLongitude, secondLatitude, secondLongitude, results)
        return results[0]
    }

    private fun ResolvedPlace.toPayload(): Map<String, Any?> = mapOf(
        "country" to country,
        "admin_area" to adminArea,
        "city" to city,
        "district" to district,
        "road_or_poi" to roadOrPoi,
        "display_label" to displayLabel,
        "full_address" to fullAddress,
        "geocode_precision" to precision,
        "resolved_at" to dateFormat.format(Date(resolvedAt)),
        "geocode_source" to "android_geocoder"
    )

    private fun Double.roundTo(digits: Int): Double {
        val factor = 10.0.pow(digits)
        return kotlin.math.round(this * factor) / factor
    }

    private fun stableSegmentId(startedAt: Long): String {
        val raw = "location|segment|$startedAt"
        return UUID.nameUUIDFromBytes(raw.toByteArray(StandardCharsets.UTF_8)).toString()
    }

    private fun sampleId(location: Location, locationTime: Long): String {
        val normalizedTimeSeconds = locationTime / 1000L
        val raw = "location|sample|$normalizedTimeSeconds|${location.latitude.roundTo(5)}|${location.longitude.roundTo(5)}|${location.provider}"
        return UUID.nameUUIDFromBytes(raw.toByteArray(StandardCharsets.UTF_8)).toString()
    }

    private companion object {
        const val STAY_RADIUS_METERS = 150f
        const val MIN_STAY_SECONDS = 15 * 60
        const val PLACE_CACHE_MILLIS = 15 * 60 * 1000L
    }
}
