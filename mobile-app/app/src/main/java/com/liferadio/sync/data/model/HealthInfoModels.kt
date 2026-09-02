package com.liferadio.sync.data.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

/** Read-only, centrally derived health information. It is never reconstructed locally. */
@JsonClass(generateAdapter = true)
data class CentralHealthInfo(
    @Json(name = "date") val date: String,
    @Json(name = "timezone") val timezone: String,
    @Json(name = "sleep") val sleep: CentralSleepReference,
    @Json(name = "steps") val steps: CentralStepsSummary
)

@JsonClass(generateAdapter = true)
data class CentralSleepReference(
    @Json(name = "status") val status: String,
    @Json(name = "window_start") val windowStart: String,
    @Json(name = "window_end") val windowEnd: String,
    @Json(name = "estimated_start") val estimatedStart: String?,
    @Json(name = "estimated_end") val estimatedEnd: String?,
    @Json(name = "finalized_at") val finalizedAt: String?,
    @Json(name = "interval_seconds") val intervalSeconds: Long?,
    @Json(name = "rest_seconds") val restSeconds: Long?,
    @Json(name = "interruption_seconds") val interruptionSeconds: Long?,
    @Json(name = "last_activity_at") val lastActivityAt: String?,
    @Json(name = "first_activity_at") val firstActivityAt: String?,
    @Json(name = "last_activity_devices") val lastActivityDevices: List<CentralHealthDevice>,
    @Json(name = "first_activity_devices") val firstActivityDevices: List<CentralHealthDevice>,
    @Json(name = "contributing_device_ids") val contributingDeviceIds: List<String>,
    @Json(name = "warnings") val warnings: List<String>
)

@JsonClass(generateAdapter = true)
data class CentralHealthDevice(
    @Json(name = "device_id") val deviceId: String,
    @Json(name = "display_name") val displayName: String,
    @Json(name = "platform") val platform: String
)

@JsonClass(generateAdapter = true)
data class CentralStepsSummary(@Json(name = "devices") val devices: List<CentralStepDevice>)

@JsonClass(generateAdapter = true)
data class CentralStepDevice(
    @Json(name = "device_id") val deviceId: String,
    @Json(name = "display_name") val displayName: String,
    @Json(name = "status") val status: String,
    @Json(name = "steps") val steps: Long?,
    @Json(name = "sample_count") val sampleCount: Int,
    @Json(name = "first_sample_at") val firstSampleAt: String?,
    @Json(name = "last_sample_at") val lastSampleAt: String?,
    /** Present only on central versions that provide the fixed Shanghai 0..23 hourly split. */
    @Json(name = "hourly_steps") val hourlySteps: List<Long>?,
    @Json(name = "warnings") val warnings: List<String>
)
