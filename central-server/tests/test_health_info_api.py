import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from central.config import CentralConfig
from central.domain import normalize_event
from central.http import create_server
from central.health_info import _step_events


DEVICE_TOKEN = "health-device-token-0123456789-ABCDEFGH"
READ_TOKEN = "health-read-token-0123456789-ABCDEFGHIJK"


class HealthInfoApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        config = CentralConfig(
            database_path=Path(self.temp_dir.name) / "central.sqlite3",
            host="127.0.0.1",
            port=0,
            token_bindings={DEVICE_TOKEN: "android-health"},
            read_token=READ_TOKEN,
        )
        self.server = create_server(config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, body

    def upload(self, events):
        batch_id = str(uuid.uuid4())
        payload = {
            "schema_version": "v1",
            "batch_id": batch_id,
            "device": {"device_id": "android-health", "platform": "android", "display_name": "My Phone"},
            "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "events": events,
        }
        headers = {
            "Authorization": f"Bearer {DEVICE_TOKEN}",
            "Idempotency-Key": batch_id,
            "Content-Type": "application/json",
        }
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("POST", "/v1/events/batches", body=json.dumps(payload).encode("utf-8"), headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, body

    def test_operational_health_path_remains_public(self):
        status, payload = self.request("/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_health_info_requires_authentication_and_strict_date(self):
        status, _ = self.request("/v1/health-info?date=2026-08-12")
        self.assertEqual(status, 401)
        status, payload = self.request("/v1/health-info?date=2026-8-2", DEVICE_TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_date")

    def test_read_and_device_tokens_can_read_current_health_info(self):
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        for token in (READ_TOKEN, DEVICE_TOKEN):
            status, payload = self.request(f"/v1/health-info?date={today}", token)
            self.assertEqual(status, 200)
            self.assertEqual(payload["date"], today)
            self.assertEqual(payload["timezone"], "Asia/Shanghai")
            self.assertEqual(payload["steps"], {"devices": []})
            self.assertIn("last_activity_devices", payload["sleep"])
            self.assertIn("first_activity_devices", payload["sleep"])

    def test_uploaded_step_observations_are_differenced_in_health_info(self):
        shanghai = timezone(timedelta(hours=8))
        local_date = datetime.now(timezone.utc).astimezone(shanghai).date() - timedelta(days=1)
        session_id = str(uuid.uuid4())

        def event(at_minute, value):
            occurred = datetime.combine(local_date, time(0, at_minute), shanghai).astimezone(timezone.utc)
            return {
                "event_id": str(uuid.uuid4()),
                "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
                "event_type": "health.steps_observation",
                "source": {"kind": "android", "collector": "step_counter", "reliability": "observed"},
                "payload": {
                    "counter_value": value,
                    "counter_session_id": session_id,
                    "sensor_type": "android.step_counter",
                },
            }

        status, acknowledgement = self.upload([event(5, 100), event(10, 125)])
        self.assertEqual(status, 200)
        self.assertEqual(len(acknowledgement["confirmed_event_ids"]), 2)
        status, payload = self.request(f"/v1/health-info?date={local_date.isoformat()}", DEVICE_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["steps"]["devices"][0]["steps"], 25)
        self.assertEqual(len(payload["steps"]["devices"][0]["hourly_steps"]), 24)
        self.assertEqual(payload["steps"]["devices"][0]["hourly_steps"][0], 25)
        self.assertEqual(payload["steps"]["devices"][0]["steps"], sum(payload["steps"]["devices"][0]["hourly_steps"]))
        self.assertEqual(payload["steps"]["devices"][0]["display_name"], "My Phone")

    def test_old_nearest_baseline_is_used_without_loading_unrelated_history(self):
        shanghai = timezone(timedelta(hours=8))
        local_date = datetime.now(timezone.utc).astimezone(shanghai).date() - timedelta(days=1)
        session_id = str(uuid.uuid4())

        def event(local_at, value):
            return {
                "event_id": str(uuid.uuid4()),
                "occurred_at": local_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "event_type": "health.steps_observation",
                "source": {"kind": "android", "collector": "step_counter", "reliability": "observed"},
                "payload": {"counter_value": value, "counter_session_id": session_id, "sensor_type": "android.step_counter"},
            }

        status, _ = self.upload([
            event(datetime.combine(local_date - timedelta(days=60), time(12), shanghai), 1),
            event(datetime.combine(local_date - timedelta(days=9), time(12), shanghai), 100),
            event(datetime.combine(local_date, time(2), shanghai), 125),
        ])
        self.assertEqual(status, 200)
        status, payload = self.request(f"/v1/health-info?date={local_date.isoformat()}", DEVICE_TOKEN)
        self.assertEqual(status, 200)
        device = payload["steps"]["devices"][0]
        self.assertEqual(25, device["steps"])
        self.assertEqual(25, device["hourly_steps"][2])

    def test_sleep_boundary_returns_current_device_name_and_real_completion_time(self):
        shanghai = timezone(timedelta(hours=8))
        local_date = datetime.now(timezone.utc).astimezone(shanghai).date() - timedelta(days=1)

        def usage(local_time, app_name):
            occurred = datetime.combine(local_date + local_time[0], local_time[1], shanghai).astimezone(timezone.utc)
            return {
                "event_id": str(uuid.uuid4()),
                "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
                "event_type": "app.foreground",
                "source": {"kind": "android", "collector": "usage_stats", "reliability": "observed"},
                "duration_seconds": 600,
                "payload": {"app": {"display_name": app_name}},
            }

        before = usage((-timedelta(days=1), time(22, 50)), "睡前阅读")
        after = usage((timedelta(), time(7, 0)), "时钟")
        status, _ = self.upload([before, after])
        self.assertEqual(status, 200)
        status, payload = self.request(f"/v1/health-info?date={local_date.isoformat()}", DEVICE_TOKEN)
        self.assertEqual(status, 200)
        sleep = payload["sleep"]
        self.assertEqual(sleep["status"], "final")
        self.assertEqual(sleep["finalized_at"], after["occurred_at"])
        self.assertEqual(sleep["first_activity_devices"][0]["display_name"], "My Phone")
        self.assertEqual(sleep["first_activity_devices"][0]["platform"], "android")
        self.assertEqual(sleep["last_activity_apps"][0]["app_name"], "睡前阅读")
        self.assertEqual(sleep["first_activity_apps"][0], {
            "device_id": "android-health",
            "device_display_name": "My Phone",
            "platform": "android",
            "app_name": "时钟",
        })


class StepsObservationValidationTest(unittest.TestCase):
    def event(self, **changes):
        event = {
            "event_id": str(uuid.uuid4()),
            "occurred_at": "2026-08-12T08:00:00Z",
            "event_type": "health.steps_observation",
            "source": {"kind": "android", "collector": "step_counter", "reliability": "observed"},
            "payload": {
                "counter_value": 123,
                "counter_session_id": str(uuid.uuid4()),
                "sensor_type": "android.step_counter",
            },
        }
        event.update(changes)
        return event

    def test_valid_observation_is_immutable(self):
        normalized, rejection = normalize_event(self.event())
        self.assertIsNone(rejection)
        self.assertIsNotNone(normalized)
        self.assertFalse(normalized.mutable)

    def test_invalid_source_and_boolean_counter_are_rejected(self):
        event = self.event()
        event["source"]["kind"] = "desktop"
        _, rejection = normalize_event(event)
        self.assertEqual(rejection.code, "invalid_steps_source")

        event = self.event()
        event["payload"]["counter_value"] = True
        _, rejection = normalize_event(event)
        self.assertEqual(rejection.code, "invalid_steps_payload")


class StepEventQueryTest(unittest.TestCase):
    def test_query_uses_only_day_rows_and_one_old_baseline_per_device(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE events (
                device_id TEXT, occurred_at TEXT, event_id TEXT,
                event_type TEXT, payload_json TEXT
            )
        """)
        payload = json.dumps({"counter_value": 0, "counter_session_id": str(uuid.uuid4())})
        rows = [
            ("phone", "2026-06-01T00:00:00Z", "old-unrelated", "health.steps_observation", payload),
            ("phone", "2026-08-01T00:00:00Z", "nearest-baseline", "health.steps_observation", payload),
            ("phone", "2026-08-11T16:05:00Z", "day-observation", "health.steps_observation", payload),
            ("other", "2026-06-01T00:00:00Z", "other-unrelated", "health.steps_observation", payload),
        ]
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)

        events = _step_events(connection, datetime(2026, 8, 12).date())

        self.assertEqual(["2026-08-01T00:00:00Z", "2026-08-11T16:05:00Z"], [event["occurred_at"] for event in events])
        selects = [statement for statement in statements if "FROM events" in statement]
        self.assertEqual(2, len(selects))  # One day scan and one parameterized baseline lookup.
        self.assertNotIn("old-unrelated", str(events))
        self.assertNotIn("other-unrelated", str(events))


if __name__ == "__main__":
    unittest.main()
