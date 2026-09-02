package com.liferadio.sync.ui.screens

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WishEditorPolicyTest {
    @Test fun `edit mode locks fixed duration and cache mode blocks writes`() {
        assertTrue(WishEditorPolicy.isDurationMutable(null))
        assertFalse(WishEditorPolicy.isDurationMutable("wish-1"))
        assertTrue(WishEditorPolicy.canWrite(false))
        assertFalse(WishEditorPolicy.canWrite(true))
        assertTrue(WishEditorPolicy.canEditReminder("active"))
        assertFalse(WishEditorPolicy.canEditReminder("archived"))
        assertFalse(WishEditorPolicy.canEditReminder("cancelled"))
    }

    @Test fun `partial reminder failure explains that text was already saved`() {
        assertTrue(WishEditorPolicy.partialReminderFailureMessage("连接失败").contains("文字已保存"))
    }
}
