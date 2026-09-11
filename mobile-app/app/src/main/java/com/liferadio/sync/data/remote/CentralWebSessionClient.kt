package com.liferadio.sync.data.remote

import com.squareup.moshi.Moshi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.HttpUrl.Companion.toHttpUrl
import java.io.IOException
import java.util.concurrent.TimeUnit

sealed interface WebUiSessionResult {
    data class Success(val webUrl: String) : WebUiSessionResult
    data class Failure(val message: String) : WebUiSessionResult
}

class CentralWebSessionClient(
    baseUrl: String,
    private val tokenProvider: () -> String,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()
) {
    private val base = baseUrl.trimEnd('/').plus('/').toHttpUrl()
    private val mapAdapter = Moshi.Builder().build().adapter(Map::class.java)

    suspend fun create(): WebUiSessionResult = withContext(Dispatchers.IO) {
        val token = tokenProvider()
        if (token.isBlank()) return@withContext WebUiSessionResult.Failure("中央凭据尚未配置")
        val request = Request.Builder()
            .url(base.newBuilder().addPathSegments("v1/web-sessions").build())
            .header("Authorization", "Bearer $token")
            .post(ByteArray(0).toRequestBody("application/json".toMediaType()))
            .build()
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    return@withContext WebUiSessionResult.Failure(
                        if (response.code == 409) "中央服务尚未配置可用的 HTTPS WebUI 地址"
                        else "中央服务返回 HTTP ${response.code}"
                    )
                }
                val payload = runCatching { mapAdapter.fromJson(response.body?.string().orEmpty()) }.getOrNull()
                val webUrl = payload?.get("web_url") as? String
                if (!isValidWebUrl(webUrl)) {
                    WebUiSessionResult.Failure("中央服务返回了无效的 WebUI 地址")
                } else {
                    WebUiSessionResult.Success(webUrl!!)
                }
            }
        } catch (_: IOException) {
            WebUiSessionResult.Failure("无法连接中央服务")
        }
    }

    companion object {
        internal fun isValidWebUrl(value: String?): Boolean {
            val url = runCatching { value?.toHttpUrl() }.getOrNull() ?: return false
            return url.isHttps && url.host.isNotBlank() &&
                url.fragment?.startsWith("lifelink_bootstrap=") == true
        }
    }
}
