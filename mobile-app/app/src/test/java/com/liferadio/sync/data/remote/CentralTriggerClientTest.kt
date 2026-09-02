package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.EventTrigger
import com.liferadio.sync.data.model.EventTriggerCreate
import com.liferadio.sync.data.model.EventTriggerPatch
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CentralTriggerClientTest {
    private class FakeServer(var code: Int, var json: String) : Interceptor {
        lateinit var request: Request
        var requested: Boolean = false

        override fun intercept(chain: Interceptor.Chain): Response {
            request = chain.request()
            requested = true
            return Response.Builder()
                .request(request)
                .protocol(Protocol.HTTP_1_1)
                .code(code)
                .message("test")
                .body(json.toResponseBody("application/json".toMediaType()))
                .build()
        }
    }

    private fun client(fake: FakeServer, token: String = "device-token") = CentralTriggerClient(
        baseUrl = "https://central.example/",
        tokenProvider = { token },
        client = OkHttpClient.Builder().addInterceptor(fake).build()
    )

    @Test
    fun listCatalogParsesContractShapeAndUsesDeviceToken() = runBlocking {
        val fake = FakeServer(200, """
            {"trigger_types":[{"trigger_type":"late_usage_milestone","display_name":"Late usage milestone","config_version":1,"target_scopes":["wish"],"interval_minutes":{"minimum":1,"allowed_values":[15,30,60,120]},"parameters_schema":{"required":["device_id","start_local_time"]}}]}
        """.trimIndent())

        val result = client(fake).listTriggerTypes()

        assertTrue(result is TriggerResult.Success)
        assertEquals("/v1/trigger-types", fake.request.url.encodedPath)
        assertEquals("Bearer device-token", fake.request.header("Authorization"))
    }

    @Test
    fun createTriggerSendsWishAssociationAndStableRequestId() = runBlocking {
        val response = """
            {"trigger_id":"00000000-0000-4000-8000-000000000003","wish_id":"00000000-0000-4000-8000-000000000002","trigger_type":"blacklist_usage_milestone","config_version":1,"parameters":{"platform_scope":"all"},"interval_minutes":60,"enabled":true,"created_at":"2026-08-09T00:00:00Z","updated_at":"2026-08-09T00:00:00Z","last_triggered_at":null}
        """.trimIndent()
        val fake = FakeServer(201, response)
        val requestId = "00000000-0000-4000-8000-000000000001"

        val result = client(fake).createEventTrigger(
            EventTriggerCreate(
                requestId = requestId,
                wishId = "00000000-0000-4000-8000-000000000002",
                triggerType = "blacklist_usage_milestone",
                parameters = mapOf("platform_scope" to "all"),
                intervalMinutes = 60
            )
        )

        assertTrue(result is TriggerResult.Success)
        assertEquals(requestId, fake.request.bodyTextValue("request_id"))
        assertEquals("00000000-0000-4000-8000-000000000002", fake.request.bodyTextValue("wish_id"))
        assertEquals("POST", fake.request.method)
        assertTrue((result as TriggerResult.Success).data is EventTrigger)
    }

    @Test
    fun scheduledReminderCreateAndUpdateSendFixedDummyIntervalOne() = runBlocking {
        val response = """{"trigger_id":"00000000-0000-4000-8000-000000000003","wish_id":"00000000-0000-4000-8000-000000000002","trigger_type":"scheduled_reminder","config_version":1,"parameters":{"reminder_local_time":"22:30"},"interval_minutes":1,"enabled":true,"created_at":"2026-08-09T00:00:00Z","updated_at":"2026-08-09T00:00:00Z","last_triggered_at":null}"""
        val fake = FakeServer(200, response)
        client(fake).createEventTrigger(EventTriggerCreate("request-1", "wish-1", "scheduled_reminder", parameters = mapOf("reminder_local_time" to "22:30"), intervalMinutes = 1))
        assertTrue(fake.request.bodyTextValue("interval_minutes") == "1")
        client(fake).updateEventTrigger("trigger-1", EventTriggerPatch(intervalMinutes = 1))
        assertTrue(fake.request.bodyTextValue("interval_minutes") == "1")
    }

    @Test
    fun disableAndReenableUsePostTransportOnTheRetainedTrigger() = runBlocking {
        val response = """
            {"trigger_id":"00000000-0000-4000-8000-000000000003","wish_id":"00000000-0000-4000-8000-000000000002","trigger_type":"blacklist_usage_milestone","config_version":1,"parameters":{"platform_scope":"all"},"interval_minutes":60,"enabled":false,"created_at":"2026-08-09T00:00:00Z","updated_at":"2026-08-10T00:00:00Z","last_triggered_at":null}
        """.trimIndent()
        val fake = FakeServer(200, response)

        val result = client(fake).updateEventTrigger(
            "00000000-0000-4000-8000-000000000003",
            EventTriggerPatch(enabled = false)
        )

        assertTrue(result is TriggerResult.Success)
        assertEquals("POST", fake.request.method)
        assertEquals("/v1/event-triggers/00000000-0000-4000-8000-000000000003", fake.request.url.encodedPath)
        val buffer = okio.Buffer()
        fake.request.body?.writeTo(buffer)
        assertTrue(buffer.readUtf8().contains("\"enabled\":false"))
    }

    @Test
    fun deleteUsesPostTransportCompatibilityPath() = runBlocking {
        val fake = FakeServer(204, "")

        val result = client(fake).deleteEventTrigger(
            "00000000-0000-4000-8000-000000000003"
        )

        assertTrue(result is TriggerResult.Success)
        assertEquals("POST", fake.request.method)
        assertEquals(
            "/v1/event-triggers/00000000-0000-4000-8000-000000000003/delete",
            fake.request.url.encodedPath
        )
    }

    @Test
    fun notFoundReturnsActionableDiagnostic() = runBlocking {
        val fake = FakeServer(404, """{"error":"not_found","message":"endpoint not found"}""")

        val result = client(fake).listEventTriggers()

        assertTrue(result is TriggerResult.Failure)
        val failure = result as TriggerResult.Failure
        assertEquals("not_found", failure.errorKey)
        assertTrue(failure.reason.contains("更新或重启中央服务"))
    }

    @Test
    fun missingTokenDoesNotSendRequest() = runBlocking {
        val fake = FakeServer(200, "{}")

        val result = client(fake, token = "").listEventTriggers()

        assertTrue(result is TriggerResult.Failure)
        assertEquals(401, (result as TriggerResult.Failure).statusCode)
        assertFalse(fake.requested)
    }

    @Test
    fun retryClassificationMatchesUncertainOutcomes() {
        assertTrue(CentralTriggerClient.isRetryable(null))
        assertTrue(CentralTriggerClient.isRetryable(408))
        assertTrue(CentralTriggerClient.isRetryable(429))
        assertTrue(CentralTriggerClient.isRetryable(503))
        assertFalse(CentralTriggerClient.isRetryable(400))
        assertFalse(CentralTriggerClient.isRetryable(409))
    }

    private fun Request.bodyTextValue(key: String): String? {
        val buffer = okio.Buffer()
        body?.writeTo(buffer)
        val pattern = Regex("\\\"$key\\\"\\s*:\\s*(?:\\\"([^\\\"]+)\\\"|([0-9]+))")
        return pattern.find(buffer.readUtf8())?.let { it.groupValues[1].ifBlank { it.groupValues[2] } }
    }
}
