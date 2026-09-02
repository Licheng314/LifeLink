from __future__ import annotations

import json
import unittest
from pathlib import Path


CONTRACTS_DIR = Path(__file__).resolve().parents[1]
OPENAPI_PATH = CONTRACTS_DIR / "life-radio-api-v1.yaml"
FIXTURE_PATH = CONTRACTS_DIR / "fixtures" / "wish-event-system-v1.json"


def yaml_block(text: str, marker: str, indent: int) -> str:
    lines = text.splitlines()
    start = lines.index(" " * indent + marker)
    selected = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        selected.append(line)
    return "\n".join(selected)


class WishEventContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = OPENAPI_PATH.read_text(encoding="utf-8")
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_version_and_existing_ack_are_preserved(self) -> None:
        self.assertIn("version: 1.15.5", self.openapi)
        upload = yaml_block(self.openapi, "/v1/events/batches:", 2)
        self.assertIn("confirmed_event_ids", upload)
        self.assertIn("accepted_event_ids", upload)

    def test_wishes_are_fixed_and_idempotent(self) -> None:
        create = yaml_block(self.openapi, "WishCreate:", 4)
        wish = yaml_block(self.openapi, "Wish:", 4)
        endpoint = yaml_block(self.openapi, "/v1/wishes:", 2)
        self.assertIn("request_id:", create)
        self.assertIn("format: uuid", create)
        self.assertIn("enum: [3, 7]", create)
        self.assertIn("business_day_snapshot:", wish)
        self.assertIn("creation business day", wish)
        self.assertIn("wish_days:", wish)
        self.assertIn("minItems: 3", wish)
        self.assertIn("maxItems: 7", wish)
        self.assertIn("unarchived_wish_limit_reached", endpoint)
        self.assertIn("idempotency_conflict", endpoint)
        self.assertEqual(len(self.fixture["wish"]["wish_days"]), 3)

    def test_daily_assessments_reject_future_dates_and_allow_history_revision(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/wishes/{wish_id}/days/{business_date}:", 2)
        day = yaml_block(self.openapi, "WishDay:", 4)
        self.assertIn("future_wish_day", endpoint)
        self.assertIn("Historical revisions are allowed", endpoint)
        self.assertIn("enum: [manual, automatic, null]", day)
        self.assertEqual(self.fixture["historical_revision"]["response"]["revision"], 1)

    def test_central_lazy_finalize_owns_the_72_hour_grace_period(self) -> None:
        wish = yaml_block(self.openapi, "Wish:", 4)
        self.assertIn("72-hour grace period", wish)
        self.assertIn("idempotent lazy finalize", wish)
        self.assertIn("null evaluation becomes not_completed/automatic", " ".join(wish.split()))
        self.assertIn("exactly one wish.period_completed", wish)
        self.assertIn("not performed by the v1.13 minute scheduler", " ".join(wish.split()))
        self.assertIn("never authoritatively archive", wish)

    def test_manual_completion_requires_expiry_and_all_daily_results(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/wishes/{wish_id}/complete:", 2)
        completion = self.fixture["wish_manual_completion"]
        self.assertIn("registeredDeviceAuth", endpoint)
        self.assertIn("final wish business day has ended", endpoint)
        self.assertIn("wish_days_incomplete", endpoint)
        self.assertIn("missing_business_dates", endpoint)
        self.assertIn("72-hour automatic fallback", endpoint)
        self.assertEqual(completion["result"]["status"], "archived")
        self.assertEqual(completion["timeline_event"]["evidence"]["automatic_finalized"], False)

    def test_cancel_is_idempotent_for_cancelled_wishes_only(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/wishes/{wish_id}/cancel:", 2)
        self.assertIn("deprecated: true", endpoint)
        self.assertIn("Legacy compatibility only", endpoint)
        self.assertIn("returns this current final record with 200", endpoint)
        self.assertIn("normally archived", endpoint)

    def test_wish_text_patch_is_closed_and_preserves_all_non_text_state(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/wishes/{wish_id}:", 2)
        patch = yaml_block(self.openapi, "WishTextPatch:", 4)
        self.assertIn("updateWishText", endpoint)
        self.assertIn("registeredDeviceAuth", endpoint)
        self.assertIn("active, cancelled, and archived", endpoint)
        self.assertIn("WishTextPatch", endpoint)
        self.assertIn("additionalProperties: false", patch)
        self.assertIn("required: [text]", patch)
        self.assertIn("only mutable wish field", patch)
        self.assertEqual(self.fixture["wish_text_patch"]["request"], {"text": "Read one chapter before bed"})
        self.assertEqual(self.fixture["wish_text_patch"]["response"]["duration_days"], 3)
        self.assertIn("updateWishTextViaPost", endpoint)
        self.assertIn("Semantically identical to PATCH", endpoint)

    def test_post_transport_aliases_cover_wish_and_trigger_updates_and_deletes(self) -> None:
        wish = yaml_block(self.openapi, "/v1/wishes/{wish_id}:", 2)
        wish_delete = yaml_block(self.openapi, "/v1/wishes/{wish_id}/delete:", 2)
        trigger = yaml_block(self.openapi, "/v1/event-triggers/{trigger_id}:", 2)
        trigger_delete = yaml_block(self.openapi, "/v1/event-triggers/{trigger_id}/delete:", 2)
        self.assertIn("updateWishTextViaPost", wish)
        self.assertIn("deleteWishViaPost", wish_delete)
        self.assertIn("updateEventTriggerViaPost", trigger)
        self.assertIn("deleteEventTriggerViaPost", trigger_delete)
        for block in (wish, wish_delete, trigger, trigger_delete):
            self.assertIn("registeredDeviceAuth", block)

    def test_wish_delete_is_atomic_idempotent_and_blocks_create_revival(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/wishes/{wish_id}:", 2)
        create = yaml_block(self.openapi, "/v1/wishes:", 2)
        deletion = self.fixture["wish_deletion"]
        self.assertIn("deleteWish", endpoint)
        self.assertIn("Permanently delete", endpoint)
        self.assertIn("registeredDeviceAuth", endpoint)
        self.assertIn("wish_days", endpoint)
        self.assertIn("event_triggers", endpoint)
        self.assertIn("wish.created, wish.cancelled, wish.period_completed, wish.result_revised", endpoint)
        self.assertIn("wish_id and trigger_id to null", endpoint)
        self.assertIn("original device events are never changed", endpoint)
        self.assertIn("same request_id plus same normalized creation body", endpoint)
        self.assertIn("'410':", create)
        self.assertIn("wish_deleted", create)
        self.assertIn("different normalized body", create)
        self.assertEqual(deletion["response_status"], 204)
        self.assertEqual(deletion["repeat_response_status"], 204)
        self.assertRegex(deletion["tombstone"]["creation_body_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(deletion["retained_real_trigger_event"]["wish_id"], None)
        self.assertEqual(deletion["retained_real_trigger_event"]["trigger_id"], None)
        self.assertEqual(deletion["delayed_create_retry"]["same_normalized_body_code"], "wish_deleted")
        self.assertEqual(deletion["delayed_create_retry"]["different_normalized_body_code"], "idempotency_conflict")

    def test_timeline_has_range_filters_stable_order_and_no_cursor(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/timeline-events:", 2)
        event = yaml_block(self.openapi, "TimelineEvent:", 4)
        self.assertIn("TimelineCategory", endpoint)
        self.assertIn("TimelineWishId", endpoint)
        self.assertIn("TimelineImportance", endpoint)
        self.assertIn("no cursor", endpoint)
        self.assertIn("occurred_at DESC, timeline_event_id DESC", endpoint)
        self.assertIn("dedupe_key:", event)
        self.assertEqual(self.fixture["timeline"]["events"][0]["event_key"], "wish.result_revised")
        self.assertEqual(self.fixture["timeline"]["events"][1]["event_key"], "wish.period_completed")
        self.assertIn("wish.created", event)
        self.assertIn("wish.cancelled", event)
        self.assertIn("wish.period_completed", event)
        self.assertIn("wish.result_revised", event)
        self.assertIn("Append-only", event)

    def test_trigger_catalog_and_instances_are_strict_and_not_executors(self) -> None:
        catalog = yaml_block(self.openapi, "/v1/trigger-types:", 2)
        instances = yaml_block(self.openapi, "/v1/event-triggers:", 2)
        parameters = yaml_block(self.openapi, "TriggerParameters:", 4)
        blacklist = yaml_block(self.openapi, "BlacklistUsageMilestoneParameters:", 4)
        late = yaml_block(self.openapi, "LateUsageMilestoneParameters:", 4)
        self.assertIn("Read-only catalog", catalog)
        self.assertIn("createEventTrigger", instances)
        self.assertIn("additionalProperties: false", blacklist)
        self.assertIn("enum: [all, pc, android, web]", blacklist)
        self.assertIn("reserved value all", late)
        self.assertIn("BlacklistUsageMilestoneParameters", parameters)
        self.assertEqual(self.fixture["trigger"]["trigger_type"], "blacklist_usage_milestone")
        self.assertRegex(self.fixture["trigger_create"]["request_id"], r"^[0-9a-f-]{36}$")
        self.assertNotIn("location", parameters.lower())

    def test_trigger_creation_has_request_id_idempotency(self) -> None:
        create = yaml_block(self.openapi, "EventTriggerCreate:", 4)
        endpoint = yaml_block(self.openapi, "/v1/event-triggers:", 2)
        self.assertIn("request_id:", create)
        self.assertIn("format: uuid", create)
        self.assertIn("idempotency_conflict", endpoint)

    def test_wish_linked_triggers_follow_terminal_wish_state(self) -> None:
        create_endpoint = yaml_block(self.openapi, "/v1/event-triggers:", 2)
        update_endpoint = yaml_block(self.openapi, "/v1/event-triggers/{trigger_id}:", 2)
        create = yaml_block(self.openapi, "EventTriggerCreate:", 4)
        patch = yaml_block(self.openapi, "EventTriggerPatch:", 4)
        trigger = yaml_block(self.openapi, "EventTrigger:", 4)
        self.assertIn("status=active wish", create_endpoint)
        self.assertIn("status=active wish", create)
        self.assertIn("cancelled or normally archived", update_endpoint)
        self.assertIn("cannot be re-enabled", patch)
        self.assertIn("atomically sets enabled=false in the same transaction", trigger)
        self.assertIn("retains this trigger record", trigger)
        self.assertIn("may still be deleted", trigger)

    def test_new_resource_reads_allow_read_or_registered_device_tokens_only(self) -> None:
        for path in ("/v1/wishes:", "/v1/timeline-events:", "/v1/trigger-types:", "/v1/event-triggers:"):
            block = yaml_block(self.openapi, path, 2)
            self.assertIn("- readBearerAuth: []", block)
            self.assertIn("- registeredDeviceAuth: []", block)
            self.assertNotIn("- bearerAuth: []", block)

    def test_v113_background_and_scheduler_contracts_are_central_read_models(self) -> None:
        background = yaml_block(self.openapi, "/v1/event-background:", 2)
        settings = yaml_block(self.openapi, "SharedSettings:", 4)
        periodic = yaml_block(self.openapi, "PeriodicSummarySchedule:", 4)
        self.assertIn("getEventBackground", background)
        self.assertIn("readBearerAuth", background)
        self.assertIn("registeredDeviceAuth", background)
        self.assertIn("latest real-time background entry for WebUI", background)
        self.assertIn("include_in_ai=true", background)
        self.assertIn("AI cursor, credentials, provider, model, or delivery retry state", background)
        self.assertIn("sleep_local_time", settings)
        self.assertIn("ai_display_name", settings)
        self.assertIn("morning_report", settings)
        self.assertIn("evening_report", settings)
        self.assertIn("periodic_summary", settings)
        self.assertIn("enum: [30, 60, 120, 180, 240]", periodic)
        self.assertEqual(self.fixture["shared_event_settings"]["periodic_summary"]["interval_minutes"], 120)
        self.assertEqual(self.fixture["event_background"]["ai_understanding"]["real_time_valid_for_minutes"], 15)
        self.assertFalse(self.fixture["shared_event_settings"]["morning_report"]["enabled"])

    def test_v113_background_has_central_display_sections_and_dual_freshness_rules(self) -> None:
        summary = yaml_block(self.openapi, "EventBackgroundSummary:", 4)
        guide = yaml_block(self.openapi, "AIUnderstandingGuide:", 4)
        realtime = yaml_block(self.openapi, "RealTimeBackgroundItem:", 4)
        background = self.fixture["event_background"]
        for section in ("wish", "device_and_apps", "blacklist", "location_and_activity"):
            self.assertIn(section + ":", summary)
            self.assertTrue(background["background_summary"][section]["items"][0]["text"])
        self.assertIn("title", guide)
        self.assertIn("items", guide)
        self.assertGreaterEqual(len(background["ai_understanding"]["items"]), 2)
        self.assertIn("is_stale", realtime)
        self.assertIn("include_in_ai", realtime)
        self.assertIn("current_app", realtime)
        self.assertTrue(background["real_time_items"][0]["include_in_ai"])
        self.assertFalse(background["real_time_items"][0]["is_stale"])
        self.assertTrue(background["real_time_items"][1]["is_stale"])
        self.assertFalse(background["real_time_items"][1]["include_in_ai"])
        self.assertIn("trailing 60 minutes", summary)
        activity = background["background_summary"]["location_and_activity"]["items"][0]
        self.assertTrue(activity["item_key"].startswith("activity.interval:"))
        self.assertEqual(activity["text"], "14:45–15:30 步行，持续 45 分钟。")

    def test_v113_generated_events_have_fixed_system_rules_and_delivery_attachment(self) -> None:
        timeline = yaml_block(self.openapi, "TimelineEvent:", 4)
        evidence = yaml_block(self.openapi, "SystemMilestoneEvidence:", 4)
        delivery = yaml_block(self.openapi, "AIDeliveryState:", 4)
        scheduled = yaml_block(self.openapi, "ScheduledReminderParameters:", 4)
        self.assertIn("system.device_usage_milestone", timeline)
        self.assertIn("report.periodic", timeline)
        self.assertIn("System milestone rules are fixed central rules", timeline)
        self.assertIn("statistics_window", timeline)
        self.assertIn("delivery", timeline)
        self.assertIn("device_usage_hourly", evidence)
        self.assertIn("late_online_half_hourly", evidence)
        self.assertIn("late_online_hourly", evidence)
        self.assertIn("enum: [pending, sent, not_configured, failed]", delivery)
        self.assertIn("v1.13 central generation uses not_configured only", delivery)
        self.assertIn("reminder_local_time", scheduled)
        self.assertIn("more than 15 minutes", scheduled)
        self.assertEqual(self.fixture["scheduled_wish_reminder"]["trigger_type"], "scheduled_reminder")
        self.assertTrue(self.fixture["scheduled_wish_reminder"]["timeline_event"]["title"].startswith("心愿提醒·"))
        self.assertEqual(self.fixture["report_event"]["delivery"]["state"], "not_configured")
        self.assertTrue(self.fixture["report_event"]["evidence"]["body"])
        self.assertEqual(self.fixture["system_milestone"]["evidence"]["aggregate_scope"], "all_devices")
        self.assertEqual(self.fixture["late_online_milestone"]["evidence"]["rule"], "late_online_half_hourly")
        self.assertEqual(self.fixture["late_online_milestone"]["evidence"]["threshold_minutes"], 30)
        self.assertEqual(self.fixture["late_online_milestone"]["evidence"]["online_device_ids"], ["desktop-b76c5155-47e8-4c39-9bd4-2d23388cd35f"])


if __name__ == "__main__":
    unittest.main()
