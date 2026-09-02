package com.liferadio.sync.data.local

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate
import java.time.ZoneId

class LocalActivityClassifierTest {
    private val start = 1_700_000_000_000L
    private fun location(at: Long, lat: Double, accuracy: Float = 10f) =
        LocalActivityClassifier.LocationObservation(at, lat, 121.0, accuracy)

    @Test fun classifiesAllFiveLabelsAndRejectsGpsDrift() {
        val previous = location(start, 31.0)
        assertEquals(LocalActivityClassifier.Label.RUNNING, LocalActivityClassifier.classify(600, previous, location(start + 300_000, 31.0001)))
        assertEquals(LocalActivityClassifier.Label.WALKING, LocalActivityClassifier.classify(10, previous, location(start + 300_000, 31.0001)))
        assertEquals(LocalActivityClassifier.Label.TRANSPORT, LocalActivityClassifier.classify(0, previous, location(start + 300_000, 31.01)))
        assertEquals(LocalActivityClassifier.Label.STATIONARY, LocalActivityClassifier.classify(0, previous, location(start + 300_000, 31.00005)))
        assertEquals(LocalActivityClassifier.Label.UNKNOWN, LocalActivityClassifier.classify(null, previous, null))
        assertEquals(LocalActivityClassifier.Label.UNKNOWN, LocalActivityClassifier.classify(0, previous, location(start + 300_000, 31.01, 120f)))
        assertEquals(LocalActivityClassifier.Label.UNKNOWN, LocalActivityClassifier.classify(0, previous, location(start + 11 * 60_000, 31.01)))
    }

    @Test fun mergesBucketsAndKeepsCrossDayBoundaries() {
        val zone = ZoneId.of("Asia/Shanghai")
        val date = LocalDate.of(2026, 8, 12)
        val dayStart = date.atStartOfDay(zone).toInstant().toEpochMilli()
        val dayEnd = date.plusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()
        val result = LocalActivityClassifier.classifyDay(
            steps = listOf(
                LocalActivityClassifier.StepObservation(dayStart + 60_000, 100, "a"),
                LocalActivityClassifier.StepObservation(dayStart + 240_000, 115, "a"),
                LocalActivityClassifier.StepObservation(dayStart + 360_000, 130, "a"),
                LocalActivityClassifier.StepObservation(dayStart + 540_000, 145, "a")
            ),
            locations = emptyList(), dayStart = dayStart, dayEnd = dayEnd
        )
        assertEquals(1, result.size)
        assertEquals(LocalActivityClassifier.Label.WALKING, result.single().label)
        assertEquals(dayStart + 10 * 60_000L, result.single().endedAt)
    }

    @Test fun missingCounterSessionEvidenceDoesNotCreateUploadOrActivity() {
        val dayStart = start / LocalActivityClassifier.BUCKET_MILLIS * LocalActivityClassifier.BUCKET_MILLIS
        val result = LocalActivityClassifier.classifyDay(
            steps = listOf(LocalActivityClassifier.StepObservation(dayStart + 60_000, 10, "first")),
            locations = emptyList(), dayStart = dayStart, dayEnd = dayStart + LocalActivityClassifier.BUCKET_MILLIS
        )
        assertEquals(LocalActivityClassifier.Label.UNKNOWN, result.single().label)
        assertTrue(result.all { it.label == LocalActivityClassifier.Label.UNKNOWN })
    }

    @Test fun longStepSamplingGapDoesNotPretendAccumulatedStepsWereRunning() {
        val dayStart = start / LocalActivityClassifier.BUCKET_MILLIS * LocalActivityClassifier.BUCKET_MILLIS
        val result = LocalActivityClassifier.classifyDay(
            steps = listOf(
                LocalActivityClassifier.StepObservation(dayStart + 60_000, 100, "same"),
                LocalActivityClassifier.StepObservation(dayStart + 61 * 60_000, 800, "same")
            ),
            locations = emptyList(), dayStart = dayStart, dayEnd = dayStart + 2 * 60 * 60_000L
        )
        assertEquals(LocalActivityClassifier.Label.UNKNOWN, result.last().label)
    }
}
