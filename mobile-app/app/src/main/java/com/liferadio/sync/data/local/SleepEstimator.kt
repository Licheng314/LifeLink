package com.liferadio.sync.data.local

import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId

/**
 * Nightly sleep window estimator.
 *
 * Searches the local [21:00 → 12:00next] window for the longest continuous
 * device-idle gap, merging gaps that are separated by < 30 min of usage.
 */
object SleepEstimator {

    private val BEDTIME_WINDOW_START = LocalTime.of(21, 0)
    private val BEDTIME_WINDOW_END   = LocalTime.of(12, 0)
    private val MIN_GAP              = Duration.ofHours(1)
    private val MAX_USAGE_BETWEEN_GAPS = Duration.ofMinutes(30)

    data class SleepEstimate(
        /** Start of the estimated sleep window (epoch seconds). */
        val sleepStartEpoch: Long,
        /** End of the estimated sleep window (epoch seconds). */
        val sleepEndEpoch: Long,
        /** Total duration in seconds. */
        val durationSeconds: Long,
        /** Epoch seconds of the last active usage before the sleep gap. */
        val lastActiveBeforeEpoch: Long,
        /** Epoch seconds of the first active usage after the sleep gap. */
        val firstActiveAfterEpoch: Long,
        /** Human-readable label. */
        val label: String
    ) {
        val durationHours: Float get() = durationSeconds / 3600f

        companion object {
            fun nullForDate(date: LocalDate) = SleepEstimate(
                sleepStartEpoch = 0L,
                sleepEndEpoch = 0L,
                durationSeconds = 0L,
                lastActiveBeforeEpoch = 0L,
                firstActiveAfterEpoch = 0L,
                label = "暂无足够数据估算睡眠"
            )
        }
    }

    fun estimate(
        usageEvents: List<DataEventEntity>,
        now: Instant = Instant.now()
    ): SleepEstimate {
        val zone = ZoneId.systemDefault()
        val nowDt = now.atZone(zone)

        // Window: yesterday 21:00 → today 12:00 (or now if before 12:00)
        val windowStart = nowDt.toLocalDate().minusDays(1)
            .atTime(BEDTIME_WINDOW_START).atZone(zone).toInstant()
        val noonToday   = nowDt.toLocalDate().atTime(BEDTIME_WINDOW_END).atZone(zone).toInstant()
        val windowEnd   = if (now.isBefore(noonToday)) now else noonToday

        data class Interval(val start: Instant, val end: Instant)
        val intervals = usageEvents
            .mapNotNull { event ->
                val start = runCatching { Instant.parse(event.timestamp) }.getOrNull() ?: return@mapNotNull null
                val end = start.plusSeconds(event.duration.coerceAtLeast(0).toLong())
                if (end <= windowStart || start >= windowEnd) return@mapNotNull null
                Interval(maxOf(start, windowStart), minOf(end, windowEnd))
            }
            .sortedBy { it.start }

        if (intervals.isEmpty()) {
            val dur = Duration.between(windowStart, windowEnd)
            return SleepEstimate(
                sleepStartEpoch = windowStart.epochSecond,
                sleepEndEpoch   = windowEnd.epochSecond,
                durationSeconds = dur.seconds,
                lastActiveBeforeEpoch = 0L,    // no usage before = no data
                firstActiveAfterEpoch = 0L,    // no usage after = no data
                label = "完全不活跃，估算睡眠 ${formatDuration(dur)}"
            )
        }

        // merge overlapping
        val merged = mutableListOf(intervals.first())
        for (iv in intervals.drop(1)) {
            val last = merged.last()
            if (iv.start <= last.end) {
                merged[merged.lastIndex] = last.copy(end = maxOf(last.end, iv.end))
            } else merged.add(iv)
        }

        // extract gaps > 1h
        data class Gap(val start: Instant, val end: Instant, val duration: Duration)
        val gaps = mutableListOf<Gap>()

        // leading gap
        if (Duration.between(windowStart, merged.first().start) >= MIN_GAP) {
            gaps.add(Gap(windowStart, merged.first().start, Duration.between(windowStart, merged.first().start)))
        }
        // between
        for (i in 0 until merged.size - 1) {
            val d = Duration.between(merged[i].end, merged[i + 1].start)
            if (d >= MIN_GAP) gaps.add(Gap(merged[i].end, merged[i + 1].start, d))
        }
        // trailing
        val trailingDur = Duration.between(merged.last().end, windowEnd)
        if (trailingDur >= MIN_GAP) gaps.add(Gap(merged.last().end, windowEnd, trailingDur))

        if (gaps.isEmpty()) return SleepEstimate.nullForDate(nowDt.toLocalDate())

        // merge gaps with < 30 min usage between
        var bestStart = gaps.first().start
        var bestEnd   = gaps.first().end
        var bestDur   = gaps.first().duration
        var runStart  = bestStart
        var runEnd    = bestEnd
        var runDur    = bestDur

        for (i in 0 until gaps.size - 1) {
            val between = Duration.between(gaps[i].end, gaps[i + 1].start)
            if (between <= MAX_USAGE_BETWEEN_GAPS) {
                runEnd = gaps[i + 1].end
                runDur = Duration.between(runStart, runEnd)
            } else {
                runStart = gaps[i + 1].start
                runEnd   = gaps[i + 1].end
                runDur   = gaps[i + 1].duration
            }
            if (runDur > bestDur) {
                bestStart = runStart
                bestEnd   = runEnd
                bestDur   = runDur
            }
        }

        // Find last usage *before* the best gap, and first usage *after*
        // Find last usage *before* the best gap
        var lastActiveBefore = windowStart   // fallback: window start
        for (iv in merged) {
            if (iv.end <= bestStart) {
                lastActiveBefore = iv.end
            } else break
        }

        // Find first usage *after* the best gap
        var firstActiveAfter = windowEnd   // fallback: window end
        for (iv in merged) {
            if (iv.start >= bestEnd) {
                firstActiveAfter = iv.start
                break
            }
        }

        return SleepEstimate(
            sleepStartEpoch = bestStart.epochSecond,
            sleepEndEpoch   = bestEnd.epochSecond,
            durationSeconds = bestDur.seconds,
            lastActiveBeforeEpoch = lastActiveBefore.epochSecond,
            firstActiveAfterEpoch = firstActiveAfter.epochSecond,
            label = "估算睡眠 ${formatTime(bestStart, zone)} → ${formatTime(bestEnd, zone)}（${formatDuration(bestDur)}）"
        )
    }

    private fun formatDuration(d: Duration): String {
        val h = d.toHours()
        val m = d.toMinutes() % 60
        return "${h}h${m}m"
    }

    private fun formatTime(instant: Instant, zone: ZoneId): String {
        val dt = instant.atZone(zone)
        return String.format("%02d:%02d", dt.hour, dt.minute)
    }
}
