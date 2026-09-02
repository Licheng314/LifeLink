package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.EventTrigger
import com.liferadio.sync.data.model.EventTriggerCreate
import com.liferadio.sync.data.model.EventTriggerListResponse
import com.liferadio.sync.data.model.EventTriggerPatch
import com.liferadio.sync.data.model.TriggerTypeCatalogResponse
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

sealed interface TriggerResult {
    data class Success(val data: Any) : TriggerResult
    data class Failure(val statusCode: Int?, val errorKey: String?, val reason: String) : TriggerResult
}

class CentralTriggerClient(
    baseUrl: String,
    private val tokenProvider: () -> String,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build(),
    private val moshi: Moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
) {
    private val base = requireNotNull(baseUrl.toHttpUrlOrNull()) { "Central Base URL must be HTTPS" }

    private val catalogAdapter = moshi.adapter(TriggerTypeCatalogResponse::class.java)
    private val triggerAdapter = moshi.adapter(EventTrigger::class.java)
    private val triggerListAdapter = moshi.adapter(EventTriggerListResponse::class.java)
    private val createAdapter = moshi.adapter(EventTriggerCreate::class.java)
    private val patchAdapter = moshi.adapter(EventTriggerPatch::class.java)

    suspend fun listTriggerTypes(): TriggerResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/trigger-types").build()
        executeGet(url, catalogAdapter)
    }

    suspend fun listEventTriggers(): TriggerResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/event-triggers").build()
        executeGet(url, triggerListAdapter)
    }

    suspend fun createEventTrigger(request: EventTriggerCreate): TriggerResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/event-triggers").build()
        executePost(url, createAdapter.toJson(request), triggerAdapter)
    }

    suspend fun updateEventTrigger(triggerId: String, patch: EventTriggerPatch): TriggerResult =
        withContext(Dispatchers.IO) {
            val url = base.newBuilder().addPathSegments("v1/event-triggers/$triggerId").build()
            executePost(url, patchAdapter.toJson(patch), triggerAdapter)
        }

    suspend fun deleteEventTrigger(triggerId: String): TriggerResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/event-triggers/$triggerId/delete").build()
        executeDeleteViaPost(url)
    }

    private fun <T> executeGet(url: okhttp3.HttpUrl, adapter: com.squareup.moshi.JsonAdapter<T>): TriggerResult {
        val token = tokenProvider()
        if (token.isBlank()) return TriggerResult.Failure(401, "invalid_token", "central token is missing")
        return doExecute(Request.Builder().url(url).header("Authorization", "Bearer $token").get().build(), adapter)
    }

    private fun <T> executePost(url: okhttp3.HttpUrl, body: String, adapter: com.squareup.moshi.JsonAdapter<T>): TriggerResult {
        val token = tokenProvider()
        if (token.isBlank()) return TriggerResult.Failure(401, "invalid_token", "central token is missing")
        val req = Request.Builder().url(url).header("Authorization", "Bearer $token")
            .post(body.toRequestBody("application/json; charset=utf-8".toMediaType())).build()
        return doExecute(req, adapter)
    }

    private fun executeDeleteViaPost(url: okhttp3.HttpUrl): TriggerResult {
        val token = tokenProvider()
        if (token.isBlank()) return TriggerResult.Failure(401, "invalid_token", "central token is missing")
        return try {
            val request = Request.Builder().url(url).header("Authorization", "Bearer $token")
                .post("".toRequestBody(null)).build()
            val response = client.newCall(request).execute()
            response.use { resp ->
                when (resp.code) {
                    in 200..299, 404 -> TriggerResult.Success(Unit)  // 404 = already deleted
                    else -> TriggerResult.Failure(resp.code, parseErrorKey(resp.body?.string().orEmpty()),
                        "HTTP ${resp.code}")
                }
            }
        } catch (_: IOException) {
            TriggerResult.Failure(null, "connection_error", "connection failed")
        }
    }

    private fun <T> doExecute(request: Request, adapter: com.squareup.moshi.JsonAdapter<T>): TriggerResult {
        return try {
            val response = client.newCall(request).execute()
            response.use { resp ->
                if (!resp.isSuccessful) {
                    val errorBody = resp.body?.string().orEmpty()
                    val errorKey = parseErrorKey(errorBody)
                    TriggerResult.Failure(resp.code, errorKey,
                        buildTriggerError(resp.code, errorKey))
                } else {
                    val body = resp.body?.string().orEmpty()
                    val result = runCatching { adapter.fromJson(body) }.getOrNull()
                    if (result != null) TriggerResult.Success(result as Any)
                    else TriggerResult.Failure(resp.code, "parse_error", "invalid response body")
                }
            }
        } catch (_: IOException) {
            TriggerResult.Failure(null, "connection_error", "connection failed")
        }
    }

    private fun parseErrorKey(body: String): String? = runCatching {
        @Suppress("UNCHECKED_CAST")
        (Moshi.Builder().build().adapter(Map::class.java).fromJson(body) as? Map<String, Any?>)?.get("error") as? String
    }.getOrNull()

    private fun buildTriggerError(statusCode: Int?, errorKey: String?): String = when {
        errorKey == "not_found" && statusCode == 404 ->
            "中央服务尚未加载心愿与提醒接口，请更新或重启中央服务"
        errorKey == "idempotency_conflict" -> "该请求已存在，请重试"
        else -> "中央返回 HTTP ${statusCode ?: "?"}"
    }

    companion object {
        /** Whether this is a retryable (uncertain outcome) error. */
        fun isRetryable(statusCode: Int?): Boolean =
            statusCode == null || statusCode == 408 || statusCode == 429 || (statusCode in 500..599)
    }
}
