import json
import sqlite3
import unittest
from datetime import date, datetime, timezone

from central.health_sleep import derive_sleep_reference


class HealthSleepTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE devices (
                device_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL
            );
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                duration_seconds INTEGER,
                payload_json TEXT NOT NULL
            );
            """
        )

    def tearDown(self):
        self.connection.close()

    def add_device(self, device_id, platform):
        self.connection.execute("INSERT INTO devices VALUES (?, ?)", (device_id, platform))

    def add_event(self, event_id, device_id, occurred_at, duration, payload):
        self.connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, 'app.foreground', ?, ?)",
            (event_id, device_id, occurred_at, duration, json.dumps(payload)),
        )

    def test_android_and_pc_jointly_close_the_same_rest_interval(self):
        self.add_device("phone", "android")
        self.add_device("pc", "desktop")
        self.add_event("a1", "phone", "2026-08-11T14:50:00Z", 600, {})
        window = {"activitywatch": {"kind": "window", "data": {"app": "Code"}}}
        not_afk = {"activitywatch": {"kind": "afk", "data": {"status": "not-afk"}}}
        self.add_event("w1", "pc", "2026-08-11T22:00:00Z", 600, window)
        self.add_event("n1", "pc", "2026-08-11T22:00:00Z", 600, not_afk)

        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "final")
        self.assertEqual(result["estimated_start"], "2026-08-11T15:00:00Z")
        self.assertEqual(result["estimated_end"], "2026-08-11T22:00:00Z")
        self.assertEqual(result["rest_seconds"], 7 * 3600)
        self.assertEqual(result["contributing_device_ids"], ["pc", "phone"])

    def test_pc_window_without_not_afk_does_not_count_as_interaction(self):
        self.add_device("pc", "desktop")
        self.add_event(
            "w1",
            "pc",
            "2026-08-11T14:00:00Z",
            600,
            {"activitywatch": {"kind": "window", "data": {"app": "Code"}}},
        )
        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("pc_not_afk_missing:pc", result["warnings"])

    def test_single_side_evidence_never_becomes_a_whole_night(self):
        self.add_device("phone", "android")
        self.add_event("a1", "phone", "2026-08-11T15:00:00Z", 300, {})
        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["rest_seconds"])

    def test_short_interaction_can_stitch_two_long_gaps(self):
        self.add_device("phone", "android")
        self.add_event("a1", "phone", "2026-08-11T14:50:00Z", 600, {})
        self.add_event("a2", "phone", "2026-08-11T18:00:00Z", 900, {})
        self.add_event("a3", "phone", "2026-08-11T22:00:00Z", 600, {})
        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(result["estimated_start"], "2026-08-11T15:00:00Z")
        self.assertEqual(result["estimated_end"], "2026-08-11T22:00:00Z")
        self.assertEqual(result["interruption_seconds"], 900)
        self.assertEqual(result["rest_seconds"], 7 * 3600 - 900)
        self.assertEqual(result["interval_seconds"], 7 * 3600)

    def test_current_night_is_final_as_soon_as_activity_closes_the_gap(self):
        self.add_device("phone", "android")
        self.add_event("a1", "phone", "2026-08-11T14:50:00Z", 600, {})
        self.add_event("a2", "phone", "2026-08-11T23:00:00Z", 600, {})
        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 11, 23, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "final")
        self.assertEqual(result["finalized_at"], "2026-08-11T23:00:00Z")
        self.assertEqual(result["first_activity_device_ids"], ["phone"])
        self.assertEqual(result["window_end"], "2026-08-11T23:30:00Z")

    def test_current_night_without_wake_boundary_remains_estimating(self):
        self.add_device("phone", "android")
        self.add_event("a1", "phone", "2026-08-11T14:50:00Z", 600, {})
        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 11, 23, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "estimating")
        self.assertIsNone(result["finalized_at"])

    def test_boundary_devices_are_the_devices_active_at_each_exact_edge(self):
        self.add_device("early-phone", "android")
        self.add_device("late-pc", "android")
        self.add_device("wake-phone", "android")
        self.add_event("a1", "early-phone", "2026-08-11T14:40:00Z", 600, {})
        self.add_event("a2", "late-pc", "2026-08-11T14:45:00Z", 900, {})
        self.add_event("a3", "wake-phone", "2026-08-11T22:00:00Z", 600, {})
        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(result["last_activity_device_ids"], ["late-pc"])
        self.assertEqual(result["first_activity_device_ids"], ["wake-phone"])

    def test_boundary_apps_keep_application_and_platform_evidence(self):
        self.add_device("phone", "android")
        self.add_device("pc", "desktop")
        self.add_event(
            "before", "pc", "2026-08-11T14:50:00Z", 600,
            {"activitywatch": {"kind": "window", "data": {"app": "Obsidian.exe"}}},
        )
        self.add_event(
            "before-afk", "pc", "2026-08-11T14:50:00Z", 600,
            {"activitywatch": {"kind": "afk", "data": {"status": "not-afk"}}},
        )
        self.add_event(
            "after", "phone", "2026-08-11T22:00:00Z", 600,
            {"app": {"display_name": "时钟", "package_name": "com.android.deskclock"}},
        )

        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(result["last_activity_apps"], [
            {"device_id": "pc", "platform": "desktop", "app_name": "Obsidian.exe"}
        ])
        self.assertEqual(result["first_activity_apps"], [
            {"device_id": "phone", "platform": "android", "app_name": "时钟"}
        ])

    def test_multiple_short_night_interruptions_chain_like_the_original_algorithm(self):
        self.add_device("phone", "android")
        self.add_event("a1", "phone", "2026-08-11T14:50:00Z", 600, {})
        self.add_event("a2", "phone", "2026-08-11T17:00:00Z", 1200, {})
        self.add_event("a3", "phone", "2026-08-11T19:00:00Z", 1200, {})
        self.add_event("a4", "phone", "2026-08-11T22:00:00Z", 600, {})
        result = derive_sleep_reference(
            self.connection,
            date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(result["estimated_start"], "2026-08-11T15:00:00Z")
        self.assertEqual(result["estimated_end"], "2026-08-11T22:00:00Z")
        self.assertEqual(result["interruption_seconds"], 2400)
        self.assertEqual(result["interval_seconds"], 7 * 3600)


if __name__ == "__main__":
    unittest.main()
