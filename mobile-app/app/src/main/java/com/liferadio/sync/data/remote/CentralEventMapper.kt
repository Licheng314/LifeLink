package com.liferadio.sync.data.remote

import com.liferadio.sync.data.local.DataEventEntity
import com.liferadio.sync.data.model.CentralEvent
import com.liferadio.sync.data.model.CentralEventSource
import com.liferadio.sync.data.model.CentralBatchAcknowledgement
import com.squareup.moshi.Moshi
import java.nio.charset.StandardCharsets
import java.util.UUID

data class CentralBatchItem(
    val localEventId: String,
    val localRevision: Long,
    val event: CentralEvent
)

object CentralConfirmationSelector {
    fun confirmedItems(
        items: List<CentralBatchItem>,
        acknowledgement: CentralBatchAcknowledgement
    ): List<CentralBatchItem> {
        val byWireId = items.associateBy { it.event.eventId }
        return acknowledgement.confirmedEventIds.orEmpty().mapNotNull(byWireId::get)
    }
}

class CentralEventMapper(moshi: Moshi) {
    private val mapAdapter = moshi.adapter(Map::class.java)

    fun map(entity: DataEventEntity): CentralBatchItem {
        val parsedPayload = parsePayload(entity.dataJson, entity.dataType)
        val eventType = eventType(entity.dataType, parsedPayload)
        val payload = if (eventType == "health.steps_observation") {
            parsedPayload.toMutableMap().apply {
                val value = this["counter_value"]
                if (value is Double && value.isFinite() && value >= 0 && value % 1.0 == 0.0) {
                    this["counter_value"] = value.toLong()
                }
            }
        } else if (eventType == "custom.event" && entity.dataType !in CONTRACT_EVENT_TYPES) {
            mapOf(
                "event_key" to "legacy.${entity.dataType}",
                "title" to "Legacy ${entity.dataType} event",
                "metadata" to parsedPayload
            )
        } else {
            parsedPayload
        }
        return CentralBatchItem(
            localEventId = entity.id,
            localRevision = entity.revision,
            event = CentralEvent(
                eventId = wireEventId(entity.id),
                occurredAt = entity.timestamp,
                eventType = eventType,
                source = CentralEventSource(collector = collector(entity.source)),
                durationSeconds = entity.duration.coerceAtLeast(0),
                revision = entity.revision.coerceAtLeast(0),
                payload = payload
            )
        )
    }

    private fun parsePayload(json: String, dataType: String): Map<String, Any?> {
        val parsed = parseObject(json) ?: if (
            dataType == "health.steps_observation" && json.startsWith("{\\\"")
        ) {
            // v3.10 briefly persisted valid observations with JSON quotes
            // escaped at the document level. Repair only while serialising so
            // the immutable local observation and stable event id stay intact.
            parseObject(json.replace("\\\"", "\""))
        } else null
        return parsed?.entries?.associate { (key, value) -> key.toString() to value }.orEmpty()
    }

    private fun parseObject(json: String): Map<*, *>? =
        runCatching { mapAdapter.fromJson(json) }.getOrNull() as? Map<*, *>

    private fun eventType(dataType: String, payload: Map<String, Any?>): String = when (dataType) {
        "app_usage" -> "app.foreground"
        "location" -> when (payload["kind"]?.toString()) {
            "observation" -> "location.observation"
            "sample" -> "location.sample"
            "stay" -> "location.stay"
            "visit" -> "location.visit"
            else -> "location.observation"
        }
        "custom_event" -> "custom.event"
        in CONTRACT_EVENT_TYPES -> dataType
        else -> "custom.event"
    }

    private fun collector(source: String): String = when (source) {
        "android_usage_events" -> "usage_stats"
        "activitywatch" -> "activitywatch"
        "android_fused_location" -> "fused_location"
        "android_step_counter" -> "step_counter"
        else -> "life_radio_app"
    }

    private fun wireEventId(localEventId: String): String {
        val canonical = runCatching { UUID.fromString(localEventId).toString() }.getOrNull()
        return canonical ?: UUID.nameUUIDFromBytes(
            "life-radio-central|$localEventId".toByteArray(StandardCharsets.UTF_8)
        ).toString()
    }

    companion object {
        private val CONTRACT_EVENT_TYPES = setOf(
            "app.foreground",
            "custom.event",
            "calendar.event",
            "task.update",
            "location.observation",
            "location.sample",
            "location.stay",
            "location.visit",
            "manual.note",
            "health.steps_observation"
        )
    }
}
