package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.WishPatch
import com.liferadio.sync.data.model.WishCreate
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class CentralWishMutationClientTest {
    private lateinit var server: MockWebServer

    @Before fun start() { server = MockWebServer().also { it.start() } }
    @After fun stop() { server.shutdown() }

    private fun client() = CentralWishClient(server.url("/").toString(), { "device-token" })

    @Test fun `update uses POST transport and sends only text with device authorization`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(wishJson()))
        val result = client().updateWish("wish-1", WishPatch("重新专注"))

        assertTrue(result is WishResult.Success)
        server.takeRequest().also { request ->
            assertEquals("POST", request.method)
            assertEquals("/v1/wishes/wish-1", request.path)
            assertEquals("Bearer device-token", request.getHeader("Authorization"))
            assertEquals("{\"text\":\"重新专注\"}", request.body.readUtf8())
        }
        Unit
    }

    @Test fun `delete uses POST transport and treats 204 and compatibility 404 as success`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(204))
        assertTrue(client().deleteWish("wish-1") is WishResult.Success)
        server.takeRequest().also { request ->
            assertEquals("POST", request.method)
            assertEquals("/v1/wishes/wish-1/delete", request.path)
        }

        server.enqueue(MockResponse().setResponseCode(404).setBody("{\"error\":\"wish_not_found\"}"))
        assertTrue(client().deleteWish("wish-1") is WishResult.Success)
        Unit
    }

    @Test fun `deleted wish creation retry is explicit failure`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(410).setBody("{\"error\":\"wish_deleted\"}"))
        val result = client().createWish(WishCreate("request-1", "retry text")) as WishResult.Failure
        assertEquals("wish_deleted", result.errorKey)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/wishes", request.path)
        Unit
    }

    @Test fun `complete uses dedicated POST action and parses archived wish`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(wishJson().replace("\"active\"", "\"archived\"")))
        val result = client().completeWish("wish-1")
        assertTrue(result is WishResult.Success)
        server.takeRequest().also { request ->
            assertEquals("POST", request.method)
            assertEquals("/v1/wishes/wish-1/complete", request.path)
            assertEquals("Bearer device-token", request.getHeader("Authorization"))
        }
        Unit
    }

    private fun wishJson() = """{
        "wish_id":"wish-1","text":"重新专注","duration_days":3,"status":"active",
        "created_at":"2026-08-10T00:00:00Z","starts_on":"2026-08-10","ends_on":"2026-08-12",
        "business_day_snapshot":{"timezone":"Asia/Shanghai","day_start_hour":4,"settings_version":1},
        "ai_tracking_enabled":false,"completed_days":0,"wish_days":[]
    }""".trimIndent()
}
