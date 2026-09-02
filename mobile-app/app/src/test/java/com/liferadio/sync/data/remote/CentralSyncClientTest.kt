package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.CentralDevice
import com.liferadio.sync.data.model.CentralEvent
import com.liferadio.sync.data.model.CentralEventBatch
import com.liferadio.sync.data.model.CentralEventSource
import org.junit.Assert.assertEquals
import org.junit.Test

class CentralSyncClientTest {
    @Test
    fun requestUsesCentralHeadersAndPath() {
        val client = CentralSyncClient("https://context.example.com", { "unused" })
        val batch = batch()

        val request = client.createRequest(batch, "t".repeat(32))

        assertEquals("https", request.url.scheme)
        assertEquals("/v1/events/batches", request.url.encodedPath)
        assertEquals("Bearer ${"t".repeat(32)}", request.header("Authorization"))
        assertEquals(batch.batchId, request.header("Idempotency-Key"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsCleartextCentralUrl() {
        CentralSyncClient("http://context.example.com", { "t".repeat(32) })
    }

    private fun batch() = CentralEventBatch(
        batchId = "b567d5ac-f939-4f38-8534-d33dfb02be54",
        device = CentralDevice("android-install-test", displayName = "Test"),
        sentAt = "2026-07-31T04:00:00Z",
        events = listOf(
            CentralEvent(
                eventId = "0d83ab2a-9fc4-462f-a0e1-d8873f022496",
                occurredAt = "2026-07-31T03:59:00Z",
                eventType = "custom.event",
                source = CentralEventSource(collector = "life_radio_app"),
                durationSeconds = 0,
                revision = 9,
                payload = mapOf("event_key" to "test", "title" to "Test")
            )
        )
    )
}
