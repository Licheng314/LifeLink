import json
import sqlite3
import unittest
from datetime import datetime, timezone

from central.activity_state import derive_activity_state
from central.locations import _ai_summary


class ActivityStateTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE devices(device_id TEXT PRIMARY KEY, platform TEXT, display_name TEXT, custom_name TEXT, retired_at TEXT);
            CREATE TABLE shared_settings(singleton_id INTEGER PRIMARY KEY, primary_health_device_id TEXT);
            CREATE TABLE events(event_id TEXT PRIMARY KEY, device_id TEXT, occurred_at TEXT, event_type TEXT, duration_seconds INTEGER, payload_json TEXT);
            INSERT INTO devices VALUES('phone-a','android','手机A',NULL,NULL);
            INSERT INTO devices VALUES('pc-a','desktop','电脑A',NULL,NULL);
            INSERT INTO shared_settings VALUES(1,'phone-a');
        """)
        self.start = datetime(2026, 8, 13, 20, 0, tzinfo=timezone.utc)  # Shanghai 04:00
        self.end = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)

    def add(self, event_id, device_id, occurred_at, event_type, payload, duration=0):
        self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (event_id, device_id, occurred_at, event_type, duration, json.dumps(payload)))

    def test_step_distance_and_business_window_are_preserved(self):
        session = "00000000-0000-4000-8000-000000000001"
        self.add("s1", "phone-a", "2026-08-13T19:59:00Z", "health.steps_observation", {"counter_value": 100, "counter_session_id": session})
        self.add("s2", "phone-a", "2026-08-13T20:04:00Z", "health.steps_observation", {"counter_value": 150, "counter_session_id": session})
        self.add("l1", "phone-a", "2026-08-13T20:00:00Z", "location.observation", {
            "latitude": 29.495321, "longitude": 106.636812, "accuracy_m": 20,
            "place": {"display_label": "重庆市 · 南岸区 · 广福大道"},
        })
        self.add("l2", "phone-a", "2026-08-13T20:04:00Z", "location.observation", {
            "latitude": 29.495321, "longitude": 106.636812, "accuracy_m": 20,
            "place": {"display_label": "重庆市 · 南岸区 · 广福大道"},
        })
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        walking = next(item for item in result["intervals"] if item["state"] == "walking")
        self.assertGreaterEqual(walking["distance_m"], walking["steps"] * 0.7)
        self.assertGreaterEqual(datetime.fromisoformat(walking["start_at"].replace("Z", "+00:00")), self.start)
        self.assertLessEqual(datetime.fromisoformat(walking["end_at"].replace("Z", "+00:00")), self.end)
        self.assertEqual(walking["address"], "重庆市 · 南岸区 · 广福大道")
        self.assertEqual(walking["latitude"], 29.495321)
        self.assertEqual(walking["longitude"], 106.636812)

    def test_desktop_afk_is_not_green_stationary(self):
        aw = lambda kind, data: {"activitywatch": {"kind": kind, "data": data}}
        self.add("w", "pc-a", "2026-08-13T21:00:00Z", "app.foreground", aw("window", {"app": "Editor"}), 600)
        self.add("a", "pc-a", "2026-08-13T21:00:00Z", "app.foreground", aw("afk", {"status": "afk"}), 600)
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        self.assertFalse(any(item["state"] == "stationary" for item in result["intervals"]))

    def test_stationary_uses_zero_distance_and_fixed_visual_baseline(self):
        session = "00000000-0000-4000-8000-000000000002"
        self.add("s1", "phone-a", "2026-08-13T20:59:00Z", "health.steps_observation", {"counter_value": 100, "counter_session_id": session})
        self.add("s2", "phone-a", "2026-08-13T21:10:00Z", "health.steps_observation", {"counter_value": 100, "counter_session_id": session})
        self.add("l1", "phone-a", "2026-08-13T21:00:00Z", "location.observation", {"latitude": 29.5, "longitude": 106.6, "accuracy_m": 20})
        self.add("l2", "phone-a", "2026-08-13T21:10:00Z", "location.observation", {"latitude": 29.5, "longitude": 106.6, "accuracy_m": 20})
        self.add("w", "phone-a", "2026-08-13T21:00:00Z", "app.foreground", {"activitywatch": {"kind": "window", "data": {}}}, 600)
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        stationary = next(item for item in result["intervals"] if item["state"] == "stationary")
        self.assertEqual(stationary["distance_m"], 0.0)
        self.assertEqual(stationary["distance_source"], "none")
        self.assertEqual(stationary["address"], "未解析地址")
        self.assertEqual(stationary["latitude"], 29.5)
        self.assertEqual(stationary["longitude"], 106.6)

    def test_steps_without_location_do_not_create_activity(self):
        session = "00000000-0000-4000-8000-000000000003"
        self.add("s1", "phone-a", "2026-08-13T20:00:00Z", "health.steps_observation", {"counter_value": 0, "counter_session_id": session})
        self.add("s2", "phone-a", "2026-08-13T20:05:00Z", "health.steps_observation", {"counter_value": 50, "counter_session_id": session})
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        self.assertEqual(result["intervals"], [])

    def test_location_without_steps_does_not_create_activity(self):
        self.add("l1", "phone-a", "2026-08-13T20:00:00Z", "location.observation", {"latitude": 29.5, "longitude": 106.6, "accuracy_m": 10})
        self.add("l2", "phone-a", "2026-08-13T20:05:00Z", "location.observation", {"latitude": 29.55, "longitude": 106.65, "accuracy_m": 10})
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        self.assertEqual(result["intervals"], [])

    def test_device_use_without_health_sources_does_not_create_stationary(self):
        self.add("w", "phone-a", "2026-08-13T21:00:00Z", "app.foreground", {"activitywatch": {"kind": "window", "data": {}}}, 600)
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        self.assertEqual(result["intervals"], [])

    def test_missing_source_minute_is_not_bridged(self):
        first_session = "00000000-0000-4000-8000-000000000004"
        second_session = "00000000-0000-4000-8000-000000000005"
        self.add("s1", "phone-a", "2026-08-13T20:00:00Z", "health.steps_observation", {"counter_value": 0, "counter_session_id": first_session})
        self.add("s2", "phone-a", "2026-08-13T20:02:00Z", "health.steps_observation", {"counter_value": 20, "counter_session_id": first_session})
        self.add("s3", "phone-a", "2026-08-13T20:03:00Z", "health.steps_observation", {"counter_value": 0, "counter_session_id": second_session})
        self.add("s4", "phone-a", "2026-08-13T20:05:00Z", "health.steps_observation", {"counter_value": 20, "counter_session_id": second_session})
        self.add("l1", "phone-a", "2026-08-13T20:00:00Z", "location.observation", {"latitude": 29.5, "longitude": 106.6, "accuracy_m": 20})
        self.add("l2", "phone-a", "2026-08-13T20:05:00Z", "location.observation", {"latitude": 29.5, "longitude": 106.6, "accuracy_m": 20})
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        walking = [item for item in result["intervals"] if item["state"] == "walking"]
        self.assertEqual(len(walking), 2)
        self.assertEqual(walking[0]["end_at"], "2026-08-13T20:02:00Z")
        self.assertEqual(walking[1]["start_at"], "2026-08-13T20:03:00Z")

    def test_short_running_segment_is_presented_as_walking(self):
        session = "00000000-0000-4000-8000-000000000006"
        self.add("s1", "phone-a", "2026-08-13T20:00:00Z", "health.steps_observation", {"counter_value": 0, "counter_session_id": session})
        self.add("s2", "phone-a", "2026-08-13T20:10:00Z", "health.steps_observation", {"counter_value": 1200, "counter_session_id": session})
        self.add("l1", "phone-a", "2026-08-13T20:00:00Z", "location.observation", {"latitude": 29.5, "longitude": 106.6, "accuracy_m": 20})
        self.add("l2", "phone-a", "2026-08-13T20:10:00Z", "location.observation", {"latitude": 29.5, "longitude": 106.6, "accuracy_m": 20})

        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)

        self.assertFalse(any(item["state"] == "running" for item in result["intervals"]))
        walking = next(item for item in result["intervals"] if item["state"] == "walking")
        self.assertEqual(walking["duration_seconds"], 10 * 60)

    def test_primary_device_is_configured_and_candidates_expose_counts(self):
        result = derive_activity_state(self.connection, self.start, self.end, now=self.end)
        self.assertEqual(result["primary_device_id"], "phone-a")
        self.assertEqual(result["selection_source"], "configured")
        self.assertEqual(result["devices"][0]["location_observation_count"], 0)

    def test_location_ai_summary_omits_activity_when_no_valid_interval(self):
        summary = _ai_summary([], [], [], [], self.start, {"current": None, "intervals": []})
        self.assertNotIn("活动状态（中央派生）", summary)
        self.assertNotIn("暂无 15 分钟内的可靠证据", summary)


if __name__ == "__main__":
    unittest.main()
