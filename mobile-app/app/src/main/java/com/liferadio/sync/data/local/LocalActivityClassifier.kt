package com.liferadio.sync.data.local

import java.time.ZoneId
import kotlin.math.max

/** Local-only activity reference derived from existing raw observations. It never writes an outbox event. */
object LocalActivityClassifier {
    const val BUCKET_MILLIS = 5 * 60 * 1000L
    const val WALKING_STEPS_PER_BUCKET = 10L
    const val RUNNING_STEPS_PER_BUCKET = 600L
    const val RELIABLE_ACCURACY_METERS = 50f
    const val MIN_TRANSPORT_DISTANCE_METERS = 100.0
    const val MIN_TRANSPORT_SPEED_MPS = 1.5
    const val STATIONARY_DISTANCE_METERS = 30.0
    const val MAX_LOCATION_GAP_MILLIS = 10 * 60 * 1000L
    const val MAX_STEP_GAP_MILLIS = 10 * 60 * 1000L

    enum class Label(val displayName: String) {
        WALKING("步行"), RUNNING("跑步"), TRANSPORT("乘坐交通工具"), STATIONARY("静止"), UNKNOWN("未知")
    }

    data class StepObservation(val observedAt: Long, val counterValue: Long, val sessionId: String)
    data class LocationObservation(val observedAt: Long, val latitude: Double, val longitude: Double, val accuracyMeters: Float)
    data class Interval(val label: Label, val startedAt: Long, val endedAt: Long)

    fun classifyDay(
        steps: List<StepObservation>,
        locations: List<LocationObservation>,
        dayStart: Long,
        dayEnd: Long
    ): List<Interval> {
        val allSteps = steps.sortedBy { it.observedAt }
        val allLocations = locations.sortedBy { it.observedAt }
        val occupied = (allSteps.map { bucketStart(it.observedAt) } + allLocations.map { bucketStart(it.observedAt) })
            .filter { it in dayStart until dayEnd }.distinct().sorted()
        if (occupied.isEmpty()) return emptyList()
        return occupied.map { start ->
            val bucketSteps = allSteps.filter { it.observedAt in start until start + BUCKET_MILLIS }
            val delta = bucketSteps.lastOrNull()?.let { latest ->
                allSteps.lastOrNull { it.observedAt < latest.observedAt }
                    ?.takeIf {
                        it.sessionId == latest.sessionId &&
                            latest.observedAt > it.observedAt &&
                            latest.observedAt - it.observedAt <= MAX_STEP_GAP_MILLIS
                    }?.let {
                    (latest.counterValue - it.counterValue).takeIf { value -> value >= 0L }
                }
            }
            val bucketLocations = allLocations.filter { it.observedAt in start until start + BUCKET_MILLIS }
            val precedingLocation = allLocations.lastOrNull { it.observedAt < start }
            Interval(classify(delta, precedingLocation, bucketLocations.lastOrNull()), start, minOf(start + BUCKET_MILLIS, dayEnd))
        }.fold(mutableListOf()) { merged, interval ->
            val previous = merged.lastOrNull()
            if (previous != null && previous.label == interval.label && previous.endedAt == interval.startedAt) {
                merged[merged.lastIndex] = previous.copy(endedAt = interval.endedAt)
            } else merged += interval
            merged
        }
    }

    fun classify(stepDelta: Long?, previous: LocationObservation?, current: LocationObservation?): Label {
        if (stepDelta != null && stepDelta >= RUNNING_STEPS_PER_BUCKET) return Label.RUNNING
        if (stepDelta != null && stepDelta >= WALKING_STEPS_PER_BUCKET) return Label.WALKING
        if (stepDelta != null && stepDelta > 0) return Label.UNKNOWN
        if (stepDelta != 0L || previous == null || current == null ||
            previous.accuracyMeters > RELIABLE_ACCURACY_METERS || current.accuracyMeters > RELIABLE_ACCURACY_METERS) return Label.UNKNOWN
        val elapsedSeconds = (current.observedAt - previous.observedAt) / 1000.0
        if (elapsedSeconds <= 0.0) return Label.UNKNOWN
        if (current.observedAt - previous.observedAt > MAX_LOCATION_GAP_MILLIS) return Label.UNKNOWN
        val distance = distanceMeters(previous, current)
        val uncertainty = max(previous.accuracyMeters.toDouble(), current.accuracyMeters.toDouble())
        val significantDistance = max(MIN_TRANSPORT_DISTANCE_METERS, uncertainty * 2)
        return when {
            distance >= significantDistance && distance / elapsedSeconds >= MIN_TRANSPORT_SPEED_MPS -> Label.TRANSPORT
            distance <= max(STATIONARY_DISTANCE_METERS, uncertainty) -> Label.STATIONARY
            else -> Label.UNKNOWN
        }
    }

    fun dayBounds(date: java.time.LocalDate, zone: ZoneId): LongRange {
        val start = date.atStartOfDay(zone).toInstant().toEpochMilli()
        return start until date.plusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()
    }

    private fun bucketStart(timestamp: Long) = timestamp / BUCKET_MILLIS * BUCKET_MILLIS
    private fun distanceMeters(a: LocationObservation, b: LocationObservation): Double {
        val earth = 6_371_000.0
        val lat1 = Math.toRadians(a.latitude); val lat2 = Math.toRadians(b.latitude)
        val dLat = lat2 - lat1; val dLon = Math.toRadians(b.longitude - a.longitude)
        val h = kotlin.math.sin(dLat / 2) * kotlin.math.sin(dLat / 2) +
            kotlin.math.cos(lat1) * kotlin.math.cos(lat2) * kotlin.math.sin(dLon / 2) * kotlin.math.sin(dLon / 2)
        return 2 * earth * kotlin.math.asin(kotlin.math.sqrt(h))
    }
}
