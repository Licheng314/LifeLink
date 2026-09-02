package com.liferadio.sync.ui.screens

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WishEditorRetryPolicyTest {
    @Test fun `only uncertain create failures retain idempotency id`() {
        assertTrue(WishEditorPolicy.shouldRetainCreateRequestId(null))
        assertTrue(WishEditorPolicy.shouldRetainCreateRequestId(408))
        assertTrue(WishEditorPolicy.shouldRetainCreateRequestId(429))
        assertTrue(WishEditorPolicy.shouldRetainCreateRequestId(500))
        assertFalse(WishEditorPolicy.shouldRetainCreateRequestId(409))
        assertFalse(WishEditorPolicy.shouldRetainCreateRequestId(410))
    }
}
