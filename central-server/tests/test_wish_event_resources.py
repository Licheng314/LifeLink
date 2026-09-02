import http.client
import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from central.config import CentralConfig
from central.domain import content_hash
from central.http import create_server
from central.storage import (
    CentralStore, FutureWishDay, TriggerConfigurationConflict, WishDayNotFound,
    WishDaysIncomplete, WishDeleted, WishLimitReached, WishNotCompletable,
)


class WishEventResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.temp.name) / "central.sqlite3", {})
        self.now = datetime(2026, 8, 9, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, text="Read"):
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "text": text, "duration_days": 3, "ai_tracking_enabled": False}
        return self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text=text,
                                      duration_days=3, ai_tracking_enabled=False,
                                      source_device_id="desktop-a", now=self.now)

    def test_fixed_days_limit_and_request_idempotency(self):
        first = self.create()
        self.assertEqual(len(first["wish_days"]), 3)
        self.assertEqual(first["starts_on"], "2026-08-09")
        self.assertEqual(first["ends_on"], "2026-08-11")
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "text": "Repeat", "duration_days": 3, "ai_tracking_enabled": False}
        created = self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text="Repeat", duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now)
        repeat = self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text="Repeat", duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now)
        self.assertEqual(repeat["wish_id"], created["wish_id"])
        self.create("Walk")
        with self.assertRaises(WishLimitReached):
            self.create("Fourth")

    def test_concurrent_fourth_creation_leaves_no_orphan_days(self):
        gate = threading.Barrier(5); outcomes = []
        def create_one(index):
            request_id = str(uuid.uuid4()); body = {"request_id": request_id, "text": f"W{index}", "duration_days": 3, "ai_tracking_enabled": False}
            gate.wait()
            try:
                outcomes.append(self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text=body["text"], duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now))
            except WishLimitReached: outcomes.append(None)
        workers = [threading.Thread(target=create_one, args=(index,)) for index in range(4)]
        for worker in workers: worker.start()
        gate.wait()
        for worker in workers: worker.join()
        self.assertEqual(len([item for item in outcomes if item]), 3)
        self.assertEqual(sum(len(item["wish_days"]) for item in outcomes if item), 9)

    def test_reinitialize_preserves_existing_shared_and_blacklist_data(self):
        self.store.update_shared_day_start_hour(4)
        rule = self.store.create_blacklist_rule("app", "Example", "Example")
        restored = CentralStore(Path(self.temp.name) / "central.sqlite3", {})
        self.assertEqual(restored.get_shared_settings()["day_start_hour"], 4)
        self.assertIn(rule["rule_id"], {item["rule_id"] for item in restored.list_blacklist_rules()})

    def test_wish_keeps_its_cross_day_snapshot_after_global_setting_changes(self):
        self.store.update_shared_day_start_hour(4)
        created_at = datetime(2026, 8, 9, 18, tzinfo=timezone.utc)
        first_request = str(uuid.uuid4())
        first_body = {"request_id": first_request, "text": "Before", "duration_days": 3, "ai_tracking_enabled": False}
        first = self.store.create_wish(
            request_id=first_request, request_hash=content_hash(first_body), text="Before",
            duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=created_at,
        )
        self.store.update_shared_day_start_hour(0)
        second_request = str(uuid.uuid4())
        second_body = {"request_id": second_request, "text": "After", "duration_days": 3, "ai_tracking_enabled": False}
        second = self.store.create_wish(
            request_id=second_request, request_hash=content_hash(second_body), text="After",
            duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=created_at,
        )
        self.assertEqual(first["business_day_snapshot"]["day_start_hour"], 4)
        self.assertEqual(first["starts_on"], "2026-08-09")
        self.assertEqual(second["business_day_snapshot"]["day_start_hour"], 0)
        self.assertEqual(second["starts_on"], "2026-08-10")

    def test_future_rejected_and_lazy_finalize_appends_once(self):
        wish = self.create()
        current_day = self.store.assess_wish_day(
            wish_id=wish["wish_id"], business_date=wish["starts_on"],
            evaluation="completed", source_device_id="desktop-a", now=self.now,
        )
        self.assertEqual(current_day["evaluation"], "completed")
        with self.assertRaises(FutureWishDay):
            self.store.assess_wish_day(wish_id=wish["wish_id"], business_date=wish["wish_days"][1]["business_date"],
                                       evaluation="completed", source_device_id="desktop-a", now=self.now)
        with self.assertRaises(ValueError):
            self.store.assess_wish_day(wish_id=wish["wish_id"], business_date="2026-8-10",
                                       evaluation="completed", source_device_id="desktop-a", now=self.now)
        deadline = self.store._business_day_end_utc(wish["ends_on"], 0, "Asia/Shanghai") + timedelta(hours=72)
        just_before = self.store.get_wish(wish["wish_id"], now=deadline - timedelta(microseconds=1))
        self.assertEqual(just_before["status"], "active")
        archived = self.store.get_wish(wish["wish_id"], now=deadline)
        self.assertEqual(archived["status"], "archived")
        self.assertEqual([day["evaluation"] for day in archived["wish_days"]],
                         ["completed", "not_completed", "not_completed"])
        events = self.store.list_timeline(datetime(2026, 8, 1, tzinfo=timezone.utc), deadline + timedelta(days=1))["events"]
        self.assertEqual([event["event_key"] for event in events].count("wish.period_completed"), 1)

    def test_manual_completion_requires_period_end_and_all_results(self):
        wish = self.create("Finish")
        period_end = self.store._business_day_end_utc(wish["ends_on"], 0, "Asia/Shanghai")
        with self.assertRaises(WishNotCompletable):
            self.store.complete_wish(wish["wish_id"], source_device_id="desktop-a", now=period_end - timedelta(seconds=1))
        with self.assertRaises(WishDaysIncomplete) as missing:
            self.store.complete_wish(wish["wish_id"], source_device_id="desktop-a", now=period_end)
        self.assertEqual(missing.exception.missing_business_dates, [day["business_date"] for day in wish["wish_days"]])
        for day in wish["wish_days"]:
            self.store.assess_wish_day(
                wish_id=wish["wish_id"], business_date=day["business_date"], evaluation="completed",
                source_device_id="desktop-a", now=period_end,
            )
        completed = self.store.complete_wish(
            wish["wish_id"], source_device_id="desktop-a", now=period_end + timedelta(minutes=1),
        )
        self.assertEqual(completed["status"], "archived")
        repeated = self.store.complete_wish(
            wish["wish_id"], source_device_id="desktop-a", now=period_end + timedelta(minutes=2),
        )
        self.assertEqual(repeated["archived_at"], completed["archived_at"])
        events = self.store.list_timeline(self.now - timedelta(days=1), period_end + timedelta(days=1))["events"]
        event = next(item for item in events if item["event_key"] == "wish.period_completed")
        self.assertEqual(event["title"], "「Finish」心愿已完成")
        self.assertEqual(event["detail"], "完成的天数 3/3，恭喜全部完成！")
        self.assertFalse(event["evidence"]["automatic_finalized"])

    def test_trigger_parameters_are_strict_and_persist(self):
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "trigger_type": "blacklist_usage_milestone", "config_version": 1,
                "parameters": {"platform_scope": "android"}, "interval_minutes": 60, "enabled": True}
        trigger = self.store.create_trigger(request_id=request_id, request_hash=content_hash(body), wish_id=None,
                                            trigger_type=body["trigger_type"], config_version=1,
                                            parameters=body["parameters"], interval_minutes=60, enabled=True, now=self.now)
        self.assertEqual(trigger["parameters"], {"platform_scope": "android"})
        wish = self.create("Linked")
        linked = self.store.create_trigger(request_id=str(uuid.uuid4()), request_hash="linked", wish_id=wish["wish_id"], trigger_type=body["trigger_type"], config_version=1, parameters=body["parameters"], interval_minutes=60, enabled=True, now=self.now)
        self.store.cancel_wish(wish["wish_id"], source_device_id="desktop-a", now=self.now)
        self.assertFalse(next(item for item in self.store.list_triggers() if item["trigger_id"] == linked["trigger_id"])["enabled"])
        with self.assertRaises(TriggerConfigurationConflict):
            self.store.patch_trigger(linked["trigger_id"], {"enabled": True}, now=self.now)

    def test_manual_assessment_is_idempotent_and_non_fixed_date_is_explicit(self):
        wish = self.create()
        assessment_now = datetime(2026, 8, 10, 1, tzinfo=timezone.utc)
        first = self.store.assess_wish_day(wish_id=wish["wish_id"], business_date=wish["starts_on"],
                                            evaluation="completed", source_device_id="desktop-a", now=assessment_now)
        repeated = self.store.assess_wish_day(wish_id=wish["wish_id"], business_date=wish["starts_on"],
                                               evaluation="completed", source_device_id="desktop-a", now=assessment_now + timedelta(hours=1))
        self.assertEqual(repeated, first)
        with self.assertRaises(WishDayNotFound):
            self.store.assess_wish_day(wish_id=wish["wish_id"], business_date="2026-08-20",
                                       evaluation="completed", source_device_id="desktop-a", now=assessment_now)

    def test_cancel_and_archived_revision_append_each_lifecycle_event_once(self):
        cancelled = self.create("Cancel")
        first_cancel = self.store.cancel_wish(cancelled["wish_id"], source_device_id="desktop-a", now=self.now)
        repeated_cancel = self.store.cancel_wish(
            cancelled["wish_id"], source_device_id="desktop-a", now=self.now + timedelta(hours=1),
        )
        self.assertEqual(repeated_cancel, first_cancel)

        archived = self.create("Revise")
        deadline = self.store._business_day_end_utc(
            archived["ends_on"], archived["business_day_snapshot"]["day_start_hour"],
            archived["business_day_snapshot"]["timezone"],
        ) + timedelta(hours=72)
        self.store.get_wish(archived["wish_id"], now=deadline)
        revised = self.store.assess_wish_day(
            wish_id=archived["wish_id"], business_date=archived["starts_on"],
            evaluation="completed", source_device_id="desktop-a", now=deadline + timedelta(minutes=1),
        )
        repeated = self.store.assess_wish_day(
            wish_id=archived["wish_id"], business_date=archived["starts_on"],
            evaluation="completed", source_device_id="desktop-a", now=deadline + timedelta(minutes=2),
        )
        self.assertEqual(revised, repeated)
        self.assertEqual(revised["revision"], 1)

        events = self.store.list_timeline(
            datetime(2026, 8, 1, tzinfo=timezone.utc), deadline + timedelta(days=1),
        )["events"]
        self.assertEqual(sum(event["event_key"] == "wish.cancelled" for event in events), 1)
        self.assertEqual(sum(event["event_key"] == "wish.result_revised" for event in events), 1)

    def test_patch_text_changes_nothing_else_and_creates_no_timeline_event(self):
        wish = self.create("Original")
        before = self.store.list_timeline(
            datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 20, tzinfo=timezone.utc),
        )["events"]
        updated = self.store.patch_wish_text(wish["wish_id"], "Changed", now=self.now)
        self.assertEqual(updated["text"], "Changed")
        self.assertEqual(updated["wish_days"], wish["wish_days"])
        self.assertEqual(updated["business_day_snapshot"], wish["business_day_snapshot"])
        self.assertEqual(updated["starts_on"], wish["starts_on"])
        self.assertEqual(updated["ends_on"], wish["ends_on"])
        after = self.store.list_timeline(
            datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 20, tzinfo=timezone.utc),
        )["events"]
        self.assertEqual(after, before)

    def test_patch_due_wish_finalizes_before_changing_text_without_edit_timeline(self):
        wish = self.create("Due edit")
        deadline = self.store._business_day_end_utc(
            wish["ends_on"], wish["business_day_snapshot"]["day_start_hour"],
            wish["business_day_snapshot"]["timezone"],
        ) + timedelta(hours=72)
        updated = self.store.patch_wish_text(wish["wish_id"], "Final wording", now=deadline)
        self.assertEqual(updated["text"], "Final wording")
        self.assertEqual(updated["status"], "archived")
        self.assertEqual([day["evaluation"] for day in updated["wish_days"]], ["not_completed"] * 3)
        events = self.store.list_timeline(
            datetime(2026, 8, 1, tzinfo=timezone.utc), deadline + timedelta(days=1),
        )["events"]
        self.assertEqual([event["event_key"] for event in events].count("wish.period_completed"), 1)
        self.assertEqual([event["event_key"] for event in events].count("wish.created"), 1)
        self.assertEqual(len(events), 2)

    def test_delete_removes_all_wish_data_but_keeps_raw_and_trigger_history_unlinked(self):
        wish = self.create("Delete me")
        trigger = self.store.create_trigger(
            request_id=str(uuid.uuid4()), request_hash="trigger", wish_id=wish["wish_id"],
            trigger_type="blacklist_usage_milestone", config_version=1,
            parameters={"platform_scope": "pc"}, interval_minutes=60, enabled=True, now=self.now,
        )
        with self.store._connection() as connection:
            self.store._append_timeline(
                connection, occurred_at="2026-08-09T00:10:00Z", event_key="blacklist_usage_milestone",
                category="trigger", importance="normal", title="Triggered", source_kind="central",
                source_device_id=None, wish_id=wish["wish_id"], trigger_id=trigger["trigger_id"],
                subject={}, evidence={}, dedupe_key="test-trigger-history",
            )
            connection.execute(
                "INSERT INTO devices(device_id, platform, display_name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                ("raw-device", "desktop", "Raw", "2026-08-09T00:00:00Z", "2026-08-09T00:00:00Z"),
            )
            connection.execute(
                """INSERT INTO events(event_id, device_id, occurred_at, event_type, source_json, duration_seconds,
                   revision, payload_json, event_json, content_hash, is_mutable, received_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, 0, ?, ?)""",
                ("raw-event", "raw-device", "2026-08-09T00:00:00Z", "manual.note", "{}", "{}", "{}", "raw", "2026-08-09T00:00:00Z", "2026-08-09T00:00:00Z"),
            )
        self.assertTrue(self.store.delete_wish(wish["wish_id"], now=self.now))
        self.assertIsNone(self.store.get_wish(wish["wish_id"], now=self.now))
        self.assertNotIn(trigger["trigger_id"], {item["trigger_id"] for item in self.store.list_triggers()})
        timeline = self.store.list_timeline(
            datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 20, tzinfo=timezone.utc),
        )["events"]
        self.assertEqual([event["event_key"] for event in timeline], ["blacklist_usage_milestone"])
        self.assertIsNone(timeline[0]["wish_id"])
        self.assertIsNone(timeline[0]["trigger_id"])
        self.assertIsNotNone(self.store.fetch_event("raw-event"))
        with self.store._connection() as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(deleted_wish_tombstones)")]
            self.assertEqual(columns, ["wish_id", "request_id", "request_hash", "deleted_at"])

    def test_delete_is_idempotent_and_old_create_retry_never_revives(self):
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "text": "Never revive", "duration_days": 3, "ai_tracking_enabled": False}
        wish = self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text=body["text"],
                                      duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now)
        self.assertTrue(self.store.delete_wish(wish["wish_id"], now=self.now))
        self.assertFalse(self.store.delete_wish(wish["wish_id"], now=self.now))
        with self.assertRaises(WishDeleted):
            self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text=body["text"],
                                   duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now)
        altered = {**body, "text": "Different"}
        with self.assertRaisesRegex(Exception, "different request content"):
            self.store.create_wish(request_id=request_id, request_hash=content_hash(altered), text=altered["text"],
                                   duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now)

    def test_delete_accepts_cancelled_and_normally_archived_wishes(self):
        cancelled = self.create("Cancelled")
        self.store.cancel_wish(cancelled["wish_id"], source_device_id="desktop-a", now=self.now)
        self.assertTrue(self.store.delete_wish(cancelled["wish_id"], now=self.now))

        archived = self.create("Archived")
        deadline = self.store._business_day_end_utc(
            archived["ends_on"], archived["business_day_snapshot"]["day_start_hour"],
            archived["business_day_snapshot"]["timezone"],
        ) + timedelta(hours=72)
        self.assertEqual(self.store.get_wish(archived["wish_id"], now=deadline)["status"], "archived")
        self.assertTrue(self.store.delete_wish(archived["wish_id"], now=deadline))

    def test_delete_and_old_create_retry_are_serialized(self):
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "text": "Concurrent", "duration_days": 3, "ai_tracking_enabled": False}
        wish = self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text=body["text"],
                                      duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now)
        gate, outcomes = threading.Barrier(3), []
        def delete():
            gate.wait(); outcomes.append(("delete", self.store.delete_wish(wish["wish_id"], now=self.now)))
        def retry():
            gate.wait()
            try:
                outcome = self.store.create_wish(request_id=request_id, request_hash=content_hash(body), text=body["text"], duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a", now=self.now)
                outcomes.append(("retry", outcome["wish_id"]))
            except WishDeleted:
                outcomes.append(("retry", "deleted"))
        workers = [threading.Thread(target=delete), threading.Thread(target=retry)]
        for worker in workers: worker.start()
        gate.wait()
        for worker in workers: worker.join()
        self.assertIsNone(self.store.get_wish(wish["wish_id"], now=self.now))
        self.assertEqual(self.store.list_wishes(include_archived=True, now=self.now), [])
        self.assertEqual(len(outcomes), 2)


class WishEventHttpTests(unittest.TestCase):
    device_token = "wish-device-token-0123456789-ABCDEFGHIJKLMN"
    read_token = "wish-read-token-0123456789-ABCDEFGHIJKLMNO"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = CentralConfig(database_path=Path(self.temp.name) / "central.sqlite3", host="127.0.0.1", port=0,
                               token_bindings={self.device_token: "desktop-wishes"}, read_token=self.read_token)
        self.server = create_server(config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method, path, token=None, body=None, raw_response=False):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        raw = json.dumps(body).encode() if body is not None else None
        if raw: headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, raw, headers); response = connection.getresponse(); result = response.read()
        connection.close(); return response.status, result if raw_response else (json.loads(result.decode()) if result else {})

    def test_permissions_and_main_error_mappings(self):
        self.assertEqual(self.request("GET", "/v1/wishes")[0], 401)
        self.assertEqual(self.request("GET", "/v1/wishes", self.read_token)[0], 200)
        self.assertEqual(self.request("GET", "/v1/read/devices", self.device_token)[0], 403)
        request_id = str(uuid.uuid4())
        status, wish = self.request("POST", "/v1/wishes", self.device_token, {"request_id": request_id, "text": "HTTP"})
        self.assertEqual(status, 201)
        self.assertEqual(self.request("PUT", f"/v1/wishes/{wish['wish_id']}/days/2099-01-01", self.device_token, {"evaluation": "completed"})[0], 400)
        self.assertEqual(self.request("POST", f"/v1/wishes/{wish['wish_id']}/cancel", self.device_token)[0], 200)
        trigger_body = {"request_id": str(uuid.uuid4()), "wish_id": wish["wish_id"], "trigger_type": "blacklist_usage_milestone", "config_version": 1, "parameters": {"platform_scope": "pc"}, "interval_minutes": 60}
        self.assertEqual(self.request("POST", "/v1/event-triggers", self.device_token, trigger_body)[0], 409)
        trigger_body["wish_id"] = None; trigger_body["config_version"] = True
        self.assertEqual(self.request("POST", "/v1/event-triggers", self.device_token, trigger_body)[0], 400)

    def test_manual_complete_route_requires_device_and_reports_missing_dates(self):
        created_at = datetime.now(timezone.utc) - timedelta(days=4)
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "text": "HTTP finish", "duration_days": 3, "ai_tracking_enabled": False}
        wish = self.server.store.create_wish(
            request_id=request_id, request_hash=content_hash(body), text=body["text"], duration_days=3,
            ai_tracking_enabled=False, source_device_id="desktop-wishes", now=created_at,
        )
        path = f"/v1/wishes/{wish['wish_id']}/complete"
        self.assertEqual(self.request("POST", path)[0], 401)
        status, error = self.request("POST", path, self.device_token)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "wish_days_incomplete")
        self.assertEqual(error["missing_business_dates"], [day["business_date"] for day in wish["wish_days"]])
        reached = datetime.now(timezone.utc)
        for day in wish["wish_days"]:
            self.server.store.assess_wish_day(
                wish_id=wish["wish_id"], business_date=day["business_date"], evaluation="completed",
                source_device_id="desktop-wishes", now=reached,
            )
        status, completed = self.request("POST", path, self.device_token)
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "archived")

    def test_wish_patch_and_delete_require_device_auth_and_are_strict(self):
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "text": "HTTP edit"}
        status, wish = self.request("POST", "/v1/wishes", self.device_token, body)
        self.assertEqual(status, 201)
        path = f"/v1/wishes/{wish['wish_id']}"
        prefixed_path = f"/v1/wishes/unrelated/{wish['wish_id']}"
        self.assertEqual(self.request("PATCH", path)[0], 401)
        self.assertEqual(self.request("PATCH", path, self.read_token, {"text": "Nope"})[0], 401)
        self.assertEqual(self.request("PATCH", prefixed_path, self.device_token, {"text": "Wrong route"})[0], 404)
        self.assertEqual(self.request("GET", path, self.device_token)[1]["text"], "HTTP edit")
        self.assertEqual(self.request("PATCH", path, self.device_token, {"text": "", "duration_days": 7})[0], 400)
        self.assertEqual(self.request("PATCH", path, self.device_token, {"text": "   "})[0], 400)
        self.assertEqual(self.request("PATCH", path, self.device_token, {"text": "x" * 31})[0], 400)
        status, updated = self.request("PATCH", path, self.device_token, {"text": "  Final text  "})
        self.assertEqual(status, 200)
        self.assertEqual(updated["text"], "Final text")
        self.assertEqual(self.request("DELETE", path, self.read_token)[0], 401)
        self.assertEqual(self.request("DELETE", prefixed_path, self.device_token)[0], 404)
        self.assertEqual(self.request("GET", path, self.device_token)[0], 200)
        status, response_body = self.request("DELETE", path, self.device_token, raw_response=True)
        self.assertEqual(status, 204)
        self.assertEqual(response_body, b"")
        status, response_body = self.request("DELETE", path, self.device_token, raw_response=True)
        self.assertEqual(status, 204)
        self.assertEqual(response_body, b"")
        self.assertEqual(self.request("GET", path, self.device_token)[0], 404)
        self.assertEqual(self.request("POST", "/v1/wishes", self.device_token, body)[0], 410)
        self.assertEqual(self.request("DELETE", f"/v1/wishes/{uuid.uuid4()}", self.device_token)[0], 404)

    def test_post_compatibility_aliases_update_and_delete_wish(self):
        status, wish = self.request(
            "POST", "/v1/wishes", self.device_token,
            {"request_id": str(uuid.uuid4()), "text": "POST compatible"},
        )
        self.assertEqual(status, 201)
        path = f"/v1/wishes/{wish['wish_id']}"
        status, updated = self.request("POST", path, self.device_token, {"text": "Updated over POST"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["text"], "Updated over POST")
        status, response_body = self.request(
            "POST", f"{path}/delete", self.device_token, raw_response=True,
        )
        self.assertEqual(status, 204)
        self.assertEqual(response_body, b"")
        self.assertEqual(self.request("GET", path, self.device_token)[0], 404)

    def test_post_compatibility_aliases_update_and_delete_trigger(self):
        status, trigger = self.request(
            "POST", "/v1/event-triggers", self.device_token,
            {
                "request_id": str(uuid.uuid4()),
                "wish_id": None,
                "trigger_type": "blacklist_usage_milestone",
                "config_version": 1,
                "parameters": {"platform_scope": "all"},
                "interval_minutes": 60,
            },
        )
        self.assertEqual(status, 201)
        path = f"/v1/event-triggers/{trigger['trigger_id']}"
        status, updated = self.request("POST", path, self.device_token, {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(updated["enabled"])
        status, response_body = self.request(
            "POST", f"{path}/delete", self.device_token, raw_response=True,
        )
        self.assertEqual(status, 204)
        self.assertEqual(response_body, b"")


if __name__ == "__main__":
    unittest.main()
