package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.CentralBatchAcknowledgement
import com.liferadio.sync.data.model.CentralEventBatch
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.io.IOException
import java.util.concurrent.TimeUnit

sealed interface CentralUploadResult {
    data class Success(val acknowledgement: CentralBatchAcknowledgement) : CentralUploadResult
    data class AuthFailure(val statusCode: Int) : CentralUploadResult
    data class RetryableFailure(
        val statusCode: Int?,
        val retryAfterMillis: Long?,
        val reason: String
    ) : CentralUploadResult
    data class PermanentFailure(val statusCode: Int, val reason: String) : CentralUploadResult
    data class InvalidAcknowledgement(val reason: String) : CentralUploadResult
}

object CentralAcknowledgementValidator {
    fun validate(
        batch: CentralEventBatch,
        acknowledgement: CentralBatchAcknowledgement
    ): String? {
        if (acknowledgement.batchId != batch.batchId) {
            return "acknowledgement batch_id does not match"
        }
        val confirmed = acknowledgement.confirmedEventIds
            ?: return "acknowledgement is missing confirmed_event_ids"
        val requestedIds = batch.events.mapTo(mutableSetOf()) { it.eventId }
        if (confirmed.any { it !in requestedIds }) {
            return "confirmed_event_ids contains an event outside this batch"
        }
        if (confirmed.size != confirmed.toSet().size) {
            return "confirmed_event_ids contains duplicates"
        }
        return null
    }
}

object CentralRetryPolicy {
    fun backoffMillis(attemptCount: Int): Long {
        val seconds = minOf(3600L, maxOf(5L, 1L shl minOf(11, attemptCount + 1)))
        return seconds * 1000L
    }

    fun retryAfterMillis(value: String?): Long? =
        value?.trim()?.toLongOrNull()?.coerceAtLeast(0L)?.times(1000L)
}

interface CentralUploader {
    suspend fun upload(batch: CentralEventBatch): CentralUploadResult
}

class CentralSyncClient(
    baseUrl: String,
    private val tokenProvider: () -> String,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build(),
    moshi: Moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
) : CentralUploader {
    private val endpoint = requireNotNull(baseUrl.toHttpUrlOrNull()) {
        "Central Base URL must be an absolute URL"
    }.also { url ->
        require(url.isHttps) { "Central Base URL must use HTTPS" }
    }.newBuilder()
        .addPathSegments("v1/events/batches")
        .build()
    private val batchAdapter = moshi.adapter(CentralEventBatch::class.java)
    private val acknowledgementAdapter = moshi.adapter(CentralBatchAcknowledgement::class.java)

    override suspend fun upload(batch: CentralEventBatch): CentralUploadResult = withContext(Dispatchers.IO) {
        val token = tokenProvider()
        if (token.isBlank()) {
            return@withContext CentralUploadResult.AuthFailure(401)
        }
        val request = createRequest(batch, token)
        try {
            client.newCall(request).execute().use { response ->
                when {
                    response.code == 401 || response.code == 403 ->
                        CentralUploadResult.AuthFailure(response.code)
                    response.code == 429 -> CentralUploadResult.RetryableFailure(
                        statusCode = response.code,
                        retryAfterMillis = CentralRetryPolicy.retryAfterMillis(
                            response.header("Retry-After")
                        ),
                        reason = "central server rate limited the upload"
                    )
                    response.code in 500..599 -> CentralUploadResult.RetryableFailure(
                        statusCode = response.code,
                        retryAfterMillis = null,
                        reason = "central server returned HTTP ${response.code}"
                    )
                    !response.isSuccessful -> CentralUploadResult.PermanentFailure(
                        statusCode = response.code,
                        reason = "central server returned HTTP ${response.code}"
                    )
                    else -> {
                        val responseBody = response.body?.string().orEmpty()
                        val acknowledgement = runCatching {
                            acknowledgementAdapter.fromJson(responseBody)
                        }.getOrNull()
                            ?: return@use CentralUploadResult.InvalidAcknowledgement(
                                "central acknowledgement is not valid JSON"
                            )
                        CentralAcknowledgementValidator.validate(batch, acknowledgement)?.let {
                            return@use CentralUploadResult.InvalidAcknowledgement(it)
                        }
                        CentralUploadResult.Success(acknowledgement)
                    }
                }
            }
        } catch (_: IOException) {
            CentralUploadResult.RetryableFailure(
                statusCode = null,
                retryAfterMillis = null,
                reason = "central connection failed"
            )
        }
    }

    internal fun createRequest(batch: CentralEventBatch, token: String): Request {
        val body = batchAdapter.toJson(batch)
            .toRequestBody("application/json; charset=utf-8".toMediaType())
        return Request.Builder()
            .url(endpoint)
            .header("Authorization", "Bearer $token")
            .header("Idempotency-Key", batch.batchId)
            .post(body)
            .build()
    }
}
