package com.liferadio.sync.data.remote

import com.liferadio.sync.data.local.SharedSettingsCache
import com.squareup.moshi.JsonReader
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import java.io.IOException
import java.time.Instant
import java.util.concurrent.TimeUnit
import okio.Buffer

data class CentralSharedSettings(
    val timezone: String,
    val day_start_hour: Int,
    val primary_health_device_id: String?,
    val sleep_local_time: String,
    val ai_display_name: String,
    val morning_report: MorningReportSchedule,
    val evening_report: FixedTimeReportSchedule,
    val periodic_summary: PeriodicSummarySchedule,
    val settings_version: Int,
    val updated_at: String
)

data class MorningReportSchedule(val enabled: Boolean, val mode: String, val delay_minutes: Int, val local_time: String?)
data class FixedTimeReportSchedule(val enabled: Boolean, val local_time: String)
data class PeriodicSummarySchedule(val enabled: Boolean, val start_local_time: String, val end_local_time: String, val interval_minutes: Int)
data class SharedSettingsPatch(
    val sleep_local_time: String? = null,
    val ai_display_name: String? = null,
    val morning_report: MorningReportSchedule? = null,
    val evening_report: FixedTimeReportSchedule? = null,
    val periodic_summary: PeriodicSummarySchedule? = null
)

sealed interface SharedSettingsFetchResult {
    data class Success(val settings: CentralSharedSettings) : SharedSettingsFetchResult
    data class Failure(val statusCode: Int?, val reason: String) : SharedSettingsFetchResult
}

object CentralSharedSettingsValidator {
    fun validate(settings: CentralSharedSettings): String? = when {
        settings.timezone != "Asia/Shanghai" -> "timezone is not Asia/Shanghai"
        settings.day_start_hour !in 0..23 -> "day_start_hour is outside 0..23"
        !isClock(settings.sleep_local_time) -> "sleep_local_time is invalid"
        settings.ai_display_name.trim().isEmpty() || settings.ai_display_name.length > 80 -> "ai_display_name is invalid"
        settings.morning_report.mode !in setOf("after_first_usage", "fixed_time") -> "morning_report mode is invalid"
        settings.morning_report.delay_minutes !in 1..720 -> "morning_report delay is invalid"
        settings.morning_report.mode == "fixed_time" && !isClock(settings.morning_report.local_time) -> "morning_report local_time is invalid"
        settings.morning_report.mode == "after_first_usage" && settings.morning_report.local_time != null -> "morning_report local_time is invalid"
        !isClock(settings.evening_report.local_time) -> "evening_report local_time is invalid"
        !isClock(settings.periodic_summary.start_local_time) || !isClock(settings.periodic_summary.end_local_time) -> "periodic_summary time is invalid"
        settings.periodic_summary.interval_minutes !in setOf(30, 60, 120, 180, 240) -> "periodic_summary interval is invalid"
        settings.settings_version < 1 -> "settings_version must be at least 1"
        runCatching { Instant.parse(settings.updated_at) }.isFailure -> "updated_at is not UTC RFC3339"
        !settings.updated_at.endsWith("Z") -> "updated_at is not UTC RFC3339"
        else -> null
    }

    fun isClock(value: String?): Boolean = value?.matches(Regex("(?:[01][0-9]|2[0-3]):[0-5][0-9]")) == true

    fun toCache(settings: CentralSharedSettings, refreshedAt: Long, fullResponseJson: String = ""): SharedSettingsCache {
        require(validate(settings) == null) { "shared settings must be validated before caching" }
        require(refreshedAt > 0L) { "successful refresh time must be positive" }
        return SharedSettingsCache(
            timezone = settings.timezone,
            dayStartHour = settings.day_start_hour,
            settingsVersion = settings.settings_version,
            updatedAt = settings.updated_at,
            lastSuccessfulRefreshAt = refreshedAt,
            fullResponseJson = fullResponseJson
        )
    }
}

class CentralSharedSettingsClient(
    baseUrl: String,
    private val tokenProvider: () -> String,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build(),
) {
    private val endpoint = requireNotNull(baseUrl.toHttpUrlOrNull()) {
        "Central Base URL must be an absolute URL"
    }.also { require(it.isHttps) { "Central Base URL must use HTTPS" } }
        .newBuilder().addPathSegments("v1/settings/shared").build()
    private val moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
    private val settingsAdapter = moshi.adapter(CentralSharedSettings::class.java)
    private val patchAdapter = moshi.adapter(SharedSettingsPatch::class.java)

    suspend fun fetch(): SharedSettingsFetchResult = withContext(Dispatchers.IO) {
        val token = tokenProvider()
        if (token.isBlank()) return@withContext SharedSettingsFetchResult.Failure(401, "central token is missing")
        val request = createRequest(token)
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    return@use SharedSettingsFetchResult.Failure(response.code, "central returned HTTP ${response.code}")
                }
                val settings = parseValidated(response.body?.string().orEmpty())
                    ?: return@use SharedSettingsFetchResult.Failure(response.code, "shared settings response is invalid")
                SharedSettingsFetchResult.Success(settings)
            }
        } catch (_: IOException) {
            SharedSettingsFetchResult.Failure(null, "central connection failed")
        }
    }

    suspend fun update(patch: SharedSettingsPatch): SharedSettingsFetchResult = withContext(Dispatchers.IO) {
        val token = tokenProvider()
        if (token.isBlank()) return@withContext SharedSettingsFetchResult.Failure(401, "central token is missing")
        if (patch == SharedSettingsPatch()) return@withContext SharedSettingsFetchResult.Failure(400, "shared settings patch is empty")
        val request = Request.Builder().url(endpoint).header("Authorization", "Bearer $token")
            .post(patchAdapter.toJson(patch).toRequestBody("application/json; charset=utf-8".toMediaType())).build()
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@use SharedSettingsFetchResult.Failure(response.code, "central returned HTTP ${response.code}")
                parseValidated(response.body?.string().orEmpty())?.let(SharedSettingsFetchResult::Success)
                    ?: SharedSettingsFetchResult.Failure(response.code, "shared settings response is invalid")
            }
        } catch (_: IOException) { SharedSettingsFetchResult.Failure(null, "central connection failed") }
    }

    internal fun createRequest(token: String): Request = Request.Builder()
        .url(endpoint)
        .header("Authorization", "Bearer $token")
        .get()
        .build()

    internal fun parseValidated(body: String): CentralSharedSettings? {
        // Reject unknown top-level members before Moshi maps the complete contract. This protects the
        // read-only cache from partial/legacy responses being mistaken for current central authority.
        val expected = setOf("timezone", "day_start_hour", "primary_health_device_id", "sleep_local_time", "ai_display_name", "morning_report", "evening_report", "periodic_summary", "settings_version", "updated_at")
        val keysValid = runCatching {
            JsonReader.of(Buffer().writeUtf8(body)).use { reader ->
                reader.beginObject(); val seen = mutableSetOf<String>()
                while (reader.hasNext()) {
                    val key = reader.nextName(); if (key !in expected || !seen.add(key)) return@use false
                    val nested = when (key) {
                        "morning_report" -> strictObject(reader, setOf("enabled", "mode", "delay_minutes", "local_time"))
                        "evening_report" -> strictObject(reader, setOf("enabled", "local_time"))
                        "periodic_summary" -> strictObject(reader, setOf("enabled", "start_local_time", "end_local_time", "interval_minutes"))
                        else -> { reader.skipValue(); true }
                    }
                    if (!nested) return@use false
                }
                reader.endObject(); reader.peek() == JsonReader.Token.END_DOCUMENT && seen == expected
            }
        }.getOrDefault(false)
        if (!keysValid) return null
        return runCatching { settingsAdapter.fromJson(body) }.getOrNull()
            ?.takeIf { CentralSharedSettingsValidator.validate(it) == null }
        /*
        val settings = runCatching {
            JsonReader.of(Buffer().writeUtf8(body)).use { reader ->
                var timezone: String? = null
                var dayStartHour: Int? = null
                var settingsVersion: Int? = null
                var updatedAt: String? = null
                reader.beginObject()
                while (reader.hasNext()) {
                    when (reader.nextName()) {
                        "timezone" -> {
                            if (timezone != null || reader.peek() != JsonReader.Token.STRING) return@use null
                            timezone = reader.nextString()
                        }
                        "day_start_hour" -> {
                            if (dayStartHour != null || reader.peek() != JsonReader.Token.NUMBER) return@use null
                            dayStartHour = parseJsonInteger(reader.nextString()) ?: return@use null
                        }
                        "settings_version" -> {
                            if (settingsVersion != null || reader.peek() != JsonReader.Token.NUMBER) return@use null
                            settingsVersion = parseJsonInteger(reader.nextString()) ?: return@use null
                        }
                        "updated_at" -> {
                            if (updatedAt != null || reader.peek() != JsonReader.Token.STRING) return@use null
                            updatedAt = reader.nextString()
                        }
                        else -> return@use null
                    }
                }
                reader.endObject()
                if (reader.peek() != JsonReader.Token.END_DOCUMENT) return@use null
                if (timezone == null || dayStartHour == null || settingsVersion == null || updatedAt == null) return@use null
                CentralSharedSettings(timezone, dayStartHour, settingsVersion, updatedAt)
            }
        }.getOrNull() ?: return null
        return settings.takeIf { CentralSharedSettingsValidator.validate(it) == null }
         */
    }

    private fun strictObject(reader: JsonReader, expected: Set<String>): Boolean {
        if (reader.peek() != JsonReader.Token.BEGIN_OBJECT) return false
        reader.beginObject(); val seen = mutableSetOf<String>()
        while (reader.hasNext()) { val key = reader.nextName(); if (key !in expected || !seen.add(key)) return false; reader.skipValue() }
        reader.endObject()
        return seen == expected
    }

    private fun parseJsonInteger(raw: String): Int? {
        if (!raw.matches(Regex("0|[1-9][0-9]*"))) return null
        return raw.toIntOrNull()
    }
}
