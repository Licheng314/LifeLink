package com.liferadio.sync.data.local

import android.content.ContentResolver
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import com.liferadio.sync.data.model.EventBusinessDay
import java.time.Instant
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
    val businessDate: String
)

/**
 * Reads only the projection Android currently permits. Callers must gate this on the explicit
 * feature switch and [PhotoPermissionScope]; disappearing rows never modify desired sync state.
 */
class MediaStorePhotoReader(private val context: Context) {
    fun loadPage(dayStartHour: Int, limit: Int, offset: Int): List<AccessiblePhoto> {
        if (photoPermissionScope(context) == PhotoPermissionScope.NONE) return emptyList()
        val safeLimit = limit.coerceIn(1, 200)
        val safeOffset = offset.coerceAtLeast(0)
        val projection = arrayOf(
            MediaStore.Images.Media._ID, MediaStore.Images.Media.DATE_TAKEN,
            MediaStore.Images.Media.DATE_ADDED, MediaStore.Images.Media.MIME_TYPE,
            MediaStore.Images.Media.WIDTH, MediaStore.Images.Media.HEIGHT, MediaStore.Images.Media.SIZE
        )
        val args = android.os.Bundle().apply {
            putStringArray(ContentResolver.QUERY_ARG_SORT_COLUMNS, arrayOf(MediaStore.Images.Media.DATE_TAKEN, MediaStore.Images.Media._ID))
            putInt(ContentResolver.QUERY_ARG_SORT_DIRECTION, ContentResolver.QUERY_SORT_DIRECTION_DESCENDING)
            putInt(ContentResolver.QUERY_ARG_LIMIT, safeLimit)
            putInt(ContentResolver.QUERY_ARG_OFFSET, safeOffset)
        }
        val uri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        val resolver = context.contentResolver
        return try {
            resolver.query(uri, projection, args, null)?.use { cursor ->
                readPage(cursor, dayStartHour, safeLimit, skip = 0)
            }.orEmpty()
        } catch (_: IllegalArgumentException) {
            // Some vendor MediaStore implementations reject structured limit/offset or a
            // secondary sort column. The compatibility query still reads only metadata and
            // advances the cursor to the requested page; it never decodes the photo bodies.
            resolver.query(
                uri,
                projection,
                null,
                null,
                "${MediaStore.Images.Media.DATE_TAKEN} DESC, ${MediaStore.Images.Media._ID} DESC"
            )?.use { cursor -> readPage(cursor, dayStartHour, safeLimit, skip = safeOffset) }.orEmpty()
        }
    }

    private fun readPage(
        cursor: android.database.Cursor,
        dayStartHour: Int,
        limit: Int,
        skip: Int
    ): List<AccessiblePhoto> {
        val idIndex = cursor.getColumnIndex(MediaStore.Images.Media._ID)
        if (idIndex < 0) return emptyList()
        val takenIndex = cursor.getColumnIndex(MediaStore.Images.Media.DATE_TAKEN)
        val addedIndex = cursor.getColumnIndex(MediaStore.Images.Media.DATE_ADDED)
        val mimeIndex = cursor.getColumnIndex(MediaStore.Images.Media.MIME_TYPE)
        val widthIndex = cursor.getColumnIndex(MediaStore.Images.Media.WIDTH)
        val heightIndex = cursor.getColumnIndex(MediaStore.Images.Media.HEIGHT)
        val sizeIndex = cursor.getColumnIndex(MediaStore.Images.Media.SIZE)
        repeat(skip) { if (!cursor.moveToNext()) return emptyList() }
        return buildList {
            while (size < limit && cursor.moveToNext()) {
                val id = cursor.getLong(idIndex)
                val taken = cursor.longOrZero(takenIndex)
                val added = cursor.longOrZero(addedIndex)
                val captured = when {
                    taken > 0 -> Instant.ofEpochMilli(taken)
                    added > 0 -> Instant.ofEpochSecond(added)
                    else -> Instant.EPOCH
                }
                add(AccessiblePhoto(
                    mediaStoreId = id,
                    uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id),
                    capturedAt = captured,
                    timeSource = if (taken > 0) "captured" else "added",
                    mimeType = cursor.stringOrEmpty(mimeIndex),
                    width = cursor.intOrZero(widthIndex).coerceAtLeast(0),
                    height = cursor.intOrZero(heightIndex).coerceAtLeast(0),
                    byteSize = cursor.longOrZero(sizeIndex).coerceAtLeast(0),
                    businessDate = EventBusinessDay.at(dayStartHour, captured).toString()
                ))
            }
        }
    }

    private fun android.database.Cursor.longOrZero(index: Int): Long =
        if (index >= 0 && !isNull(index)) getLong(index) else 0L

    private fun android.database.Cursor.intOrZero(index: Int): Int =
        if (index >= 0 && !isNull(index)) getInt(index) else 0

    private fun android.database.Cursor.stringOrEmpty(index: Int): String =
        if (index >= 0 && !isNull(index)) getString(index).orEmpty() else ""
}

/** Pure intent comparison; unavailable MediaStore rows are intentionally absent from this input. */
data class PhotoSelectionDiff(val additions: Int, val removals: Int) { val hasChanges get() = additions > 0 || removals > 0 }

fun photoSelectionDiff(records: Iterable<PhotoSyncSelectionEntity>): PhotoSelectionDiff = PhotoSelectionDiff(
    additions = records.count { it.desiredSynced && !it.confirmedSynced },
    removals = records.count { !it.desiredSynced && it.confirmedSynced }
)

fun stablePhotoId(mediaStoreId: Long): String = UUID.nameUUIDFromBytes("lifelink-photo:$mediaStoreId".toByteArray()).toString()
