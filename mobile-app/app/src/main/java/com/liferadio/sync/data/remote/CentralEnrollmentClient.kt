package com.liferadio.sync.data.remote

import android.os.Build
import com.liferadio.sync.data.local.CentralTokenValidator
import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import java.time.Instant
import java.util.UUID
import java.util.Base64
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

data class CentralInvitation(
    val invitationId: String,
    val centralBaseUrl: String,
    val invitationToken: String,
    val scope: String,
    val expiresAt: String
) {
    val permissionLabel: String
        get() = if (scope == "dashboard") "上传与中央读取" else "仅上传本机数据"
}

object CentralInvitationParser {
    private const val PREFIX = "LR1."
    private const val MAX_CODE_LENGTH = 8192
    private val requiredKeys = setOf(
        "v", "invitation_id", "central_base_url", "invitation_token", "scope", "expires_at"
    )

    fun parse(code: String, now: Instant = Instant.now()): CentralInvitation {
        val normalized = code.trim()
        require(normalized.length <= MAX_CODE_LENGTH && normalized.startsWith(PREFIX)) {
            "请输入有效的 LR1 单行邀请码"
        }
        require(!normalized.any { it == '\n' || it == '\r' }) { "邀请码必须为单行文本" }
        val encoded = normalized.removePrefix(PREFIX)
        require(encoded.isNotBlank() && encoded.none { it == '=' }) { "邀请码编码无效" }
        val json = runCatching {
            String(Base64.getUrlDecoder().decode(encoded), Charsets.UTF_8)
        }.getOrElse { throw IllegalArgumentException("邀请码编码无效") }
        val payload = runCatching {
            @Suppress("UNCHECKED_CAST")
            Moshi.Builder().build().adapter(Map::class.java).fromJson(json) as? Map<String, Any?>
        }.getOrNull() ?: throw IllegalArgumentException("邀请码内容无效")
        require(payload.keys == requiredKeys && (payload["v"] as? Number)?.toInt() == 1) {
            "邀请码版本或字段无效"
        }
        val invitationId = payload["invitation_id"] as? String ?: ""
        require(runCatching { UUID.fromString(invitationId).toString() == invitationId }.getOrDefault(false)) {
            "邀请码 ID 无效"
        }
        val baseUrl = payload["central_base_url"] as? String ?: ""
        validateCentralBaseUrl(baseUrl)
        val token = payload["invitation_token"] as? String ?: ""
        require(CentralTokenValidator.isValid(token)) { "邀请码凭据无效" }
        val scope = payload["scope"] as? String ?: ""
        require(scope == "upload" || scope == "dashboard") { "邀请码权限无效" }
        val expiresAt = payload["expires_at"] as? String ?: ""
        val expiry = runCatching { Instant.parse(expiresAt) }
            .getOrElse { throw IllegalArgumentException("邀请码过期时间无效") }
        require(expiry.isAfter(now)) { "邀请码已过期" }
        return CentralInvitation(invitationId, normalizeBaseUrl(baseUrl), token, scope, expiresAt)
    }

    fun validateCentralBaseUrl(value: String) {
        val url = value.toHttpUrlOrNull() ?: throw IllegalArgumentException("中央地址无效")
        require(url.isHttps) { "中央地址必须使用 HTTPS" }
        require(url.username.isEmpty() && url.password.isEmpty()) { "中央地址不能包含用户信息" }
        require(url.query == null && url.fragment == null) { "中央地址不能包含查询或片段" }
        require(url.encodedPath == "/") { "中央地址必须为 HTTPS 根地址" }
    }

    fun normalizeBaseUrl(value: String): String = value.trimEnd('/')
}

@JsonClass(generateAdapter = true)
data class EnrollmentDevice(
    @Json(name = "device_id") val deviceId: String,
    @Json(name = "platform") val platform: String = "android",
    @Json(name = "display_name") val displayName: String
)

@JsonClass(generateAdapter = true)
data class EnrollmentClaim(
    @Json(name = "schema_version") val schemaVersion: String = "life-radio-enrollment-claim-v1",
    @Json(name = "invitation_id") val invitationId: String,
    @Json(name = "device") val device: EnrollmentDevice
)

@JsonClass(generateAdapter = true)
data class CentralClientProfile(
    @Json(name = "schema_version") val schemaVersion: String,
    @Json(name = "central_base_url") val centralBaseUrl: String,
    @Json(name = "device") val device: EnrollmentDevice,
    @Json(name = "upload_token") val uploadToken: String,
    @Json(name = "issued_at") val issuedAt: String,
    @Json(name = "read_token") val readToken: String? = null
)

sealed interface EnrollmentResult {
    data class Success(val profile: CentralClientProfile) : EnrollmentResult
    data class Failure(val message: String, val retryable: Boolean) : EnrollmentResult
}

class CentralEnrollmentClient(
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build(),
    moshi: Moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
) {
    private val claimAdapter = moshi.adapter(EnrollmentClaim::class.java)
    private val profileAdapter = moshi.adapter(CentralClientProfile::class.java)

    suspend fun claim(invitation: CentralInvitation, deviceId: String): EnrollmentResult =
        withContext(Dispatchers.IO) {
            val claim = EnrollmentClaim(
                invitationId = invitation.invitationId,
                device = EnrollmentDevice(
                    deviceId = deviceId,
                    displayName = (Build.MANUFACTURER + " " + Build.MODEL).trim().take(100)
                        .ifBlank { "Life Link Android" }
                )
            )
            val endpoint = invitation.centralBaseUrl.toHttpUrlOrNull()!!.newBuilder()
                .addPathSegments("v1/enrollments/claim")
                .build()
            val request = Request.Builder()
                .url(endpoint)
                .header("Authorization", "Bearer ${invitation.invitationToken}")
                .post(claimAdapter.toJson(claim).toRequestBody("application/json; charset=utf-8".toMediaType()))
                .build()
            runCatching {
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) {
                        val retryable = response.code == 429 || response.code == 503 || response.code >= 500
                        return@use EnrollmentResult.Failure(
                            when (response.code) {
                                401 -> "邀请码无效或已撤销"
                                409 -> "邀请码已绑定到其他设备"
                                410 -> "邀请码已过期"
                                429 -> "领取过于频繁，请稍后重试"
                                else -> "中央服务拒绝领取（HTTP ${response.code}）"
                            },
                            retryable
                        )
                    }
                    val profile = profileAdapter.fromJson(response.body?.string().orEmpty())
                        ?: return@use EnrollmentResult.Failure("中央服务返回了无效配置", false)
                    val valid = profile.schemaVersion == "life-radio-client-profile-v1" &&
                        profile.device.deviceId == deviceId && profile.device.platform == "android" &&
                        CentralInvitationParser.normalizeBaseUrl(profile.centralBaseUrl) == invitation.centralBaseUrl &&
                        CentralTokenValidator.isValid(profile.uploadToken) &&
                        (invitation.scope != "upload" || profile.readToken == null) &&
                        runCatching { Instant.parse(profile.issuedAt) }.isSuccess
                    if (!valid) EnrollmentResult.Failure("中央服务返回的设备配置不匹配", false)
                    else EnrollmentResult.Success(profile)
                }
            }.getOrElse { EnrollmentResult.Failure("无法连接中央服务，请检查网络后重试", true) }
        }
}
