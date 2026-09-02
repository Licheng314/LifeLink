package com.liferadio.sync.data.model

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime

@JsonClass(generateAdapter = true)
data class WishCreate(
    @Json(name = "request_id") val requestId: String,
    @Json(name = "text") val text: String,
    @Json(name = "duration_days") val durationDays: Int = 3,
    @Json(name = "ai_tracking_enabled") val aiTrackingEnabled: Boolean = false
)

/** Only the user-authored text of an existing wish is mutable. */
@JsonClass(generateAdapter = true)
data class WishPatch(
    @Json(name = "text") val text: String
)

@JsonClass(generateAdapter = true)
data class WishDayAssessment(
    @Json(name = "evaluation") val evaluation: String   // "completed" | "not_completed"
)

@JsonClass(generateAdapter = true)
data class WishDay(
    @Json(name = "business_date") val businessDate: String,
    @Json(name = "evaluation") val evaluation: String?,    // null | "completed" | "not_completed"
    @Json(name = "evaluation_source") val evaluationSource: String?,  // null | "manual" | "automatic"
    @Json(name = "evaluated_at") val evaluatedAt: String?,  // null | UTC RFC3339
    @Json(name = "revision") val revision: Int
) {
    /**
     * Client-side day status calculated from the wish's business_day_snapshot.
     * Does NOT change based on current global shared settings.
     */
    fun dayStatus(nowBusinessDate: LocalDate): WishDayStatus {
        val date = runCatching { LocalDate.parse(businessDate) }.getOrNull() ?: return WishDayStatus.UNKNOWN
        return when {
            evaluation == "completed" -> WishDayStatus.COMPLETED
            evaluation == "not_completed" -> WishDayStatus.NOT_COMPLETED
            date.isAfter(nowBusinessDate) -> WishDayStatus.UNREACHED
            date == nowBusinessDate -> WishDayStatus.TODAY
            else -> WishDayStatus.PAST_PENDING  // date < nowBusinessDate, no evaluation
        }
    }
}

enum class WishDayStatus {
    UNREACHED, TODAY, PAST_PENDING, COMPLETED, NOT_COMPLETED, UNKNOWN
}

@JsonClass(generateAdapter = true)
data class SharedSettingsSnapshot(
    @Json(name = "timezone") val timezone: String,
    @Json(name = "day_start_hour") val dayStartHour: Int,
    @Json(name = "settings_version") val settingsVersion: Int
)

@JsonClass(generateAdapter = true)
data class Wish(
    @Json(name = "wish_id") val wishId: String,
    @Json(name = "text") val text: String,
    @Json(name = "duration_days") val durationDays: Int,
    @Json(name = "status") val status: String,             // "active" | "cancelled" | "archived"
    @Json(name = "created_at") val createdAt: String,
    @Json(name = "starts_on") val startsOn: String,       // date
    @Json(name = "ends_on") val endsOn: String,           // date
    @Json(name = "business_day_snapshot") val businessDaySnapshot: SharedSettingsSnapshot,
    @Json(name = "ai_tracking_enabled") val aiTrackingEnabled: Boolean,
    @Json(name = "cancelled_at") val cancelledAt: String? = null,
    @Json(name = "archived_at") val archivedAt: String? = null,
    @Json(name = "completed_days") val completedDays: Int,
    @Json(name = "wish_days") val wishDays: List<WishDay>
)

@JsonClass(generateAdapter = true)
data class WishListResponse(
    @Json(name = "wishes") val wishes: List<Wish>
)

// ==================== Trigger Models ====================

@JsonClass(generateAdapter = true)
data class TriggerTypeCatalogItem(
    @Json(name = "trigger_type") val triggerType: String,
    @Json(name = "display_name") val displayName: String,
    @Json(name = "config_version") val configVersion: Int,
    @Json(name = "target_scopes") val targetScopes: List<String>,
    @Json(name = "interval_minutes") val intervalMinutes: IntervalMinutesDef,
    @Json(name = "parameters_schema") val parametersSchema: Map<String, Any?>
)

@JsonClass(generateAdapter = true)
data class IntervalMinutesDef(
    @Json(name = "minimum") val minimum: Int,
    @Json(name = "allowed_values") val allowedValues: List<Int>
)

@JsonClass(generateAdapter = true)
data class TriggerTypeCatalogResponse(
    @Json(name = "trigger_types") val triggerTypes: List<TriggerTypeCatalogItem>
)

// Trigger parameter types (oneOf union via Moshi manual parsing is complex;
// we use a generic Map-based approach for the three subtypes)
@JsonClass(generateAdapter = true)
data class EventTriggerCreate(
    @Json(name = "request_id") val requestId: String,
    @Json(name = "wish_id") val wishId: String?,
    @Json(name = "trigger_type") val triggerType: String,
    @Json(name = "config_version") val configVersion: Int = 1,
    @Json(name = "parameters") val parameters: Map<String, Any?>,
    @Json(name = "interval_minutes") val intervalMinutes: Int,
    @Json(name = "enabled") val enabled: Boolean = true
)

@JsonClass(generateAdapter = true)
data class EventTriggerPatch(
    @Json(name = "parameters") val parameters: Map<String, Any?>? = null,
    @Json(name = "interval_minutes") val intervalMinutes: Int? = null,
    @Json(name = "enabled") val enabled: Boolean? = null
)

@JsonClass(generateAdapter = true)
data class EventTrigger(
    @Json(name = "trigger_id") val triggerId: String,
    @Json(name = "wish_id") val wishId: String?,
    @Json(name = "trigger_type") val triggerType: String,
    @Json(name = "config_version") val configVersion: Int,
    @Json(name = "parameters") val parameters: Map<String, Any?>,
    @Json(name = "interval_minutes") val intervalMinutes: Int,
    @Json(name = "enabled") val enabled: Boolean,
    @Json(name = "created_at") val createdAt: String,
    @Json(name = "updated_at") val updatedAt: String,
    @Json(name = "last_triggered_at") val lastTriggeredAt: String?
)

@JsonClass(generateAdapter = true)
data class EventTriggerListResponse(
    @Json(name = "triggers") val triggers: List<EventTrigger>
)

/**
 * Calculate the current business date based on a wish's own
 * business_day_snapshot, NOT the current global shared settings.
 */
object WishBusinessDay {
    fun at(snapshot: SharedSettingsSnapshot, instant: Instant): LocalDate {
        val zone = ZoneId.of(snapshot.timezone)
        val zdt = ZonedDateTime.ofInstant(instant, zone)
        return if (zdt.hour < snapshot.dayStartHour) {
            zdt.toLocalDate().minusDays(1)
        } else {
            zdt.toLocalDate()
        }
    }

    fun now(snapshot: SharedSettingsSnapshot): LocalDate = at(snapshot, Instant.now())
}

/** The event resources use the current shared boundary, unlike a wish's immutable snapshot. */
object EventBusinessDay {
    fun at(dayStartHour: Int, instant: Instant): LocalDate {
        require(dayStartHour in 0..23)
        val local = instant.atZone(ZoneId.of("Asia/Shanghai"))
        return if (local.hour < dayStartHour) local.toLocalDate().minusDays(1) else local.toLocalDate()
    }
}

@JsonClass(generateAdapter = true)
data class TimelineEvent(
    @Json(name = "timeline_event_id") val timelineEventId: String,
    @Json(name = "occurred_at") val occurredAt: String,
    @Json(name = "created_at") val createdAt: String,
    @Json(name = "event_key") val eventKey: String,
    @Json(name = "category") val category: String,
    @Json(name = "importance") val importance: String,
    @Json(name = "title") val title: String,
    @Json(name = "detail") val detail: String? = null,
    @Json(name = "source_kind") val sourceKind: String,
    @Json(name = "source_device_id") val sourceDeviceId: String? = null,
    @Json(name = "wish_id") val wishId: String? = null,
    @Json(name = "trigger_id") val triggerId: String? = null,
    @Json(name = "subject") val subject: Map<String, Any?> = emptyMap(),
    @Json(name = "evidence") val evidence: Map<String, Any?> = emptyMap(),
    @Json(name = "delivery") val delivery: AIDeliveryState? = null,
    @Json(name = "dedupe_key") val dedupeKey: String
)

@JsonClass(generateAdapter = true)
data class AIDeliveryState(
    @Json(name = "state") val state: String,
    @Json(name = "target_display_name") val targetDisplayName: String,
    @Json(name = "updated_at") val updatedAt: String
)

@JsonClass(generateAdapter = true)
data class GeneratedTextItem(
    @Json(name = "item_key") val itemKey: String,
    @Json(name = "text") val text: String
)

@JsonClass(generateAdapter = true)
data class EventBackgroundSection(
    @Json(name = "title") val title: String,
    @Json(name = "items") val items: List<GeneratedTextItem>
)

@JsonClass(generateAdapter = true)
data class EventBackgroundSummary(
    @Json(name = "wish") val wish: EventBackgroundSection,
    @Json(name = "device_and_apps") val deviceAndApps: EventBackgroundSection,
    @Json(name = "blacklist") val blacklist: EventBackgroundSection,
    @Json(name = "location_and_activity") val locationAndActivity: EventBackgroundSection
)

@JsonClass(generateAdapter = true)
data class AIUnderstandingGuide(
    @Json(name = "title") val title: String,
    @Json(name = "items") val items: List<GeneratedTextItem>,
    @Json(name = "timezone") val timezone: String,
    @Json(name = "real_time_valid_for_minutes") val realTimeValidForMinutes: Int
)

@JsonClass(generateAdapter = true)
data class EventBackgroundResponse(
    @Json(name = "business_date") val businessDate: String,
    @Json(name = "generated_at") val generatedAt: String,
    @Json(name = "background_summary") val backgroundSummary: EventBackgroundSummary,
    @Json(name = "ai_understanding") val aiUnderstanding: AIUnderstandingGuide,
    @Json(name = "real_time_items") val realTimeItems: List<RealTimeBackgroundItem>
)

@JsonClass(generateAdapter = true)
data class RealTimeBackgroundItem(
    @Json(name = "kind") val kind: String,
    @Json(name = "observed_at") val observedAt: String,
    @Json(name = "is_stale") val isStale: Boolean,
    @Json(name = "include_in_ai") val includeInAi: Boolean,
    @Json(name = "device_id") val deviceId: String? = null,
    @Json(name = "display_text") val displayText: String? = null
) {
    init {
        require(kind in setOf("device_online", "current_app", "current_location", "current_activity"))
        require(runCatching { java.time.Instant.parse(observedAt) }.isSuccess)
        require(!includeInAi || !isStale)
    }
}

@JsonClass(generateAdapter = true)
data class TimelineEventListResponse(
    @Json(name = "window") val window: Any? = null,
    @Json(name = "events") val events: List<TimelineEvent>
)

/**
 * Persistent cache for last successful wish list response.
 */
data class WishCacheEntry(
    val wishesJson: String,
    val refreshedAt: Long
)

/**
 * Persistent cache for last successful timeline response.
 */
data class TimelineCacheEntry(
    val timelineJson: String,
    val refreshedAt: Long
)
