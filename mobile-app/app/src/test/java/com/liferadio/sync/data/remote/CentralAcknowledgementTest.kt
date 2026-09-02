package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.CentralBatchAcknowledgement
import com.liferadio.sync.data.model.CentralDevice
import com.liferadio.sync.data.model.CentralEvent
import com.liferadio.sync.data.model.CentralEventBatch
import com.liferadio.sync.data.model.CentralEventResult
import com.liferadio.sync.data.model.CentralEventSource
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class CentralAcknowledgementTest {
    private val eventOne = event("0d83ab2a-9fc4-462f-a0e1-d8873f022496", 3L)
    private val eventTwo = event("2d58cc06-3a7e-4650-8109-2e6ba06d1db5", 7L)
    private val batch = CentralEventBatch(
        batchId = "b567d5ac-f939-4f38-8534-d33dfb02be54",
        device = CentralDevice("android-install-test", displayName = "Test"),
        sentAt = "2026-07-31T04:00:00Z",
        events = listOf(eventOne, eventTwo)
    )

    @Test
    fun requiresConfirmedEventIds() {
        val acknowledgement = CentralBatchAcknowledgement(
            batchId = batch.batchId,
            confirmedEventIds = null
        )

        assertNotNull(CentralAcknowledgementValidator.validate(batch, acknowledgement))
    }

    @Test
    fun acceptsOnlyIdsFromTheRequest() {
        val acknowledgement = CentralBatchAcknowledgement(
            batchId = batch.batchId,
            confirmedEventIds = listOf(eventOne.eventId)
        )

        assertNull(CentralAcknowledgementValidator.validate(batch, acknowledgement))
    }

    @Test
    fun selectorLeavesRejectedAndUnconfirmedEventsPending() {
        val items = listOf(
            CentralBatchItem("local-one", 3L, eventOne),
            CentralBatchItem("local-two", 7L, eventTwo)
        )
        val acknowledgement = CentralBatchAcknowledgement(
            batchId = batch.batchId,
            confirmedEventIds = listOf(eventOne.eventId),
            eventResults = listOf(
                CentralEventResult(eventOne.eventId, "stored"),
                CentralEventResult(eventTwo.eventId, "rejected", "invalid_event")
            )
        )

        val confirmed = CentralConfirmationSelector.confirmedItems(items, acknowledgement)

        assertEquals(listOf("local-one"), confirmed.map { it.localEventId })
        assertEquals(listOf(3L), confirmed.map { it.localRevision })
    }

    private fun event(id: String, revision: Long) = CentralEvent(
        eventId = id,
        occurredAt = "2026-07-31T04:00:00Z",
        eventType = "custom.event",
        source = CentralEventSource(collector = "life_radio_app"),
        durationSeconds = 0,
        revision = revision,
        payload = mapOf("event_key" to "test", "title" to "Test")
    )
}
