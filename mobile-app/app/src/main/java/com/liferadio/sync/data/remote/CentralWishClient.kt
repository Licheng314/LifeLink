package com.liferadio.sync.data.remote

import com.liferadio.sync.data.model.TimelineEventListResponse
import com.liferadio.sync.data.model.EventBackgroundResponse
import com.liferadio.sync.data.model.Wish
import com.liferadio.sync.data.model.WishCreate
import com.liferadio.sync.data.model.WishDay
import com.liferadio.sync.data.model.WishDayAssessment
import com.liferadio.sync.data.model.WishListResponse
import com.liferadio.sync.data.model.WishPatch
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

sealed interface WishResult {
    data class Success(val data: Any) : WishResult
    data class Failure(val statusCode: Int?, val errorKey: String?, val reason: String) : WishResult
}

class CentralWishClient(
    baseUrl: String,
    private val tokenProvider: () -> String,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build(),
    private val moshi: Moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
) {
    private val base = requireNotNull(baseUrl.toHttpUrlOrNull()) {
        "Central Base URL must be an absolute URL"
    }

    private val wishAdapter = moshi.adapter(Wish::class.java)
    private val wishListAdapter = moshi.adapter(WishListResponse::class.java)
    private val wishDayAdapter = moshi.adapter(WishDay::class.java)
    private val createAdapter = moshi.adapter(WishCreate::class.java)
    private val assessmentAdapter = moshi.adapter(WishDayAssessment::class.java)
    private val patchAdapter = moshi.adapter(WishPatch::class.java)
    private val timelineListAdapter = moshi.adapter(TimelineEventListResponse::class.java)
    private val eventBackgroundAdapter = moshi.adapter(EventBackgroundResponse::class.java)

    suspend fun listWishes(includeArchived: Boolean = false): WishResult =
        withContext(Dispatchers.IO) {
            val url = base.newBuilder()
                .addPathSegments("v1/wishes")
                .addQueryParameter("include_archived", includeArchived.toString())
                .build()
            executeGet(url, wishListAdapter)
        }

    suspend fun getWish(wishId: String): WishResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/wishes/$wishId").build()
        executeGet(url, wishAdapter)
    }

    suspend fun createWish(request: WishCreate): WishResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/wishes").build()
        executePost(url, createAdapter.toJson(request), wishAdapter)
    }

    suspend fun updateWish(wishId: String, patch: WishPatch): WishResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/wishes/$wishId").build()
        executePost(url, patchAdapter.toJson(patch), wishAdapter)
    }

    suspend fun completeWish(wishId: String): WishResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/wishes/$wishId/complete").build()
        executePost(url, null, wishAdapter)
    }

    /** POST delete alias avoids deployed HTTPS mappings that reject DELETE. */
    suspend fun deleteWish(wishId: String): WishResult = withContext(Dispatchers.IO) {
        val token = tokenProvider()
        if (token.isBlank()) return@withContext WishResult.Failure(401, "invalid_token", "central token is missing")
        val url = base.newBuilder().addPathSegments("v1/wishes/$wishId/delete").build()
        try {
            val request = Request.Builder().url(url).header("Authorization", "Bearer $token")
                .post("".toRequestBody(null)).build()
            client.newCall(request).execute().use { response ->
                if (response.code in 200..299 || response.code == 404) WishResult.Success(Unit)
                else {
                    val errorKey = parseErrorKey(response.body?.string().orEmpty())
                    WishResult.Failure(response.code, errorKey, buildErrorMessage(response.code, errorKey))
                }
            }
        } catch (_: IOException) {
            WishResult.Failure(null, "connection_error", "central connection failed")
        }
    }

    suspend fun assessWishDay(wishId: String, businessDate: String, assessment: WishDayAssessment): WishResult =
        withContext(Dispatchers.IO) {
            val url = base.newBuilder()
                .addPathSegments("v1/wishes/$wishId/days/$businessDate").build()
            executePut(url, assessmentAdapter.toJson(assessment), wishDayAdapter)
        }

    suspend fun listTimeline(from: String?, to: String?, category: String? = null): WishResult =
        withContext(Dispatchers.IO) {
            val builder = base.newBuilder().addPathSegments("v1/timeline-events")
            if (from != null) builder.addQueryParameter("from", from)
            if (to != null) builder.addQueryParameter("to", to)
            if (category != null) builder.addQueryParameter("category", category)
            executeGet(builder.build(), timelineListAdapter)
        }

    /** Central projection only: clients render it and never recompute its milestones locally. */
    suspend fun getEventBackground(businessDate: String): WishResult = withContext(Dispatchers.IO) {
        val url = base.newBuilder().addPathSegments("v1/event-background")
            .addQueryParameter("business_date", businessDate).build()
        executeGet(url, eventBackgroundAdapter)
    }

    private fun <T> executeGet(url: okhttp3.HttpUrl, adapter: com.squareup.moshi.JsonAdapter<T>): WishResult {
        val token = tokenProvider()
        if (token.isBlank()) return WishResult.Failure(401, "invalid_token", "central token is missing")
        return doExecute(Request.Builder().url(url).header("Authorization", "Bearer $token").get().build(), adapter)
    }

    private fun <T> executePost(url: okhttp3.HttpUrl, body: String?, adapter: com.squareup.moshi.JsonAdapter<T>): WishResult {
        val token = tokenProvider()
        if (token.isBlank()) return WishResult.Failure(401, "invalid_token", "central token is missing")
        val req = Request.Builder().url(url).header("Authorization", "Bearer $token")
        if (body != null) req.post(body.toRequestBody("application/json; charset=utf-8".toMediaType()))
        else req.post("".toRequestBody(null))
        return doExecute(req.build(), adapter)
    }

    private fun <T> executePut(url: okhttp3.HttpUrl, body: String, adapter: com.squareup.moshi.JsonAdapter<T>): WishResult {
        val token = tokenProvider()
        if (token.isBlank()) return WishResult.Failure(401, "invalid_token", "central token is missing")
        val req = Request.Builder().url(url).header("Authorization", "Bearer $token")
            .put(body.toRequestBody("application/json; charset=utf-8".toMediaType())).build()
        return doExecute(req, adapter)
    }

    private fun <T> doExecute(request: Request, adapter: com.squareup.moshi.JsonAdapter<T>): WishResult {
        return try {
            val response = client.newCall(request).execute()
            response.use { resp ->
                if (!resp.isSuccessful) {
                    val errorBody = resp.body?.string().orEmpty()
                    val errorKey = parseErrorKey(errorBody)
                    WishResult.Failure(resp.code, errorKey, buildErrorMessage(resp.code, errorKey))
                } else {
                    val body = resp.body?.string().orEmpty()
                    val result = runCatching { adapter.fromJson(body) }.getOrNull()
                    if (result != null) WishResult.Success(result as Any)
                    else WishResult.Failure(resp.code, "parse_error", "invalid response body")
                }
            }
        } catch (_: IOException) {
            WishResult.Failure(null, "connection_error", "central connection failed")
        }
    }

    private fun parseErrorKey(body: String): String? {
        return runCatching {
            @Suppress("UNCHECKED_CAST")
            (Moshi.Builder().build().adapter(Map::class.java).fromJson(body) as? Map<String, Any?>)?.get("error") as? String
        }.getOrNull()
    }

    private fun buildErrorMessage(statusCode: Int?, errorKey: String?): String = when {
        errorKey == "not_found" && statusCode == 404 ->
            "中央服务尚未加载心愿与提醒接口，请更新或重启中央服务"
        errorKey == "unarchived_wish_limit_reached" -> "最多 3 条进行中的心愿"
        errorKey == "wish_not_found" -> "心愿不存在"
        errorKey == "wish_day_not_found" -> "该日期不属于此心愿"
        errorKey == "wish_days_incomplete" -> "仍有日期结果未填写"
        errorKey == "wish_not_completable" -> "尚未到心愿完结时间"
        errorKey == "future_wish_day" -> "未来日期不可评估"
        errorKey == "wish_not_cancellable" -> "该心愿无法取消"
        errorKey == "wish_deleted" -> "该心愿已永久删除，原创建请求不可重试"
        errorKey == "idempotency_conflict" -> "该请求已存在，请重试"
        errorKey == "invalid_invitation" || errorKey == "invalid_token" -> "中央凭据失效（HTTP ${statusCode ?: "?"}）"
        else -> "中央返回 HTTP ${statusCode ?: "?"}"
    }

    companion object {
        fun isAuthFailure(statusCode: Int?): Boolean = statusCode == 401 || statusCode == 403
        fun isServerNotLoaded(statusCode: Int?, errorKey: String?): Boolean =
            statusCode == 404 && errorKey == "not_found"
    }
}
