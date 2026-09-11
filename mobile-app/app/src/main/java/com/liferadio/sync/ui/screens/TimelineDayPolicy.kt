package com.liferadio.sync.ui.screens

import com.liferadio.sync.data.model.TimelineEvent
import com.liferadio.sync.data.model.EventBusinessDay
import java.time.Instant
import java.time.ZoneId

internal val timelineZone: ZoneId = ZoneId.of("Asia/Shanghai")

internal data class TimelineDayWindow(
    val fromInclusive: Instant,
    val toExclusive: Instant
)

internal fun timelineDayWindow(dayStartHour: Int, now: Instant): TimelineDayWindow {
    val today = EventBusinessDay.at(dayStartHour, now)
    return TimelineDayWindow(
        fromInclusive = today.atTime(dayStartHour, 0).atZone(timelineZone).toInstant(),
        toExclusive = now
    )
}

internal fun currentBusinessDayTimelineEvents(
    events: List<TimelineEvent>,
    dayStartHour: Int,
    now: Instant
): List<TimelineEvent> {
    val window = timelineDayWindow(dayStartHour, now)
    return events.filter { event ->
        val occurredAt = runCatching { Instant.parse(event.occurredAt) }.getOrNull() ?: return@filter false
        occurredAt >= window.fromInclusive && occurredAt < window.toExclusive
    }
}
