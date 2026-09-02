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

internal data class TimelineDayGroups(
    val today: List<TimelineEvent>,
    val yesterday: List<TimelineEvent>
) {
    val isEmpty: Boolean get() = today.isEmpty() && yesterday.isEmpty()
    val showDivider: Boolean get() = today.isNotEmpty() && yesterday.isNotEmpty()
}

internal fun timelineDayWindow(dayStartHour: Int, now: Instant): TimelineDayWindow {
    val today = EventBusinessDay.at(dayStartHour, now)
    return TimelineDayWindow(
        fromInclusive = today.minusDays(1)
            .atTime(dayStartHour, 0)
            .atZone(timelineZone)
            .toInstant(),
        toExclusive = now
    )
}

internal fun groupTodayAndYesterdayTimelineEvents(
    events: List<TimelineEvent>,
    dayStartHour: Int,
    now: Instant
): TimelineDayGroups {
    val window = timelineDayWindow(dayStartHour, now)
    val today = EventBusinessDay.at(dayStartHour, now)
    val yesterday = today.minusDays(1)
    val todayEvents = mutableListOf<TimelineEvent>()
    val yesterdayEvents = mutableListOf<TimelineEvent>()

    events.forEach { event ->
        val occurredAt = runCatching { Instant.parse(event.occurredAt) }.getOrNull() ?: return@forEach
        if (occurredAt < window.fromInclusive || occurredAt >= window.toExclusive) return@forEach
        when (EventBusinessDay.at(dayStartHour, occurredAt)) {
            today -> todayEvents += event
            yesterday -> yesterdayEvents += event
        }
    }

    return TimelineDayGroups(todayEvents, yesterdayEvents)
}

internal fun todayAndYesterdayTimelineEvents(
    events: List<TimelineEvent>,
    dayStartHour: Int,
    now: Instant
): List<TimelineEvent> {
    val groups = groupTodayAndYesterdayTimelineEvents(events, dayStartHour, now)
    return groups.today + groups.yesterday
}
