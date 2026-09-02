package com.liferadio.sync.data.remote

import com.liferadio.sync.data.local.CentralTokenValidator
import com.liferadio.sync.service.CentralSyncCoordinator
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CentralRetryPolicyTest {
    @Test
    fun backoffIsBounded() {
        assertEquals(5_000L, CentralRetryPolicy.backoffMillis(0))
        assertEquals(2_048_000L, CentralRetryPolicy.backoffMillis(20))
    }

    @Test
    fun retryAfterSecondsAreHonored() {
        assertEquals(120_000L, CentralRetryPolicy.retryAfterMillis("120"))
    }

    @Test
    fun tokenAndBatchLimitsMatchCentralContract() {
        assertFalse(CentralTokenValidator.isValid("short"))
        assertTrue(CentralTokenValidator.isValid("a".repeat(32)))
        assertEquals(500, CentralSyncCoordinator.MAX_BATCH_SIZE)
        assertEquals("central", CentralSyncCoordinator.CENTRAL_TARGET_ID)
    }
}
