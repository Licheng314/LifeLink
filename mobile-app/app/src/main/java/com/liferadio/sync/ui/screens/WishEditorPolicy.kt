package com.liferadio.sync.ui.screens

/** Small, platform-free rules shared by the Compose template and JVM tests. */
object WishEditorPolicy {
    fun canWrite(cacheOnly: Boolean) = !cacheOnly
    fun isDurationMutable(editingWishId: String?) = editingWishId == null
    fun canEditReminder(wishStatus: String) = wishStatus == "active"
    fun partialReminderFailureMessage(reason: String) = "文字已保存，但提醒设置失败：$reason"
    fun shouldRetainCreateRequestId(statusCode: Int?): Boolean =
        statusCode == null || statusCode == 408 || statusCode == 429 || statusCode >= 500
}

/** `scheduled_reminder` has a fixed transport placeholder, never a user-facing cadence. */
object ScheduledReminderPolicy {
    const val DUMMY_INTERVAL_MINUTES = 1
    fun intervalFor(triggerType: String, requested: Int): Int =
        if (triggerType == "scheduled_reminder") DUMMY_INTERVAL_MINUTES else requested
}
