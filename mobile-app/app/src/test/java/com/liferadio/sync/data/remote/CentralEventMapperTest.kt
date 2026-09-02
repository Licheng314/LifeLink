package com.liferadio.sync.data.remote

import com.liferadio.sync.data.local.DataEventEntity
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class CentralEventMapperTest {
    private val mapper = CentralEventMapper(
        Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
    )

    @Test
    fun mapsStepObservationWithStableWireIdAndCounterPayload() {
        val entity = DataEventEntity(
            id = "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            source = "android_step_counter", sourceType = "android",
            dataType = "health.steps_observation", timestamp = "2026-08-12T08:00:00Z",
            duration = 0, dataJson = "{\"counter_value\":123,\"counter_session_id\":\"3f2504e0-4f89-41d3-9a0c-0305e82c3301\",\"sensor_type\":\"android.step_counter\"}"
        )
        val first = mapper.map(entity)
        val second = mapper.map(entity)
        assertEquals("health.steps_observation", first.event.eventType)
        assertEquals("step_counter", first.event.source.collector)
        assertEquals(first.event.eventId, second.event.eventId)
        assertEquals(123L, first.event.payload["counter_value"])
    }

    @Test
    fun repairsV310EscapedStepPayloadWithoutChangingEventIdentity() {
        val entity = DataEventEntity(
            id = "c99929e0-9665-3a17-b546-4dceaf424f25",
            source = "android_step_counter", sourceType = "android",
            dataType = "health.steps_observation", timestamp = "2026-08-13T14:15:53Z",
            duration = 0,
            dataJson = """{\"counter_value\":3006,\"counter_session_id\":\"3f2504e0-4f89-41d3-9a0c-0305e82c3301\",\"sensor_type\":\"android.step_counter\"}"""
        )

        val mapped = mapper.map(entity)

        assertEquals(entity.id, mapped.event.eventId)
        assertEquals(3006L, mapped.event.payload["counter_value"])
        assertEquals("3f2504e0-4f89-41d3-9a0c-0305e82c3301", mapped.event.payload["counter_session_id"])
        assertEquals("android.step_counter", mapped.event.payload["sensor_type"])
    }

    @Test
    fun mapsLocationObservationAndRevision() {
        val entity = DataEventEntity(
            id = "0e0e8842-7606-45df-a4cc-230698bf5605",
            source = "android_fused_location",
            sourceType = "android",
            dataType = "location",
            timestamp = "2026-07-31T04:09:59Z",
            duration = 0,
            dataJson = """{"kind":"observation","latitude":29.5,"longitude":106.6}""",
            revision = 42L
        )

        val item = mapper.map(entity)

        assertEquals(entity.id, item.event.eventId)
        assertEquals("location.observation", item.event.eventType)
        assertEquals("fused_location", item.event.source.collector)
        assertEquals(42L, item.event.revision)
        assertEquals(29.5, item.event.payload["latitude"])
    }

    @Test
    fun convertsLegacyNonUuidIdDeterministically() {
        val entity = DataEventEntity(
            id = "aw_bucket_123",
            source = "activitywatch",
            sourceType = "android",
            dataType = "app_usage",
            timestamp = "2026-07-31T04:00:00Z",
            duration = 60,
            dataJson = "{}"
        )

        val first = mapper.map(entity).event.eventId
        val second = mapper.map(entity).event.eventId

        assertEquals(first, second)
        assertNotEquals(entity.id, first)
    }
}
