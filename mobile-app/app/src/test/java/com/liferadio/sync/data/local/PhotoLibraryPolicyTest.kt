package com.liferadio.sync.data.local

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PhotoLibraryPolicyTest {
    @Test fun `diff separates additions and removals`() {
        val records = listOf(
            selection("one", desired = true, confirmed = false),
            selection("two", desired = false, confirmed = true),
            selection("three", desired = true, confirmed = true)
        )
        val diff = photoSelectionDiff(records)
        assertEquals(1, diff.additions)
        assertEquals(1, diff.removals)
        assertTrue(diff.hasChanges)
    }

    @Test fun `unchanged and unavailable rows produce no deletion`() {
        val diff = photoSelectionDiff(listOf(selection("kept", desired = true, confirmed = true)))
        assertFalse(diff.hasChanges)
        assertEquals(0, photoSelectionDiff(emptyList()).removals)
    }

    @Test fun `stable photo id is repeatable and source local`() {
        assertEquals(stablePhotoId(42), stablePhotoId(42))
        assertFalse(stablePhotoId(42) == stablePhotoId(43))
    }

    private fun selection(id: String, desired: Boolean, confirmed: Boolean) = PhotoSyncSelectionEntity(
        photoId = id, mediaStoreId = id.hashCode().toLong(), desiredSynced = desired, confirmedSynced = confirmed, businessDate = "2026-09-18"
    )
}
