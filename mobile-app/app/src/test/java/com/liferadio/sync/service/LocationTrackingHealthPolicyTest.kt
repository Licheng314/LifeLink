package com.liferadio.sync.service

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocationTrackingHealthPolicyTest {
    @Test fun doesNotMarkDisabledTrackingAsStale() {
        assertFalse(LocationTrackingHealthPolicy.isStale(false, 0L, 100L))
    }

    @Test fun marksMissingOrExpiredAcceptedFixAsStale() {
        assertTrue(LocationTrackingHealthPolicy.isStale(true, 0L, 100L))
        assertTrue(LocationTrackingHealthPolicy.isStale(true, 1L, 1L + LocationTrackingHealthPolicy.STALE_AFTER_MILLIS + 1L))
        assertFalse(LocationTrackingHealthPolicy.isStale(true, 1L, 1L + LocationTrackingHealthPolicy.STALE_AFTER_MILLIS))
    }
}
