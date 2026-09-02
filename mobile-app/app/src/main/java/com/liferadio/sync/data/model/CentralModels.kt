package com.liferadio.sync.data.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

@JsonClass(generateAdapter = true)
data class CentralDevice(
    @Json(name = "device_id") val deviceId: String,
    @Json(name = "platform") val platform: String = "android",
    @Json(name = "display_name") val displayName: String
)

@JsonClass(generateAdapter = true)
data class CentralEventSource(
    @Json(name = "kind") val kind: String = "android",
    @Json(name = "collector") val collector: String,
    @Json(name = "reliability") val reliability: String = "observed"
)

@JsonClass(generateAdapter = true)
data class CentralEvent(
    @Json(name = "event_id") val eventId: String,
    @Json(name = "occurred_at") val occurredAt: String,
    @Json(name = "event_type") val eventType: String,
    @Json(name = "source") val source: CentralEventSource,
    @Json(name = "duration_seconds") val durationSeconds: Int,
    @Json(name = "revision") val revision: Long,
    @Json(name = "payload") val payload: Map<String, Any?>
)

@JsonClass(generateAdapter = true)
data class CentralEventBatch(
    @Json(name = "schema_version") val schemaVersion: String = "v1",
    @Json(name = "batch_id") val batchId: String,
    @Json(name = "device") val device: CentralDevice,
    @Json(name = "sent_at") val sentAt: String,
    @Json(name = "events") val events: List<CentralEvent>
)

@JsonClass(generateAdapter = true)
data class CentralEventResult(
    @Json(name = "event_id") val eventId: String,
    @Json(name = "status") val status: String,
    @Json(name = "code") val code: String? = null,
    @Json(name = "message") val message: String? = null
)

@JsonClass(generateAdapter = true)
data class CentralRejectedEvent(
    @Json(name = "event_id") val eventId: String,
    @Json(name = "code") val code: String,
    @Json(name = "message") val message: String? = null
)

@JsonClass(generateAdapter = true)
data class CentralBatchAcknowledgement(
    @Json(name = "batch_id") val batchId: String,
    @Json(name = "accepted_event_ids") val acceptedEventIds: List<String> = emptyList(),
    @Json(name = "confirmed_event_ids") val confirmedEventIds: List<String>? = null,
    @Json(name = "duplicate_event_ids") val duplicateEventIds: List<String> = emptyList(),
    @Json(name = "event_results") val eventResults: List<CentralEventResult> = emptyList(),
    @Json(name = "rejected_events") val rejectedEvents: List<CentralRejectedEvent> = emptyList(),
    @Json(name = "received_at") val receivedAt: String? = null
)
