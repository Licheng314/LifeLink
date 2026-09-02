package com.liferadio.sync.data.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import com.liferadio.sync.data.model.CentralStepDevice

class CentralHealthInfoClientTest {
    private val date = "2026-08-12"
    private val valid = """
        {"date":"2026-08-12","timezone":"Asia/Shanghai","sleep":{"status":"final","window_start":"2026-08-11T13:00:00Z","window_end":"2026-08-12T04:00:00Z","estimated_start":"2026-08-11T15:05:00Z","estimated_end":"2026-08-11T22:20:00Z","finalized_at":"2026-08-11T22:20:00Z","interval_seconds":26100,"rest_seconds":24900,"interruption_seconds":1200,"last_activity_at":"2026-08-11T15:05:00Z","first_activity_at":"2026-08-11T22:20:00Z","last_activity_devices":[{"device_id":"desktop-1","display_name":"书房电脑","platform":"desktop"}],"first_activity_devices":[{"device_id":"android-1","display_name":"手机","platform":"android"}],"contributing_device_ids":["desktop-1","android-1"],"warnings":[]},"steps":{"devices":[{"device_id":"android-1","display_name":"手机","status":"available","steps":6421,"sample_count":18,"first_sample_at":"2026-08-11T16:04:00Z","last_sample_at":"2026-08-12T15:55:00Z","warnings":[]},{"device_id":"android-2","display_name":"备用手机","status":"insufficient_samples","steps":null,"sample_count":1,"first_sample_at":"2026-08-12T01:00:00Z","last_sample_at":"2026-08-12T01:00:00Z","warnings":[]}]}}
    """.trimIndent()

    @Test
    fun createsAuthenticatedShanghaiDateRequest() {
        val request = CentralHealthInfoClient("https://central.example.test", { "unused" })
            .createRequest("token", date)

        assertEquals("GET", request.method)
        assertEquals("/v1/health-info", request.url.encodedPath)
        assertEquals(date, request.url.queryParameter("date"))
        assertEquals("Bearer token", request.header("Authorization"))
    }

    @Test
    fun acceptsFinalSleepWithBoundaryDevicesAndSeparateStepDevices() {
        val result = CentralHealthInfoClient("https://central.example.test", { "unused" }).parseValidated(valid, date)

        requireNotNull(result)
        assertEquals("2026-08-11T22:20:00Z", result.sleep.finalizedAt)
        assertEquals(listOf("书房电脑"), result.sleep.lastActivityDevices.map { it.displayName })
        assertEquals(listOf("手机"), result.sleep.firstActivityDevices.map { it.displayName })
        assertEquals(2, result.steps.devices.size)
        assertEquals(6421L, result.steps.devices.first().steps)
    }

    @Test
    fun rejectsCorruptedWrongDateAndInconsistentFinalResponses() {
        val client = CentralHealthInfoClient("https://central.example.test", { "unused" })
        assertNull(client.parseValidated("not json", date))
        assertNull(client.parseValidated(valid.replace("2026-08-12\",\"timezone", "2026-08-13\",\"timezone"), date))
        assertNull(client.parseValidated(valid.replace("\"finalized_at\":\"2026-08-11T22:20:00Z\"", "\"finalized_at\":\"2026-08-11T22:21:00Z\""), date))
        assertNull(client.parseValidated(valid.replace("\"steps\":6421", "\"steps\":-1"), date))
    }

    @Test
    fun acceptsEstimatingWithoutInventingAnInterval() {
        val estimating = valid
            .replace("\"status\":\"final\"", "\"status\":\"estimating\"")
            .replace("\"estimated_start\":\"2026-08-11T15:05:00Z\"", "\"estimated_start\":null")
            .replace("\"estimated_end\":\"2026-08-11T22:20:00Z\"", "\"estimated_end\":null")
            .replace("\"finalized_at\":\"2026-08-11T22:20:00Z\"", "\"finalized_at\":null")
            .replace("\"interval_seconds\":26100", "\"interval_seconds\":null")
            .replace("\"rest_seconds\":24900", "\"rest_seconds\":null")
            .replace("\"interruption_seconds\":1200", "\"interruption_seconds\":null")
            .replace("\"last_activity_at\":\"2026-08-11T15:05:00Z\"", "\"last_activity_at\":null")
            .replace("\"first_activity_at\":\"2026-08-11T22:20:00Z\"", "\"first_activity_at\":null")

        val result = CentralHealthInfoClient("https://central.example.test", { "unused" }).parseValidated(estimating, date)
        assertTrue(result?.sleep?.estimatedStart == null)
        assertTrue(result?.sleep?.intervalSeconds == null)
    }

    @Test
    fun strictlyValidatesHourlyStepsAndKeepsOldCentralCompatible() {
        val client = CentralHealthInfoClient("https://central.example.test", { "unused" })
        val hourly = (0..23).joinToString(",") { if (it == 23) "6421" else "0" }
        val firstWarning = "\"last_sample_at\":\"2026-08-12T15:55:00Z\",\"warnings\":[]"
        assertEquals(24, client.parseValidated(valid.replace(firstWarning, "\"last_sample_at\":\"2026-08-12T15:55:00Z\",\"hourly_steps\":[$hourly],\"warnings\":[]"), date)
            ?.steps?.devices?.first()?.hourlySteps?.size)
        val secondWarning = "\"last_sample_at\":\"2026-08-12T01:00:00Z\",\"warnings\":[]"
        assertEquals(24, client.parseValidated(valid.replace(secondWarning, "\"last_sample_at\":\"2026-08-12T01:00:00Z\",\"hourly_steps\":[${List(24) { 0 }.joinToString(",")}],\"warnings\":[]"), date)
            ?.steps?.devices?.last()?.hourlySteps?.size)
        assertNull(client.parseValidated(valid.replace(firstWarning, "\"last_sample_at\":\"2026-08-12T15:55:00Z\",\"hourly_steps\":[1,2],\"warnings\":[]"), date))
        assertNull(client.parseValidated(valid.replace(firstWarning, "\"last_sample_at\":\"2026-08-12T15:55:00Z\",\"hourly_steps\":[${List(24) { 1 }.joinToString(",")}],\"warnings\":[]"), date))
    }

    @Test
    fun selectsOneStableBestDeviceWithoutSumming() {
        fun device(id: String, status: String, samples: Int, last: String?) = CentralStepDevice(id, id, status, 100, samples, null, last, null, emptyList())
        val selected = CentralStepDeviceSelector.defaultDevice(listOf(
            device("z", "insufficient_samples", 99, "2026-08-12T02:00:00Z"),
            device("b", "available", 2, "2026-08-12T03:00:00Z"),
            device("a", "available", 2, "2026-08-12T03:00:00Z")
        ))
        assertEquals("a", selected?.deviceId)
    }
}
