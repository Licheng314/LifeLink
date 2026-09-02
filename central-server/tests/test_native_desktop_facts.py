import json
import sqlite3
import unittest
import uuid
from datetime import date, datetime, timezone

from central.domain import normalize_event
from central.health_sleep import derive_sleep_reference


def event(event_type, payload, *, collector="windows_native", duration=60):
    return {
        "event_id": str(uuid.uuid4()),
        "occurred_at": "2026-08-11T14:50:00Z",
        "event_type": event_type,
        "source": {"kind": "desktop", "collector": collector, "reliability": "observed"},
        "duration_seconds": duration,
        "revision": 1,
        "payload": payload,
    }


class NativeDesktopFactsTest(unittest.TestCase):
    def test_native_app_input_and_domain_are_mutable_contract_facts(self):
        cases = [
            event("app.foreground", {"app": {"display_name": "Code.exe", "process_name": "Code.exe"}}),
            event("device.input_state", {"status": "active", "idle_threshold_seconds": 180}),
            event("web.foreground", {"domain": "example.com"}, collector="browser_extension"),
        ]
        for item in cases:
            normalized, rejection = normalize_event(item)
            self.assertIsNone(rejection)
            self.assertTrue(normalized.mutable)

    def test_domain_fact_rejects_url_or_extra_private_fields(self):
        for payload in (
            {"domain": "https://example.com/path"},
            {"domain": "example.com", "title": "private"},
        ):
            _, rejection = normalize_event(event("web.foreground", payload, collector="browser_extension"))
            self.assertEqual(rejection.code, "invalid_web_payload")

    def test_native_active_state_allows_pc_window_to_bound_sleep(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE devices(device_id TEXT PRIMARY KEY, platform TEXT NOT NULL);
            CREATE TABLE events(event_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
              occurred_at TEXT NOT NULL, event_type TEXT NOT NULL,
              duration_seconds INTEGER, payload_json TEXT NOT NULL);
            INSERT INTO devices VALUES('phone','android'),('pc','desktop');
        """)
        rows = [
            ("before", "phone", "2026-08-11T14:50:00Z", "app.foreground", 600, {}),
            ("after-app", "pc", "2026-08-11T22:00:00Z", "app.foreground", 600,
             {"app": {"display_name": "Code.exe", "process_name": "Code.exe"}}),
            ("after-input", "pc", "2026-08-11T22:00:00Z", "device.input_state", 600,
             {"status": "active", "idle_threshold_seconds": 180}),
        ]
        connection.executemany(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            [(a, b, c, d, e, json.dumps(f)) for a, b, c, d, e, f in rows],
        )
        result = derive_sleep_reference(
            connection, date(2026, 8, 12),
            now=datetime(2026, 8, 12, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(result["estimated_start"], "2026-08-11T15:00:00Z")
        self.assertEqual(result["estimated_end"], "2026-08-11T22:00:00Z")
        self.assertEqual(result["first_activity_apps"][0]["app_name"], "Code.exe")
        connection.close()


if __name__ == "__main__":
    unittest.main()
