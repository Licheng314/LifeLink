package com.liferadio.sync.ui.screens

import com.liferadio.sync.data.model.TimelineEvent
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Test

class TimelineDayPolicyTest {
    private val dayStartHour = 4
    private val now = Instant.parse("2026-09-01T04:00:00Z") // 2026-09-01 12:00 in Shanghai

    @Test
    fun queryWindowStartsAtYesterdayBusinessDayBoundaryInShanghai() {
        val window = timelineDayWindow(dayStartHour, now)

        assertEquals(Instant.parse("2026-08-30T20:00:00Z"), window.fromInclusive)
        assertEquals(now, window.toExclusive)
    }

    @Test
    fun groupsTodayBeforeYesterdayWhilePreservingOrderWithinEachDay() {
        val events = listOf(
            event("yesterday-newer", "2026-08-31T15:00:00Z"),
            event("today-newer", "2026-09-01T03:00:00Z"),
            event("today-older", "2026-08-31T20:30:00Z"),
            event("yesterday-older", "2026-08-30T20:10:00Z")
        )

        val groups = groupTodayAndYesterdayTimelineEvents(events, dayStartHour, now)

        assertEquals(listOf("today-newer", "today-older"), groups.today.map { it.timelineEventId })
        assertEquals(listOf("yesterday-newer", "yesterday-older"), groups.yesterday.map { it.timelineEventId })
        assertEquals(
            listOf("today-newer", "today-older", "yesterday-newer", "yesterday-older"),
            todayAndYesterdayTimelineEvents(events, dayStartHour, now).map { it.timelineEventId }
        )
    }

    @Test
    fun excludesEventsOutsideTheTwoShanghaiBusinessDays() {
        val events = listOf(
            event("before-yesterday", "2026-08-30T19:59:59Z"),
            event("yesterday", "2026-08-30T20:00:00Z"),
            event("today", "2026-08-31T20:00:00Z"),
            event("future", "2026-09-01T04:00:00Z"),
            event("invalid", "not-an-instant")
        )

        assertEquals(
            listOf("today", "yesterday"),
            todayAndYesterdayTimelineEvents(events, dayStartHour, now).map { it.timelineEventId }
        )
    }

    @Test
    fun dividerIsOnlyNeededWhenBothDaysHaveEvents() {
        val today = event("today", "2026-08-31T20:00:00Z")
        val yesterday = event("yesterday", "2026-08-30T20:00:00Z")

        assertEquals(false, groupTodayAndYesterdayTimelineEvents(listOf(today), dayStartHour, now).showDivider)
        assertEquals(false, groupTodayAndYesterdayTimelineEvents(listOf(yesterday), dayStartHour, now).showDivider)
        assertEquals(true, groupTodayAndYesterdayTimelineEvents(listOf(today, yesterday), dayStartHour, now).showDivider)
    }

    @Test
    fun beforeBoundaryStillBelongsToThePreviousCalendarDatesBusinessDay() {
        val beforeBoundaryNow = Instant.parse("2026-08-31T19:00:00Z") // 2026-09-01 03:00
        val events = listOf(
            event("today-before-boundary", "2026-08-31T18:30:00Z"),
            event("yesterday-before-boundary", "2026-08-30T18:30:00Z")
        )

        val groups = groupTodayAndYesterdayTimelineEvents(events, dayStartHour, beforeBoundaryNow)

        assertEquals(listOf("today-before-boundary"), groups.today.map { it.timelineEventId })
        assertEquals(listOf("yesterday-before-boundary"), groups.yesterday.map { it.timelineEventId })
    }

    @Test
    fun timeLabelsUseTheBusinessDayBoundary() {
        val beforeBoundaryNow = Instant.parse("2026-08-31T19:00:00Z") // 2026-09-01 03:00

        assertEquals(
            "今天 23:00",
            formatTimelineTime("2026-08-31T15:00:00Z", dayStartHour, beforeBoundaryNow)
        )
        assertEquals(
            "昨天 03:00",
            formatTimelineTime("2026-08-30T19:00:00Z", dayStartHour, beforeBoundaryNow)
        )
    }

    private fun event(id: String, occurredAt: String) = TimelineEvent(
        timelineEventId = id,
        occurredAt = occurredAt,
        createdAt = occurredAt,
        eventKey = "test.event",
        category = "system",
        importance = "normal",
        title = id,
        sourceKind = "system",
        dedupeKey = id
    )
}
