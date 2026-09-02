package com.liferadio.sync.ui.screens

import com.liferadio.sync.data.model.RealTimeBackgroundItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class EventBackgroundDisplayPolicyTest {
    @Test fun `fresh item keeps central wording`() {
        assertEquals("书房电脑在线", EventBackgroundDisplayPolicy.realTimeLabel(
            RealTimeBackgroundItem("device_online", "2026-08-16T10:00:00Z", false, true, "pc-1", "书房电脑在线")
        ))
    }

    @Test fun `stale device and app are omitted but other context keeps observed time`() {
        assertNull(EventBackgroundDisplayPolicy.realTimeLabel(
            RealTimeBackgroundItem("current_app", "2026-08-16T10:00:00Z", true, false, "pc-1", "上次使用 Chrome")
        ))
        assertEquals("上次活动状态为静止（上次更新：08-16 18:00）", EventBackgroundDisplayPolicy.realTimeLabel(
            RealTimeBackgroundItem("current_activity", "2026-08-16T10:00:00Z", true, false, "pc-1", "上次活动状态为静止")
        ))
    }
}
