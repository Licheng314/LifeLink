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
        val projection = arrayOf(
            MediaStore.Images.Media._ID, MediaStore.Images.Media.DATE_TAKEN,
            MediaStore.Images.Media.DATE_ADDED, MediaStore.Images.Media.MIME_TYPE,
            MediaStore.Images.Media.WIDTH, MediaStore.Images.Media.HEIGHT, MediaStore.Images.Media.SIZE
        )
        val args = android.os.Bundle().apply {
            putStringArray(ContentResolver.QUERY_ARG_SORT_COLUMNS, arrayOf(MediaStore.Images.Media.DATE_TAKEN, MediaStore.Images.Media._ID))
            putInt(ContentResolver.QUERY_ARG_SORT_DIRECTION, ContentResolver.QUERY_SORT_DIRECTION_DESCENDING)
            putInt(ContentResolver.QUERY_ARG_LIMIT, limit.coerceIn(1, 200))
            putInt(ContentResolver.QUERY_ARG_OFFSET, offset.coerceAtLeast(0))
        }
        return context.contentResolver.query(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, projection, args, null)?.use { cursor ->
            val idIndex = cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID)
            val takenIndex = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_TAKEN)
            val addedIndex = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_ADDED)
            val mimeIndex = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.MIME_TYPE)
            val widthIndex = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.WIDTH)
            val heightIndex = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.HEIGHT)
            val sizeIndex = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.SIZE)
            buildList {
                while (cursor.moveToNext()) {
                    val id = cursor.getLong(idIndex)
                    val taken = cursor.getLong(takenIndex)
                    val added = cursor.getLong(addedIndex)
                    val captured = if (taken > 0) Instant.ofEpochMilli(taken) else Instant.ofEpochSecond(added)
                    add(AccessiblePhoto(
                        mediaStoreId = id,
                        uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id),
                        capturedAt = captured,
                        timeSource = if (taken > 0) "captured" else "added",
                        mimeType = cursor.getString(mimeIndex).orEmpty(),
                        width = cursor.getInt(widthIndex).coerceAtLeast(0), height = cursor.getInt(heightIndex).coerceAtLeast(0),
                        byteSize = cursor.getLong(sizeIndex).coerceAtLeast(0),
                        businessDate = EventBusinessDay.at(dayStartHour, captured).toString()
                    ))
                }
            }
        }.orEmpty()
    }
}

/** Pure intent comparison; unavailable MediaStore rows are intentionally absent from this input. */
data class PhotoSelectionDiff(val additions: Int, val removals: Int) { val hasChanges get() = additions > 0 || removals > 0 }

fun photoSelectionDiff(records: Iterable<PhotoSyncSelectionEntity>): PhotoSelectionDiff = PhotoSelectionDiff(
    additions = records.count { it.desiredSynced && !it.confirmedSynced },
    removals = records.count { !it.desiredSynced && it.confirmedSynced }
)

fun stablePhotoId(mediaStoreId: Long): String = UUID.nameUUIDFromBytes("lifelink-photo:$mediaStoreId".toByteArray()).toString()
