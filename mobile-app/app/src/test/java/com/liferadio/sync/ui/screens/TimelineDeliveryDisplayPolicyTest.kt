package com.liferadio.sync.ui.screens

import com.liferadio.sync.data.model.AIDeliveryState
import com.liferadio.sync.data.model.TimelineEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class TimelineDeliveryDisplayPolicyTest {
    private fun event(
        eventKey: String,
        detail: String?,
        delivery: AIDeliveryState? = null,
    ) = TimelineEvent(
        timelineEventId = "event-1",
        occurredAt = "2026-08-29T00:00:00Z",
        createdAt = "2026-08-29T00:00:00Z",
        eventKey = eventKey,
        category = "system",
        importance = "high",
        title = "测试事件",
        detail = detail,
        sourceKind = "system",
        delivery = delivery,
        dedupeKey = "event-1",
    )

    @Test
    fun reportWithDeliveryUsesOneComputedStatusLine() {
        val report = event(
            eventKey = "report.morning",
            detail = "今日早报已准备就绪。等待 Talo 接入。",
            delivery = AIDeliveryState("not_configured", "Talo", "2026-08-29T00:00:00Z"),
        )

        assertNull(timelineCardDetail(report))
        assertEquals("今日早报已准备就绪。等待 Talo 接入。", deliveryLabel(report))
    }

    @Test
    fun ordinaryEventKeepsItsDetail() {
        val ordinary = event("system.device_usage_milestone", "设备使用达到 1 小时")

        assertEquals("设备使用达到 1 小时", timelineCardDetail(ordinary))
    }
}
