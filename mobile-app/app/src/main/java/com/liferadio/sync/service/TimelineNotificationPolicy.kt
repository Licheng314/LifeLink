package com.liferadio.sync.service

internal object TimelineNotificationPolicy {
    fun shouldNotify(importance: String): Boolean = importance == "high" || importance == "normal"

    fun titlePrefix(importance: String): String = if (importance == "high") "重要事件" else "新事件"
}
