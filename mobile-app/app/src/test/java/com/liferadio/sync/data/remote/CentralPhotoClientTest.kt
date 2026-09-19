package com.liferadio.sync.data.remote

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Test

class CentralPhotoClientTest {
    @Test fun `delete uses post compatibility endpoint and sync id`() {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(200).setBody("{\"photo\":{\"photo_id\":\"a\",\"status\":\"deleted\"},\"changed\":true}"))
            server.start()
            val result = CentralPhotoClient(server.url("/").toString(), { "device-token" }).delete("a", "sync-1")
            assertEquals(true, (result as PhotoRequestResult.Success).value.changed)
            val request = server.takeRequest()
            assertEquals("POST", request.method)
            assertEquals("/v1/photos/a/delete", request.path)
            assertEquals("sync-1", request.getHeader("X-Photo-Sync-Id"))
            assertEquals("Bearer device-token", request.getHeader("Authorization"))
        }
    }
}
