import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from central.domain import canonical_json
from central.storage import CentralStore


class CalendarDaysStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.temp.name) / "central.sqlite3", {})
        self.connection = self.store._connect()
        self.connection.execute(
            """INSERT INTO devices(device_id, platform, display_name, first_seen_at, last_seen_at)
               VALUES ('desktop-a', 'desktop', 'Desktop', '2026-08-16T00:00:00Z', '2026-08-17T00:00:00Z')"""
        )
        self.store.update_shared_day_start_hour(4)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def raw_event(self, event_type, occurred_at, payload):
        event = {
            "event_id": str(uuid.uuid4()), "occurred_at": occurred_at,
            "event_type": event_type, "source": {"kind": "desktop"},
            "duration_seconds": 0, "payload": payload,
        }
        event_json = canonical_json(event)
        self.connection.execute(
            """INSERT INTO events(event_id, device_id, occurred_at, event_type, source_json,
               duration_seconds, revision, payload_json, event_json, content_hash, is_mutable, received_at, updated_at)
               VALUES (?, 'desktop-a', ?, ?, ?, 0, 0, ?, ?, 'hash', 0, ?, ?)""",
            (event["event_id"], occurred_at, event_type, canonical_json(event["source"]),
             canonical_json(payload), event_json, occurred_at, occurred_at),
        )
        return len(event_json.encode("utf-8"))

    def test_groups_exclusive_modules_at_shared_business_day_boundary(self):
        expected_usage = self.raw_event("app.foreground", "2026-08-16T19:59:00Z", {"name": "编辑器"})
        expected_location = self.raw_event("location.observation", "2026-08-16T19:59:01Z", {"place": "家"})
        expected_health = self.raw_event("health.steps_observation", "2026-08-16T19:59:02Z", {"counter_value": 10})
        expected_other = self.raw_event("custom.event", "2026-08-16T19:59:03Z", {"key": "value"})
        self.connection.execute(
            """INSERT INTO timeline_events(
                timeline_event_id, occurred_at, created_at, event_key, category, importance, title, detail,
                source_kind, source_device_id, wish_id, trigger_id, subject_json, evidence_json,
                statistics_window_json, delivery_json, dedupe_key
            ) VALUES (?, '2026-08-16T20:00:00Z', '2026-08-16T20:00:01Z', 'system.test', 'system', 'normal',
                '可读时间线', NULL, 'central', NULL, NULL, NULL, '{"kind":"test"}', '{"message":"你好"}', NULL, NULL, 'calendar-test')""",
            (str(uuid.uuid4()),),
        )
        self.connection.commit()

        result = self.store.calendar_days("2026-08-16", "2026-08-17", now=datetime(2026, 8, 16, 20, tzinfo=timezone.utc))
        previous, current = result["days"]

        self.assertEqual(result["today_business_date"], "2026-08-17")
        self.assertEqual(result["earliest_available_date"], "2026-08-16")
        self.assertEqual(result["latest_available_date"], "2026-08-17")
        self.assertTrue(previous["available"])
        self.assertEqual(previous["modules"]["usage"], {"bytes": expected_usage, "records": 1})
        self.assertEqual(previous["modules"]["location"], {"bytes": expected_location, "records": 1})
        self.assertEqual(previous["modules"]["health"], {"bytes": expected_health, "records": 1})
        self.assertEqual(previous["modules"]["other"], {"bytes": expected_other, "records": 1})
        self.assertEqual(previous["modules"]["timeline"], {"bytes": 0, "records": 0})
        self.assertEqual(previous["total_bytes"], expected_usage + expected_location + expected_health + expected_other)
        self.assertTrue(current["available"])
        self.assertEqual(current["modules"]["timeline"]["records"], 1)
        self.assertEqual(current["total_bytes"], current["modules"]["timeline"]["bytes"])
        self.assertGreater(current["total_bytes"], 0)

    def test_rejects_reversed_and_too_large_ranges(self):
        with self.assertRaises(ValueError):
            self.store.calendar_days("2026-08-18", "2026-08-17")
        with self.assertRaises(ValueError):
            self.store.calendar_days("2026-08-01", "2026-09-12")


if __name__ == "__main__":
    unittest.main()
