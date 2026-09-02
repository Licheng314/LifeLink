package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.WishDay
import com.liferadio.sync.data.model.WishDayStatus
import com.liferadio.sync.data.model.SharedSettingsSnapshot
import com.liferadio.sync.data.model.EventBusinessDay
import com.liferadio.sync.data.model.WishBusinessDay
import org.junit.Assert.*
import org.junit.Test
import java.time.LocalDate
import java.time.Instant

class WishBusinessDayTest {

    private val snapshot = SharedSettingsSnapshot("Asia/Shanghai", 4, 1)

    @Test
    fun `before day_start_hour is previous calendar day`() {
        val result = WishBusinessDay.at(snapshot, Instant.parse("2026-08-08T19:00:00Z"))
        assertEquals(LocalDate.of(2026, 8, 8), result)
    }

    @Test
    fun `after day_start_hour is current calendar day`() {
        val result = WishBusinessDay.at(snapshot, Instant.parse("2026-08-08T21:00:00Z"))
        assertEquals(LocalDate.of(2026, 8, 9), result)
    }

    @Test
    fun `WishDay dayStatus UNREACHED for future date`() {
        val today = LocalDate.of(2026, 8, 9)
        val day = WishDay("2026-08-12", null, null, null, 0)
        assertEquals(WishDayStatus.UNREACHED, day.dayStatus(today))
    }

    @Test
    fun `WishDay dayStatus TODAY`() {
        val today = LocalDate.of(2026, 8, 9)
        val day = WishDay("2026-08-09", null, null, null, 0)
        assertEquals(WishDayStatus.TODAY, day.dayStatus(today))
    }

    @Test
    fun `WishDay dayStatus PAST_PENDING for overdue unevaluated`() {
        val today = LocalDate.of(2026, 8, 10)
        val day = WishDay("2026-08-09", null, null, null, 0)
        assertEquals(WishDayStatus.PAST_PENDING, day.dayStatus(today))
    }

    @Test
    fun `WishDay dayStatus COMPLETED overrides date`() {
        val today = LocalDate.of(2026, 8, 11)
        val day = WishDay("2026-08-09", "completed", "manual", "2026-08-09T00:00:00Z", 0)
        assertEquals(WishDayStatus.COMPLETED, day.dayStatus(today))
    }

    @Test
    fun `WishDay dayStatus NOT_COMPLETED overrides date`() {
        val today = LocalDate.of(2026, 8, 11)
        val day = WishDay("2026-08-09", "not_completed", "manual", "2026-08-09T00:00:00Z", 0)
        assertEquals(WishDayStatus.NOT_COMPLETED, day.dayStatus(today))
    }

    @Test
    fun `SharedSettingsSnapshot validates day_start_hour 0 to 23`() {
        val valid = SharedSettingsSnapshot("Asia/Shanghai", 0, 1)
        assertEquals(0, valid.dayStartHour)
        val last = SharedSettingsSnapshot("Asia/Shanghai", 23, 1)
        assertEquals(23, last.dayStartHour)
    }

    @Test
    fun `WishDay dayStatus UNKNOWN for invalid date`() {
        val day = WishDay("not-a-date", null, null, null, 0)
        assertEquals(WishDayStatus.UNKNOWN, day.dayStatus(LocalDate.of(2026, 8, 9)))
    }

    @Test
    fun `event background business day honors current shared boundary in Shanghai`() {
        assertEquals(LocalDate.of(2026, 8, 8), EventBusinessDay.at(4, Instant.parse("2026-08-08T19:00:00Z")))
        assertEquals(LocalDate.of(2026, 8, 9), EventBusinessDay.at(4, Instant.parse("2026-08-08T20:00:00Z")))
    }
}

class CentralAcknowledgementValidatorTest {

    @Test
    fun `matches expected error keys`() {
        assertTrue(CentralWishClient.isAuthFailure(401))
        assertTrue(CentralWishClient.isAuthFailure(403))
        assertFalse(CentralWishClient.isAuthFailure(404))
        assertFalse(CentralWishClient.isAuthFailure(null))
    }
}
