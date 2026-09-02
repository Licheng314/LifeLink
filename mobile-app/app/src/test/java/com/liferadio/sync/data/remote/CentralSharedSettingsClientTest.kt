package com.liferadio.sync.data.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Test

class CentralSharedSettingsClientTest {
    private val valid = CentralSharedSettings(
        timezone = "Asia/Shanghai",
        day_start_hour = 4,
        primary_health_device_id = null,
        sleep_local_time = "23:00",
        ai_display_name = "AI",
        morning_report = MorningReportSchedule(false, "after_first_usage", 60, null),
        evening_report = FixedTimeReportSchedule(false, "23:00"),
        periodic_summary = PeriodicSummarySchedule(false, "10:00", "22:00", 120),
        settings_version = 2,
        updated_at = "2026-08-09T01:02:03Z"
    )

    @Test
    fun createsAuthenticatedGetRequestForSharedSettings() {
        val request = CentralSharedSettingsClient("https://central.example.test", { "unused" })
            .createRequest("t".repeat(32))

        assertEquals("GET", request.method)
        assertEquals("/v1/settings/shared", request.url.encodedPath)
        assertEquals("Bearer ${"t".repeat(32)}", request.header("Authorization"))
    }

    @Test
    fun acceptsOnlyTheCentralSharedSettingsContract() {
        assertNull(CentralSharedSettingsValidator.validate(valid))
        assertTrue(CentralSharedSettingsValidator.validate(valid.copy(timezone = "UTC"))!!.contains("timezone"))
        assertTrue(CentralSharedSettingsValidator.validate(valid.copy(day_start_hour = 24))!!.contains("day_start_hour"))
        assertTrue(CentralSharedSettingsValidator.validate(valid.copy(settings_version = 0))!!.contains("settings_version"))
        assertTrue(CentralSharedSettingsValidator.validate(valid.copy(updated_at = "not-a-time"))!!.contains("updated_at"))
        assertTrue(CentralSharedSettingsValidator.validate(valid.copy(updated_at = "2026-08-09T01:02:03+08:00"))!!.contains("updated_at"))
    }

    @Test
    fun rejectsMalformedAndWronglyTypedResponsesBeforeTheyReachTheCache() {
        val client = CentralSharedSettingsClient("https://central.example.test", { "unused" })

        assertEquals(valid, client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":4,"primary_health_device_id":null,"sleep_local_time":"23:00","ai_display_name":"AI","morning_report":{"enabled":false,"mode":"after_first_usage","delay_minutes":60,"local_time":null},"evening_report":{"enabled":false,"local_time":"23:00"},"periodic_summary":{"enabled":false,"start_local_time":"10:00","end_local_time":"22:00","interval_minutes":120},"settings_version":2,"updated_at":"2026-08-09T01:02:03Z"}
        """.trimIndent()))
        assertNull(client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":"4","settings_version":2,"updated_at":"2026-08-09T01:02:03Z"}
        """.trimIndent()))
        assertNull(client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":4.5,"settings_version":2,"updated_at":"2026-08-09T01:02:03Z"}
        """.trimIndent()))
        assertNull(client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":4.0,"settings_version":2,"updated_at":"2026-08-09T01:02:03Z"}
        """.trimIndent()))
        assertNull(client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":4,"settings_version":2.0,"updated_at":"2026-08-09T01:02:03Z"}
        """.trimIndent()))
        assertNull(client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":4,"settings_version":2,"updated_at":"2026-08-09T01:02:03Z","extra":true}
        """.trimIndent()))
        assertNull(client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":4,"settings_version":2,"updated_at":"invalid"}
        """.trimIndent()))
        assertNull(client.parseValidated("""
            {"timezone":"Asia/Shanghai","day_start_hour":4,"primary_health_device_id":null,"sleep_local_time":"23:00","ai_display_name":"AI","morning_report":{"enabled":false,"mode":"after_first_usage","delay_minutes":60,"local_time":null,"unexpected":true},"evening_report":{"enabled":false,"local_time":"23:00"},"periodic_summary":{"enabled":false,"start_local_time":"10:00","end_local_time":"22:00","interval_minutes":120},"settings_version":2,"updated_at":"2026-08-09T01:02:03Z"}
        """.trimIndent()))
    }

    @Test
    fun convertsOnlyValidatedResponseToCache() {
        val cache = CentralSharedSettingsValidator.toCache(valid, refreshedAt = 1234L)

        assertEquals("Asia/Shanghai", cache.timezone)
        assertEquals(4, cache.dayStartHour)
        assertEquals(2, cache.settingsVersion)
        assertEquals(1234L, cache.lastSuccessfulRefreshAt)

        assertThrows(IllegalArgumentException::class.java) {
            CentralSharedSettingsValidator.toCache(valid.copy(timezone = "UTC"), refreshedAt = 1234L)
        }
        assertThrows(IllegalArgumentException::class.java) {
            CentralSharedSettingsValidator.toCache(valid, refreshedAt = 0L)
        }
    }
}
