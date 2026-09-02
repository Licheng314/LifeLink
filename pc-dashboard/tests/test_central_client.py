import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

from central_client import CentralClient, CentralReadClient, CentralReadError
from outbox import Outbox


NOW = datetime(2026, 7, 31, 1, 2, 3, tzinfo=timezone.utc)
DEVICE = {
    "device_id": "desktop-11111111-1111-4111-8111-111111111111",
    "platform": "desktop",
    "display_name": "Test PC",
}


def aw_event() -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "occurred_at": "2026-07-31T01:00:00Z",
        "event_type": "app.foreground",
        "source": {
            "kind": "desktop", "collector": "activitywatch",
            "reliability": "observed",
        },
        "duration_seconds": 10,
        "revision": 1,
        "payload": {
            "app": {"package_name": "chrome.exe"},
            "activitywatch": {"kind": "window", "event_id": 1},
        },
    }


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class CentralClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "outbox.sqlite3"

    def tearDown(self):
        self.directory.cleanup()

    def test_plain_http_is_allowed_only_for_local_loopback(self):
        for local_url in (
            "http://127.0.0.1:8091", "http://localhost:8091",
            "http://[::1]:8091",
        ):
            CentralClient(local_url, "token")
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            CentralClient("http://central.example.test", "token")

    def test_read_view_uses_server_bearer_and_exact_utc_window(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"devices": [], "online_window_seconds": 600})

        payload = CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        ).read_view(
            "devices",
            from_utc="2026-07-30T20:00:00Z",
            to_utc="2026-07-31T20:00:00Z",
            local_device_id=DEVICE["device_id"],
        )

        request = captured["request"]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer read-secret")
        self.assertIn("/v1/read/devices?", request.full_url)
        self.assertIn("from=2026-07-30T20%3A00%3A00Z", request.full_url)
        self.assertIn("to=2026-07-31T20%3A00%3A00Z", request.full_url)
        self.assertIn("local_device_id=desktop-11111111", request.full_url)
        self.assertEqual(payload["devices"], [])

    def test_read_failure_never_exposes_token(self):
        secret = "read-secret-must-not-leak"

        def fail(request, timeout):
            raise HTTPError(request.full_url, 403, secret, Message(), None)

        with self.assertRaises(CentralReadError) as caught:
            CentralReadClient(
                "https://central.example.test", secret, opener=fail,
            ).read_view(
                "usage",
                from_utc="2026-07-30T20:00:00Z",
                to_utc="2026-07-31T20:00:00Z",
            )
        self.assertEqual(caught.exception.category, "auth_error")
        self.assertNotIn(secret, str(caught.exception))

    def test_shared_settings_uses_registered_device_bearer_for_get_and_post(self):
        captured = []

        def open_request(request, timeout):
            captured.append(request)
            return FakeResponse({
                "timezone": "Asia/Shanghai", "day_start_hour": 4,
                "primary_health_device_id": None, "sleep_local_time": "23:00", "ai_display_name": "AI",
                "morning_report": {"enabled": False, "mode": "after_first_usage", "delay_minutes": 60, "local_time": None},
                "evening_report": {"enabled": False, "local_time": "23:00"},
                "periodic_summary": {"enabled": False, "start_local_time": "10:00", "end_local_time": "22:00", "interval_minutes": 120},
                "settings_version": 2, "updated_at": "2026-08-09T01:02:03Z",
            })

        client = CentralClient(
            "https://central.example.test", "device-secret", opener=open_request,
        )
        self.assertEqual(client.get_shared_settings()["day_start_hour"], 4)
        self.assertEqual(client.update_shared_settings(4)["settings_version"], 2)
        client.update_shared_settings({"primary_health_device_id": "android-install-example"})
        self.assertEqual([request.get_method() for request in captured], ["GET", "POST", "POST"])
        self.assertTrue(all(
            request.full_url.endswith("/v1/settings/shared")
            and request.get_header("Authorization") == "Bearer device-secret"
            for request in captured
        ))
        self.assertEqual(json.loads(captured[1].data.decode("utf-8")), {"day_start_hour": 4})
        self.assertEqual(json.loads(captured[2].data.decode("utf-8")), {"primary_health_device_id": "android-install-example"})
        self.assertEqual(client.update_shared_settings({"sleep_local_time": "22:30"})["sleep_local_time"], "23:00")
        self.assertEqual(json.loads(captured[3].data.decode("utf-8")), {"sleep_local_time": "22:30"})

    def test_shared_settings_rejects_invalid_central_response_and_error_category(self):
        client = CentralClient(
            "https://central.example.test", "device-secret",
            opener=lambda request, timeout: FakeResponse({"day_start_hour": 4}),
        )
        with self.assertRaisesRegex(CentralReadError, "invalid fields"):
            client.get_shared_settings()

        def reject(request, timeout):
            raise HTTPError(request.full_url, 403, "denied", Message(), None)

        with self.assertRaises(CentralReadError) as caught:
            CentralClient(
                "https://central.example.test", "device-secret", opener=reject,
            ).update_shared_settings(5)
        self.assertEqual(caught.exception.category, "auth_error")

    def test_upload_uses_bearer_and_idempotency_then_confirms(self):
        item = aw_event()
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            body = json.loads(request.data.decode("utf-8"))
            return FakeResponse({
                "batch_id": body["batch_id"],
                "confirmed_event_ids": [item["event_id"]],
                "accepted_event_ids": [item["event_id"]],
                "duplicate_event_ids": [],
                "event_results": [],
                "rejected_events": [],
                "received_at": "2026-07-31T01:02:03Z",
            })

        with Outbox(self.path) as outbox:
            outbox.upsert_event(item, now=NOW)
            client = CentralClient(
                "https://central.example.test", "secret-token",
                opener=open_request, clock=lambda: NOW,
            )
            result = client.sync_once(outbox, DEVICE)

            self.assertEqual(result["status"], "ok")
            request = captured["request"]
            self.assertEqual(request.full_url, "https://central.example.test/v1/events/batches")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
            self.assertEqual(request.get_header("Idempotency-key"), result["batch_id"])
            self.assertEqual(outbox.event_status(item["event_id"])["state"], "acked")

    def test_central_ack_requires_matching_batch_and_explicit_confirmed_list(self):
        for acknowledgement in (
            {"batch_id": "wrong", "confirmed_event_ids": []},
            {"accepted_event_ids": []},
            {"batch_id": None, "confirmed_event_ids": ["outside-batch"]},
        ):
            with self.subTest(acknowledgement=acknowledgement):
                database = self.path.with_name(f"{uuid.uuid4()}.sqlite3")
                item = aw_event()

                def open_request(request, timeout):
                    payload = dict(acknowledgement)
                    if payload.get("batch_id") is None:
                        payload["batch_id"] = json.loads(request.data)["batch_id"]
                    return FakeResponse(payload)

                with Outbox(database) as outbox:
                    outbox.upsert_event(item, now=NOW)
                    result = CentralClient(
                        "http://127.0.0.1:8091", "token",
                        opener=open_request, clock=lambda: NOW,
                    ).sync_once(outbox, DEVICE)

                    self.assertEqual(result["status"], "invalid_ack")
                    self.assertNotEqual(
                        outbox.event_status(item["event_id"])["state"], "acked",
                    )
                    active = outbox.status()["active_batch"]
                    self.assertIsNotNone(active)
                    self.assertEqual(active["state"], "retry")
                    self.assertEqual(active["attempt_count"], 1)

    def test_http_and_timeout_failures_are_classified(self):
        cases = [
            (401, None, "auth_error"),
            (403, None, "auth_error"),
            (429, "120", "rate_limited"),
            (503, None, "server_error"),
        ]
        for status_code, retry_after, expected in cases:
            with self.subTest(status_code=status_code):
                database = self.path.with_name(f"{uuid.uuid4()}.sqlite3")
                headers = Message()
                if retry_after:
                    headers["Retry-After"] = retry_after

                def fail(request, timeout):
                    raise HTTPError(
                        request.full_url, status_code, "failure", headers, None,
                    )

                with Outbox(database) as outbox:
                    outbox.upsert_event(aw_event(), now=NOW)
                    result = CentralClient(
                        "https://central.example.test", "token",
                        opener=fail, clock=lambda: NOW,
                    ).sync_once(outbox, DEVICE)
                    self.assertEqual(result["status"], expected)

        database = self.path.with_name("timeout.sqlite3")
        with Outbox(database) as outbox:
            outbox.upsert_event(aw_event(), now=NOW)
            result = CentralClient(
                "https://central.example.test", "token",
                opener=lambda request, timeout: (_ for _ in ()).throw(TimeoutError()),
                clock=lambda: NOW,
            ).sync_once(outbox, DEVICE)
            self.assertEqual(result["status"], "network_error")

    # --- Blacklist rules read ---

    def test_blacklist_read_uses_correct_path(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"rules": []})

        CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        ).read_blacklist_rules()

        request = captured["request"]
        self.assertEqual(request.get_method(), "GET")
        self.assertNotIn("/v1/read/", request.full_url)
        self.assertIn("/v1/settings/blacklist-rules", request.full_url)

    def test_calendar_days_uses_central_contract_path_and_range(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            return FakeResponse({"days": []})

        payload = CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        ).read_calendar_days(from_date="2026-08-24", to_date="2026-08-29")

        self.assertEqual(payload, {"days": []})
        request = captured["request"]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer read-secret")
        self.assertIn("/v1/calendar-days?from=2026-08-24&to=2026-08-29", request.full_url)

    def test_calendar_days_rejects_invalid_or_oversized_dates(self):
        client = CentralReadClient("https://central.example.test", "read-secret")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            client.read_calendar_days(from_date="2026-02-30", to_date="2026-03-01")
        with self.assertRaisesRegex(ValueError, "42 days"):
            client.read_calendar_days(from_date="2026-08-01", to_date="2026-09-12")

    def test_blacklist_read_uses_device_token_when_read_token_missing(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"rules": [{"rule_id": "r1", "rule_type": "app", "pattern": "test", "normalized_pattern": "test", "label": "T", "enabled": True}]})

        client = CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        )
        rules = client.read_blacklist_rules(token="device-token-override")

        self.assertGreater(len(rules), 0)
        request = captured["request"]
        self.assertEqual(request.get_header("Authorization"), "Bearer device-token-override")
        self.assertIn("/v1/settings/blacklist-rules", request.full_url)

    def test_blacklist_read_falls_back_to_instance_token(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"rules": []})

        CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        ).read_blacklist_rules()

        request = captured["request"]
        self.assertEqual(request.get_header("Authorization"), "Bearer read-secret")

    def test_blacklist_read_path_does_not_affect_usage_read_path(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"devices": []})

        CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        ).read_view("devices", from_utc="2026-07-30T20:00:00Z", to_utc="2026-07-31T20:00:00Z")

        request = captured["request"]
        self.assertIn("/v1/read/devices?", request.full_url)
        self.assertNotIn("/v1/settings/", request.full_url)

    def test_blacklist_read_preserves_platform_scope(self):
        """Verify that platform_scope is passed through from central response."""
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({
                "rules": [
                    {"rule_id": "r1", "rule_type": "app", "pattern": "wechat.exe",
                     "normalized_pattern": "wechat.exe", "label": "微信", "enabled": True,
                     "platform_scope": "pc"},
                    {"rule_id": "r2", "rule_type": "domain", "pattern": "bilibili.com",
                     "normalized_pattern": "bilibili.com", "label": "B站", "enabled": True,
                     "platform_scope": "web"},
                    {"rule_id": "r3", "rule_type": "app", "pattern": "抖音",
                     "normalized_pattern": "抖音", "label": "抖音", "enabled": True,
                     "platform_scope": "android"},
                ]
            })

        rules = CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        ).read_blacklist_rules()

        self.assertEqual(len(rules), 3)
        self.assertEqual(rules[0]["platform_scope"], "pc")
        self.assertEqual(rules[0]["rule_type"], "app")
        self.assertEqual(rules[1]["platform_scope"], "web")
        self.assertEqual(rules[1]["rule_type"], "domain")
        self.assertEqual(rules[2]["platform_scope"], "android")
        self.assertEqual(rules[2]["rule_type"], "app")

    def test_blacklist_read_handles_missing_platform_scope(self):
        """Verify backwards compatibility when platform_scope is absent."""
        def open_request(request, timeout):
            return FakeResponse({
                "rules": [
                    {"rule_id": "r1", "rule_type": "app", "pattern": "old-app",
                     "normalized_pattern": "old-app", "label": "Old", "enabled": True},
                    {"rule_id": "r2", "rule_type": "domain", "pattern": "old.com",
                     "normalized_pattern": "old.com", "label": "Old Site", "enabled": True},
                ]
            })

        rules = CentralReadClient(
            "https://central.example.test", "read-secret", opener=open_request,
        ).read_blacklist_rules()

        self.assertEqual(len(rules), 2)
        self.assertIsNone(rules[0].get("platform_scope"))
        self.assertIsNone(rules[1].get("platform_scope"))


if __name__ == "__main__":
    unittest.main()
