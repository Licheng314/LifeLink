package com.liferadio.sync.data.remote

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import com.liferadio.sync.data.local.AccessiblePhoto
import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.UUID

@JsonClass(generateAdapter = true)
data class CentralPhoto(@Json(name = "photo_id") val photoId: String, @Json(name = "status") val status: String)
@JsonClass(generateAdapter = true)
data class CentralPhotoListResponse(@Json(name = "photos") val photos: List<CentralPhoto>, @Json(name = "next_cursor") val nextCursor: String?)
@JsonClass(generateAdapter = true)
data class CentralPhotoContentResponse(@Json(name = "photo") val photo: CentralPhoto, @Json(name = "changed") val changed: Boolean)
@JsonClass(generateAdapter = true)
data class PhotoSyncCompleteRequest(@Json(name = "sync_id") val syncId: String)
@JsonClass(generateAdapter = true)
data class PhotoSyncCompleteResponse(@Json(name = "sync_id") val syncId: String, @Json(name = "added_count") val addedCount: Int, @Json(name = "current_business_date_added_count") val currentBusinessDateAddedCount: Int, @Json(name = "removed_count") val removedCount: Int, @Json(name = "business_date") val businessDate: String)
@JsonClass(generateAdapter = true)
private data class PhotoErrorResponse(val message: String? = null)

sealed interface PhotoRequestResult<out T> {
    data class Success<T>(val value: T): PhotoRequestResult<T>
    data class Failure(val message: String, val mayHaveReachedServer: Boolean = false): PhotoRequestResult<Nothing>
}

/** Exact v1 photo contract client. It never uses DELETE because public HTTPS mappings may reject it. */
class CentralPhotoClient(private val baseUrl: String, private val tokenProvider: () -> String?, private val http: OkHttpClient = OkHttpClient()) {
    private val moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
    private val listAdapter = moshi.adapter(CentralPhotoListResponse::class.java)
    private val contentAdapter = moshi.adapter(CentralPhotoContentResponse::class.java)
    private val completeAdapter = moshi.adapter(PhotoSyncCompleteResponse::class.java)
    private val completeRequestAdapter = moshi.adapter(PhotoSyncCompleteRequest::class.java)
    private val errorAdapter = moshi.adapter(PhotoErrorResponse::class.java)

    fun list(cursor: String?): PhotoRequestResult<CentralPhotoListResponse> = executeJson(
        Request.Builder().url("${baseUrl.trimEnd('/')}/v1/photos?limit=30&include_deleted=true" + (cursor?.let { "&cursor=${java.net.URLEncoder.encode(it, "UTF-8")}" } ?: "")).authorized().get().build(), listAdapter
    )

    fun upload(photoId: String, photo: AccessiblePhoto, copy: PreparedPhotoCopy, syncId: String): PhotoRequestResult<CentralPhotoContentResponse> {
        val request = Request.Builder().url("${baseUrl.trimEnd('/')}/v1/photos/$photoId/content").authorized()
            .header("X-Photo-Sync-Id", syncId).header("X-Photo-Captured-At", photo.capturedAt.toString())
            .header("X-Photo-Time-Source", photo.timeSource).header("X-Photo-Mime-Type", copy.mimeType)
            .header("X-Photo-Width", copy.width.toString()).header("X-Photo-Height", copy.height.toString())
            .header("X-Photo-Byte-Size", copy.bytes.size.toString()).header("X-Photo-Sha256", copy.sha256)
            .post(copy.bytes.toRequestBody(copy.mimeType.toMediaType())).build()
        return executeJson(request, contentAdapter)
    }

    fun delete(photoId: String, syncId: String): PhotoRequestResult<CentralPhotoContentResponse> = executeJson(
        Request.Builder().url("${baseUrl.trimEnd('/')}/v1/photos/$photoId/delete").authorized().header("X-Photo-Sync-Id", syncId)
            .post(ByteArray(0).toRequestBody("application/json".toMediaType())).build(), contentAdapter
    )

    fun complete(syncId: String): PhotoRequestResult<PhotoSyncCompleteResponse> = executeJson(
        Request.Builder().url("${baseUrl.trimEnd('/')}/v1/photos/sync-complete").authorized()
            .post(completeRequestAdapter.toJson(PhotoSyncCompleteRequest(syncId)).toRequestBody("application/json; charset=utf-8".toMediaType())).build(), completeAdapter
    )

    private fun Request.Builder.authorized(): Request.Builder = header("Authorization", "Bearer ${tokenProvider().orEmpty()}")
    private fun <T> executeJson(request: Request, adapter: com.squareup.moshi.JsonAdapter<T>): PhotoRequestResult<T> = try {
        http.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val detail = runCatching { errorAdapter.fromJson(body)?.message }.getOrNull()
                PhotoRequestResult.Failure(
                    "中央照片服务返回 ${response.code}" + (detail?.takeIf { it.isNotBlank() }?.let { "：$it" } ?: "")
                )
            } else {
                adapter.fromJson(body)?.let { PhotoRequestResult.Success(it) }
                    ?: PhotoRequestResult.Failure("中央照片服务响应无效", mayHaveReachedServer = true)
            }
        }
    } catch (_: Exception) { PhotoRequestResult.Failure("暂时无法连接中央照片服务", mayHaveReachedServer = true) }
}

data class PreparedPhotoCopy(val bytes: ByteArray, val mimeType: String, val width: Int, val height: Int, val sha256: String)

/** Creates a bounded display copy for upload; the original MediaStore item is never written. */
object PhotoUploadPreparer {
    const val MAX_EDGE = 1920
    const val MAX_BYTES = 8 * 1024 * 1024
    fun prepare(context: Context, uri: Uri): PreparedPhotoCopy? = runCatching {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        val boundsRead = context.contentResolver.openInputStream(uri)?.use {
            // decodeStream deliberately returns null when inJustDecodeBounds is true; the
            // dimensions and MIME metadata in Options are the success signal.
            BitmapFactory.decodeStream(it, null, bounds)
            bounds.outWidth > 0 && bounds.outHeight > 0
        } ?: false
        if (!boundsRead) return null
        var sample = 1
        while (bounds.outWidth / sample > MAX_EDGE * 2 || bounds.outHeight / sample > MAX_EDGE * 2) sample *= 2
        val bitmap = context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply { inSampleSize = sample }) } ?: return null
        val scale = minOf(1f, MAX_EDGE.toFloat() / maxOf(bitmap.width, bitmap.height))
        val display = if (scale < 1f) Bitmap.createScaledBitmap(bitmap, (bitmap.width * scale).toInt(), (bitmap.height * scale).toInt(), true) else bitmap
        val displayWidth = display.width
        val displayHeight = display.height
        var quality = 90; var bytes: ByteArray
        do { bytes = ByteArrayOutputStream().use { out -> display.compress(Bitmap.CompressFormat.JPEG, quality, out); out.toByteArray() }; quality -= 10 } while (bytes.size > MAX_BYTES && quality >= 40)
        if (display !== bitmap) display.recycle(); bitmap.recycle()
        if (bytes.size > MAX_BYTES) return null
        PreparedPhotoCopy(bytes, "image/jpeg", displayWidth, displayHeight, MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) })
    }.getOrNull()
}
