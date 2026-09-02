import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from central.storage import CentralStore


class EventBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.temp.name) / "central.sqlite3", {})
        self.connection = self.store._connect()
        self.now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def device(self, device_id, platform="desktop"):
        self.connection.execute(
            """INSERT INTO devices(device_id, platform, display_name, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, '2026-08-16T00:00:00Z', '2026-08-16T11:59:00Z')""",
            (device_id, platform, device_id),
        )

    def event(self, device_id, occurred_at, duration, app, kind="window"):
        event_id = str(uuid.uuid4())
        payload = json.dumps({
            "app": {"display_name": app},
            "activitywatch": {"kind": kind, "data": {"app": app}},
        })
        self.connection.execute(
            """INSERT INTO events(
                   event_id, device_id, occurred_at, event_type, source_json,
                   duration_seconds, revision, payload_json, event_json,
                   content_hash, is_mutable, received_at, updated_at
               ) VALUES (?, ?, ?, 'app.foreground', '{}', ?, 0, ?, '{}', ?, 0, ?, ?)""",
            (event_id, device_id, occurred_at, duration, payload, event_id, occurred_at, occurred_at),
        )

    def raw_event(self, device_id, occurred_at, event_type, payload):
        event_id = str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO events(
                   event_id, device_id, occurred_at, event_type, source_json,
                   duration_seconds, revision, payload_json, event_json,
                   content_hash, is_mutable, received_at, updated_at
               ) VALUES (?, ?, ?, ?, '{}', 0, 0, ?, '{}', ?, 0, ?, ?)""",
            (event_id, device_id, occurred_at, event_type, json.dumps(payload), event_id, occurred_at, occurred_at),
        )

    def test_current_background_groups_only_online_devices_with_fresh_apps(self):
        self.device("fresh")
        self.device("stale")
        self.event("fresh", "2026-08-16T11:40:00Z", 300, "Editor")
        self.event("fresh", "2026-08-16T11:59:00Z", 60, "AFK", kind="afk")
        self.event("stale", "2026-08-16T11:39:00Z", 300, "Reader")
        self.connection.commit()

        result = self.store.event_background("2026-08-16", now=self.now)
        apps = {
            item["device_id"]: item
            for item in result["real_time_items"]
            if item["kind"] == "current_app"
        }

        self.assertIn("Editor", apps["fresh"]["display_text"])
        self.assertNotIn("AFK", apps["fresh"]["display_text"])
        self.assertFalse(apps["fresh"]["is_stale"])
        self.assertTrue(apps["fresh"]["include_in_ai"])
        self.assertNotIn("stale", apps)
        kinds_and_devices = [
            (item["kind"], item.get("device_id")) for item in result["real_time_items"]
            if item["kind"] in {"device_online", "current_app"}
        ]
        self.assertEqual(kinds_and_devices[:2], [
            ("device_online", "fresh"), ("current_app", "fresh"),
        ])

    def test_wish_background_does_not_remind_for_unfilled_current_day(self):
        created_at = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        self.store.create_wish(
            request_id=str(uuid.uuid4()),
            request_hash=str(uuid.uuid4()),
            text="期望发布 Life Link",
            duration_days=3,
            ai_tracking_enabled=False,
            source_device_id="desktop-a",
            now=created_at,
        )

        result = self.store.event_background("2026-08-16", now=self.now)
        wish_text = result["background_summary"]["wish"]["items"][0]["text"]
        guides = [item["text"] for item in result["ai_understanding"]["items"]]

        self.assertIn("待填写：08-15（需要提醒用户填写结果）", wish_text)
        self.assertIn("待填写：08-16（今天的进度。不需要提醒）", wish_text)
        self.assertIn("尚未到达：08-17", wish_text)
        self.assertTrue(any("当前业务日的待填写只是今天的进度、不需要提醒" in text for text in guides))

    def test_location_without_steps_is_not_projected_as_activity(self):
        self.device("phone", "android")
        self.connection.execute(
            "UPDATE shared_settings SET primary_health_device_id='phone' WHERE singleton_id=1"
        )
        self.raw_event("phone", "2026-08-16T11:50:00Z", "location.observation", {
            "latitude": 29.5, "longitude": 106.6, "accuracy_m": 20,
            "place": {"display_label": "重庆市 · 南岸区"},
        })
        self.raw_event("phone", "2026-08-16T11:59:00Z", "location.observation", {
            "latitude": 29.5, "longitude": 106.6, "accuracy_m": 20,
            "place": {"display_label": "重庆市 · 南岸区"},
        })
        self.connection.commit()

        result = self.store.event_background("2026-08-16", now=self.now)

        self.assertTrue(any(item["kind"] == "current_location" for item in result["real_time_items"]))
        self.assertFalse(any(item["kind"] == "current_activity" for item in result["real_time_items"]))
        location_items = result["background_summary"]["location_and_activity"]["items"]
        self.assertFalse(any(str(item["item_key"]).startswith("activity.") for item in location_items))

    def test_steps_without_location_are_not_projected_as_activity(self):
        self.device("phone", "android")
        self.connection.execute(
            "UPDATE shared_settings SET primary_health_device_id='phone' WHERE singleton_id=1"
        )
        session = "00000000-0000-4000-8000-000000000001"
        self.raw_event("phone", "2026-08-16T11:50:00Z", "health.steps_observation", {
            "counter_value": 100, "counter_session_id": session,
        })
        self.raw_event("phone", "2026-08-16T11:59:00Z", "health.steps_observation", {
            "counter_value": 200, "counter_session_id": session,
        })
        self.connection.commit()

        result = self.store.event_background("2026-08-16", now=self.now)

        self.assertFalse(any(item["kind"] == "current_activity" for item in result["real_time_items"]))
        location_items = result["background_summary"]["location_and_activity"]["items"]
        self.assertFalse(any(str(item["item_key"]).startswith("activity.") for item in location_items))

    def test_background_includes_full_activity_intervals_overlapping_past_hour(self):
        location = {
            "current_stays": [],
            "latest": None,
            "activity_state": {
                "primary_device_id": "phone",
                "current": None,
                "intervals": [
                    {
                        "start_at": "2026-08-16T10:30:00Z",
                        "end_at": "2026-08-16T11:00:00Z",
                        "state": "stationary",
                        "duration_seconds": 1800,
                    },
                    {
                        "start_at": "2026-08-16T10:45:00Z",
                        "end_at": "2026-08-16T11:30:00Z",
                        "state": "walking",
                        "duration_seconds": 2700,
                    },
                    {
                        "start_at": "2026-08-16T11:40:00Z",
                        "end_at": "2026-08-16T11:55:00Z",
                        "state": "transport",
                        "duration_seconds": 900,
                    },
                ],
            },
        }

        with patch("central.storage.locations_view", return_value=location):
            result = self.store.event_background("2026-08-16", now=self.now)

        activity_items = [
            item for item in result["background_summary"]["location_and_activity"]["items"]
            if str(item["item_key"]).startswith("activity.interval:")
        ]
        self.assertEqual(len(activity_items), 2)
        self.assertEqual(
            activity_items[0]["text"],
            "18:45–19:30 步行，持续 45 分钟。",
        )
        self.assertEqual(
            activity_items[1]["text"],
            "19:40–19:55 乘坐交通工具，持续 15 分钟。",
        )

    def test_background_keeps_entire_long_interval_crossing_one_hour_cutoff(self):
        location = {
            "current_stays": [],
            "latest": None,
            "activity_state": {
                "primary_device_id": "phone",
                "current": None,
                "intervals": [{
                    "start_at": "2026-08-16T09:30:00Z",
                    "end_at": "2026-08-16T11:30:00Z",
                    "state": "running",
                    "duration_seconds": 7200,
                }],
            },
        }

        with patch("central.storage.locations_view", return_value=location):
            result = self.store.event_background("2026-08-16", now=self.now)

        activity_items = result["background_summary"]["location_and_activity"]["items"]
        self.assertEqual(len(activity_items), 1)
        self.assertEqual(
            activity_items[0]["text"],
            "17:30–19:30 跑步，持续 2 小时。",
        )


if __name__ == "__main__":
    unittest.main()
