package com.liferadio.sync.ui.screens

import com.liferadio.sync.data.model.RealTimeBackgroundItem
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Presentation only: central owns freshness and all current-state wording. */
object EventBackgroundDisplayPolicy {
    private val zone = ZoneId.of("Asia/Shanghai")
    private val formatter = DateTimeFormatter.ofPattern("MM-dd HH:mm")

    fun realTimeLabel(item: RealTimeBackgroundItem): String? {
        if (item.isStale && item.kind in setOf("device_online", "current_app")) return null
        val text = item.displayText?.trim().takeUnless { it.isNullOrEmpty() } ?: return null
        if (!item.isStale || text.contains("上次更新：")) return text
        val observed = runCatching { Instant.parse(item.observedAt).atZone(zone).format(formatter) }.getOrNull()
            ?: return text
        return "$text（上次更新：$observed）"
    }
}
