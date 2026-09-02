package com.liferadio.sync.ui.screens

import org.junit.Assert.assertEquals
import org.junit.Test

class UsageEventDisplayParserTest {
    @Test
    fun `reads current nested collector payload`() {
        val identity = UsageEventDisplayParser.identity(
            mapOf("app" to mapOf("display_name" to "系统桌面", "package_name" to "com.miui.home"))
        )

        assertEquals("com.miui.home", identity.packageName)
        assertEquals("系统桌面", identity.displayName)
    }

    @Test
    fun `reads legacy flat payload`() {
        val identity = UsageEventDisplayParser.identity(
            mapOf("app" to "微信", "package" to "com.tencent.mm")
        )

        assertEquals("com.tencent.mm", identity.packageName)
        assertEquals("微信", identity.displayName)
    }

    @Test
    fun `uses package suffix when display name is missing`() {
        val identity = UsageEventDisplayParser.identity(
            mapOf("package_name" to "com.example.reader")
        )

        assertEquals("com.example.reader", identity.packageName)
        assertEquals("reader", identity.displayName)
    }
}
