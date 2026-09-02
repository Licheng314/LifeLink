package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.CentralHealthDevice
import com.liferadio.sync.data.model.CentralHealthInfo
import com.liferadio.sync.data.model.CentralSleepReference
import com.liferadio.sync.data.model.CentralStepDevice
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.time.Instant
import java.time.LocalDate
import java.util.concurrent.TimeUnit

sealed interface HealthInfoFetchResult {
    data class Success(val healthInfo: CentralHealthInfo, val rawResponse: String) : HealthInfoFetchResult
    data class Failure(val statusCode: Int?, val reason: String) : HealthInfoFetchResult
}

object CentralHealthInfoValidator {
    fun validate(response: CentralHealthInfo, requestedDate: String? = null): String? {
        if ((requestedDate != null && response.date != requestedDate) || runCatching { LocalDate.parse(response.date) }.isFailure) return "date is invalid"
        if (response.timezone != "Asia/Shanghai") return "timezone is not Asia/Shanghai"
        validateSleep(response.sleep)?.let { return it }
        response.steps.devices.forEach { device -> validateSteps(device)?.let { return it } }
        return null
    }

    private fun validateSleep(sleep: CentralSleepReference): String? {
        if (sleep.status !in setOf("estimating", "final", "insufficient_data")) return "sleep status is invalid"
        if (!isInstant(sleep.windowStart) || !isInstant(sleep.windowEnd)) return "sleep window is invalid"
        val dates = listOf(sleep.estimatedStart, sleep.estimatedEnd, sleep.finalizedAt, sleep.lastActivityAt, sleep.firstActivityAt)
        if (dates.any { it != null && !isInstant(it) }) return "sleep timestamp is invalid"
        val durations = listOf(sleep.intervalSeconds, sleep.restSeconds, sleep.interruptionSeconds)
        if (durations.any { it != null && it < 0 }) return "sleep duration is invalid"
        if (!validDevices(sleep.lastActivityDevices) || !validDevices(sleep.firstActivityDevices)) return "sleep devices are invalid"
        if (sleep.contributingDeviceIds.any(String::isBlank) || sleep.warnings.any(String::isBlank)) return "sleep lists are invalid"
        if (sleep.status == "final") {
            if (sleep.estimatedStart == null || sleep.estimatedEnd == null || sleep.finalizedAt == null ||
                sleep.intervalSeconds == null || sleep.restSeconds == null || sleep.interruptionSeconds == null ||
                sleep.lastActivityAt == null || sleep.firstActivityAt == null) return "final sleep is incomplete"
            if (Instant.parse(sleep.finalizedAt) != Instant.parse(sleep.estimatedEnd)) return "finalized_at differs from estimated_end"
        }
        return null
    }

    private fun validateSteps(device: CentralStepDevice): String? {
        if (device.deviceId.isBlank() || device.displayName.isBlank()) return "step device is invalid"
        if (device.status !in setOf("available", "insufficient_samples")) return "step status is invalid"
        if (device.sampleCount < 1 || device.steps?.let { it < 0 } == true) return "step values are invalid"
        if (device.status == "available" && device.steps == null) return "available steps are missing"
        device.hourlySteps?.let { hourly ->
            val validHourly = when (device.status) {
                "available" -> device.steps != null && hourly.sum() == device.steps
                "insufficient_samples" -> device.steps == null && hourly.all { it == 0L }
                else -> false
            }
            if (hourly.size != 24 || hourly.any { it < 0L } || !validHourly) {
                return "hourly steps are invalid"
            }
        }
        if ((device.firstSampleAt != null && !isInstant(device.firstSampleAt)) ||
            (device.lastSampleAt != null && !isInstant(device.lastSampleAt)) || device.warnings.any(String::isBlank)) return "step timestamps are invalid"
        return null
    }

    private fun validDevices(devices: List<CentralHealthDevice>) = devices.all {
        it.deviceId.isNotBlank() && it.displayName.isNotBlank() && it.platform in setOf("android", "desktop", "web")
    }

    private fun isInstant(value: String): Boolean = runCatching { Instant.parse(value) }.isSuccess
}

/** Stable display-only choice; central data is never combined across devices. */
object CentralStepDeviceSelector {
    fun defaultDevice(devices: List<CentralStepDevice>): CentralStepDevice? = devices.sortedWith(
        compareBy<CentralStepDevice>(
            { if (it.status == "available") 0 else 1 },
        ).thenByDescending { it.sampleCount }
            .thenByDescending { it.lastSampleAt?.let { value -> runCatching { Instant.parse(value).toEpochMilli() }.getOrDefault(Long.MIN_VALUE) } ?: Long.MIN_VALUE }
            .thenBy { it.deviceId }
    ).firstOrNull()
}

class CentralHealthInfoClient(
    baseUrl: String,
    private val tokenProvider: () -> String,
    private val client: OkHttpClient = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS).build()
) {
    private val baseEndpoint = requireNotNull(baseUrl.toHttpUrlOrNull()) { "Central Base URL must be an absolute URL" }
        .also { require(it.isHttps) { "Central Base URL must use HTTPS" } }
        .newBuilder().addPathSegments("v1/health-info").build()
    private val adapter = Moshi.Builder().add(KotlinJsonAdapterFactory()).build().adapter(CentralHealthInfo::class.java)

    suspend fun fetch(date: String): HealthInfoFetchResult = withContext(Dispatchers.IO) {
        val token = tokenProvider()
        if (token.isBlank()) return@withContext HealthInfoFetchResult.Failure(401, "central token is missing")
        try {
            client.newCall(createRequest(token, date)).execute().use { response ->
                if (!response.isSuccessful) return@use HealthInfoFetchResult.Failure(response.code, "central returned HTTP ${response.code}")
                val body = response.body?.string().orEmpty()
                val data = parseValidated(body, date) ?: return@use HealthInfoFetchResult.Failure(response.code, "health information response is invalid")
                HealthInfoFetchResult.Success(data, body)
            }
        } catch (_: IOException) {
            HealthInfoFetchResult.Failure(null, "central connection failed")
        }
    }

    internal fun createRequest(token: String, date: String): Request = Request.Builder()
        .url(baseEndpoint.newBuilder().addQueryParameter("date", date).build())
        .header("Authorization", "Bearer $token")
        .get()
        .build()

    internal fun parseValidated(body: String, requestedDate: String? = null): CentralHealthInfo? = runCatching {
        adapter.fromJson(body)
    }.getOrNull()?.takeIf { CentralHealthInfoValidator.validate(it, requestedDate) == null }
}
