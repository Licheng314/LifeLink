package com.liferadio.sync.data.local

import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import com.liferadio.sync.data.model.EventBusinessDay
import java.time.Instant
import java.util.PriorityQueue
import java.util.UUID

/** The scope actually granted by Android; it is deliberately not inferred from the photo count. */
enum class PhotoPermissionScope { NONE, SELECTED, ALL }

fun photoPermissionScope(context: Context): PhotoPermissionScope = when {
    Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
        ContextCompat.checkSelfPermission(context, android.Manifest.permission.READ_MEDIA_IMAGES) == PackageManager.PERMISSION_GRANTED -> PhotoPermissionScope.ALL
    Build.VERSION.SDK_INT >= 34 &&
        ContextCompat.checkSelfPermission(context, android.Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED) == PackageManager.PERMISSION_GRANTED -> PhotoPermissionScope.SELECTED
    Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU &&
        ContextCompat.checkSelfPermission(context, android.Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED -> PhotoPermissionScope.ALL
    else -> PhotoPermissionScope.NONE
}

data class AccessiblePhoto(
    val mediaStoreId: Long,
    val uri: Uri,
    val capturedAt: Instant,
    val timeSource: String,
    val mimeType: String,
    val width: Int,
    val height: Int,
    val byteSize: Long,
    val businessDate: String,
    /** Best-effort, local-only label for the compact gallery UI. */
    val sourceLabel: String
)

/**
 * Reads only the projection Android currently permits. Callers must gate this on the explicit
 * feature switch and [PhotoPermissionScope]; disappearing rows never modify desired sync state.
 */
class MediaStorePhotoReader(private val context: Context) {
    private val baseProjection = arrayOf(
        MediaStore.Images.Media._ID,
        MediaStore.Images.Media.DATE_TAKEN,
        MediaStore.Images.Media.DATE_ADDED,
        MediaStore.Images.Media.MIME_TYPE,
        MediaStore.Images.Media.WIDTH,
        MediaStore.Images.Media.HEIGHT,
        MediaStore.Images.Media.SIZE
    )
    private val projection = buildList {
        addAll(baseProjection)
        add(MediaStore.Images.Media.DISPLAY_NAME)
        add(MediaStore.Images.Media.BUCKET_DISPLAY_NAME)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            add(MediaStore.Images.Media.OWNER_PACKAGE_NAME)
            add(MediaStore.Images.Media.RELATIVE_PATH)
        }
    }.toTypedArray()

    fun loadPage(dayStartHour: Int, limit: Int, offset: Int): List<AccessiblePhoto> {
        if (photoPermissionScope(context) == PhotoPermissionScope.NONE) return emptyList()
        val safeLimit = limit.coerceIn(1, 200)
        val safeOffset = offset.coerceAtLeast(0)
        val uri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        // MediaStore providers are allowed to ignore structured sort arguments. Several OEM
        // providers honor LIMIT/OFFSET but return their default (oldest-first) order, which makes
        // the first page contain only old photos. Scan metadata only and choose the newest page
        // locally so ordering is deterministic without decoding any image body.
        return queryCompatible(uri, null, null)?.use { cursor ->
            readNewestPage(cursor, dayStartHour, safeLimit, safeOffset)
        }.orEmpty()
    }

    fun loadByMediaStoreIds(dayStartHour: Int, mediaStoreIds: Collection<Long>): List<AccessiblePhoto> {
        if (photoPermissionScope(context) == PhotoPermissionScope.NONE || mediaStoreIds.isEmpty()) return emptyList()
        val uri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        return mediaStoreIds.distinct().chunked(200).flatMap { ids ->
            val placeholders = ids.joinToString(",") { "?" }
            queryCompatible(
                uri,
                "${MediaStore.Images.Media._ID} IN ($placeholders)",
                ids.map { it.toString() }.toTypedArray()
            )?.use { cursor -> readNewestPage(cursor, dayStartHour, ids.size, 0) }.orEmpty()
        }
    }

    /** Some OEM providers reject newer optional projection columns instead of returning null. */
    private fun queryCompatible(uri: Uri, selection: String?, selectionArgs: Array<String>?): android.database.Cursor? =
        runCatching {
            context.contentResolver.query(uri, projection, selection, selectionArgs, null)
        }.getOrNull() ?: context.contentResolver.query(uri, baseProjection, selection, selectionArgs, null)

    private fun readNewestPage(
        cursor: android.database.Cursor,
        dayStartHour: Int,
        limit: Int,
        offset: Int
    ): List<AccessiblePhoto> {
        val idIndex = cursor.getColumnIndex(MediaStore.Images.Media._ID)
        if (idIndex < 0) return emptyList()
        val takenIndex = cursor.getColumnIndex(MediaStore.Images.Media.DATE_TAKEN)
        val addedIndex = cursor.getColumnIndex(MediaStore.Images.Media.DATE_ADDED)
        val mimeIndex = cursor.getColumnIndex(MediaStore.Images.Media.MIME_TYPE)
        val widthIndex = cursor.getColumnIndex(MediaStore.Images.Media.WIDTH)
        val heightIndex = cursor.getColumnIndex(MediaStore.Images.Media.HEIGHT)
        val sizeIndex = cursor.getColumnIndex(MediaStore.Images.Media.SIZE)
        val displayNameIndex = cursor.getColumnIndex(MediaStore.Images.Media.DISPLAY_NAME)
        val bucketIndex = cursor.getColumnIndex(MediaStore.Images.Media.BUCKET_DISPLAY_NAME)
        val ownerIndex = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) cursor.getColumnIndex(MediaStore.Images.Media.OWNER_PACKAGE_NAME) else -1
        val relativePathIndex = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) cursor.getColumnIndex(MediaStore.Images.Media.RELATIVE_PATH) else -1
        val photos = sequence {
            while (cursor.moveToNext()) {
                val id = cursor.getLong(idIndex)
                val taken = cursor.longOrZero(takenIndex)
                val added = cursor.longOrZero(addedIndex)
                val captured = when {
                    taken > 0 -> Instant.ofEpochMilli(taken)
                    added > 0 -> Instant.ofEpochSecond(added)
                    else -> Instant.EPOCH
                }
                val ownerPackage = cursor.stringOrEmpty(ownerIndex)
                val ownerLabel = ownerPackage
                    .takeIf { it.isNotBlank() && !it.startsWith("com.android.") && !it.startsWith("com.miui.") }
                    ?.let(::applicationLabel)
                yield(AccessiblePhoto(
                    mediaStoreId = id,
                    uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id),
                    capturedAt = captured,
                    timeSource = if (taken > 0) "captured" else "added",
                    mimeType = cursor.stringOrEmpty(mimeIndex),
                    width = cursor.intOrZero(widthIndex).coerceAtLeast(0),
                    height = cursor.intOrZero(heightIndex).coerceAtLeast(0),
                    byteSize = cursor.longOrZero(sizeIndex).coerceAtLeast(0),
                    businessDate = EventBusinessDay.at(dayStartHour, captured).toString(),
                    sourceLabel = compactPhotoSourceLabel(
                        ownerLabel = ownerLabel,
                        bucketName = cursor.stringOrEmpty(bucketIndex),
                        relativePath = cursor.stringOrEmpty(relativePathIndex),
                        displayName = cursor.stringOrEmpty(displayNameIndex)
                    )
                ))
            }
        }
        return newestPage(
            items = photos,
            limit = limit,
            offset = offset,
            comparator = compareBy<AccessiblePhoto>({ it.capturedAt }, { it.mediaStoreId })
        )
    }

    private fun android.database.Cursor.longOrZero(index: Int): Long =
        if (index >= 0 && !isNull(index)) getLong(index) else 0L

    private fun android.database.Cursor.intOrZero(index: Int): Int =
        if (index >= 0 && !isNull(index)) getInt(index) else 0

    private fun android.database.Cursor.stringOrEmpty(index: Int): String =
        if (index >= 0 && !isNull(index)) getString(index).orEmpty() else ""

    private fun applicationLabel(packageName: String): String? = runCatching {
        val app = context.packageManager.getApplicationInfo(packageName, 0)
        context.packageManager.getApplicationLabel(app).toString().trim().takeIf { it.isNotEmpty() }
    }.getOrNull()
}

/**
 * Builds a short display label without making OEM-only metadata a requirement. The contributing
 * app is preferred when Android exposes it; album/path/name hints provide stable fallbacks.
 */
internal fun compactPhotoSourceLabel(
    ownerLabel: String?,
    bucketName: String?,
    relativePath: String?,
    displayName: String?
): String {
    ownerLabel?.trim()?.takeIf { it.isNotEmpty() }?.let { return it }
    val hints = listOf(bucketName, relativePath, displayName).filterNotNull().joinToString("/").lowercase()
    return when {
        "screenshot" in hints || "截屏" in hints || "截图" in hints -> "截图"
        "weixin" in hints || "wechat" in hints || "micromsg" in hints -> "微信"
        "camera" in hints || "dcim" in hints -> "相机"
        "download" in hints -> "下载"
        !bucketName.isNullOrBlank() -> bucketName.trim()
        else -> "照片"
    }
}

/**
 * Keeps only the requested newest prefix in memory while consuming an arbitrarily ordered
 * metadata sequence. The comparator must order older/smaller values before newer/larger ones.
 */
internal fun <T> newestPage(
    items: Sequence<T>,
    limit: Int,
    offset: Int,
    comparator: Comparator<T>
): List<T> {
    val safeLimit = limit.coerceAtLeast(0)
    val safeOffset = offset.coerceAtLeast(0)
    if (safeLimit == 0) return emptyList()
    val retainedCount = (safeOffset.toLong() + safeLimit).coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
    val newest = PriorityQueue(retainedCount.coerceAtLeast(1), comparator)
    items.forEach { item ->
        if (newest.size < retainedCount) {
            newest.add(item)
        } else if (comparator.compare(item, newest.peek()) > 0) {
            newest.poll()
            newest.add(item)
        }
    }
    return newest.sortedWith(comparator.reversed()).drop(safeOffset).take(safeLimit)
}

/** Pure intent comparison; unavailable MediaStore rows are intentionally absent from this input. */
data class PhotoSelectionDiff(val additions: Int, val removals: Int) { val hasChanges get() = additions > 0 || removals > 0 }

fun photoSelectionDiff(records: Iterable<PhotoSyncSelectionEntity>): PhotoSelectionDiff = PhotoSelectionDiff(
    additions = records.count { it.desiredSynced && !it.confirmedSynced },
    removals = records.count { !it.desiredSynced && it.confirmedSynced }
)

fun stablePhotoId(mediaStoreId: Long): String = UUID.nameUUIDFromBytes("lifelink-photo:$mediaStoreId".toByteArray()).toString()
