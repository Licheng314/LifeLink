package com.liferadio.sync.ui.screens

/** Reads both the current nested app payload and pre-v3 legacy flat payloads. */
internal object UsageEventDisplayParser {
    data class Identity(val packageName: String, val displayName: String)

    fun identity(payload: Map<*, *>?): Identity {
        val app = payload?.get("app")
        val nestedApp = app as? Map<*, *>
        val packageName = sequenceOf(
            nestedApp?.get("package_name"),
            payload?.get("package_name"),
            payload?.get("package")
        ).mapNotNull { it?.toString()?.trim()?.takeIf(String::isNotEmpty) }
            .firstOrNull()
            ?: "unknown"
        val displayName = sequenceOf(
            nestedApp?.get("display_name"),
            payload?.get("display_name"),
            app?.takeUnless { it is Map<*, *> }
        ).mapNotNull { it?.toString()?.trim()?.takeIf(String::isNotEmpty) }
            .firstOrNull()
            ?: packageName.substringAfterLast('.').ifBlank { "未知应用" }
        return Identity(packageName, displayName)
    }
}
