package com.liferadio.sync.service

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TimelineNotificationPolicyTest {
    @Test
    fun `high and normal events notify while low events stay silent`() {
        assertTrue(TimelineNotificationPolicy.shouldNotify("high"))
        assertTrue(TimelineNotificationPolicy.shouldNotify("normal"))
        assertFalse(TimelineNotificationPolicy.shouldNotify("low"))
    }
}
