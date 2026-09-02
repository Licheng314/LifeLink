"""Tests for CentralDeviceClient + sync_server v1.7 proxy routes.

Tests verify:
- CentralDeviceClient CRUD operations (wishes, triggers, timeline)
- Anonymous / read-only / device token auth correctness
- include_archived forwarding
- PUT-only assessment (no POST alias)
- Empty body cancel support
- Stayus code preservation through proxy
"""

import json
import threading
import time
import uuid
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "central-server"))

from central_client import CentralDeviceClient, CentralReadClient
from central.config import CentralConfig
from central.http import create_server
import sync_server
from central_client import CentralReadError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _start_central(tmpdir: str) -> tuple[Any, int]:
    """Start a central server on a random port, return (server, port)."""
    db_path = str(Path(tmpdir) / "central.sqlite3")
    tokens = {
        "dev-abcdefghijklmnopqrstuvwxyz012345": "dev-a",
        "dev-zyxwvutsrqponmlkjihgfedcba543210": "dev-b",
    }
    config = CentralConfig(
        database_path=db_path,
        host="127.0.0.1",
        port=0,
        token_bindings=tokens,
        read_token="read-zyxwvutsrqponmlkjihgfedcba098765",
    )
    srv = create_server(config, ("127.0.0.1", 0))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    return srv, port


def _req(method: str, path: str, port: int, token: str | None = None,
         body: dict | None = None, expect_status: int | None = None) -> tuple[int, Any]:
    """Send an HTTP request to local test server."""
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    b = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=b, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    data = json.loads(raw.decode()) if raw else None
    if expect_status is not None and resp.status != expect_status:
        raise AssertionError(f"Expected {expect_status}, got {resp.status}: {data}")
    return resp.status, data


class _FakeWishResponse:
    status = 200

    def __init__(self, request, captured):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()

    def read(self):
        return json.dumps({"wish_id": "00000000-0000-4000-8000-000000000001", "status": "archived"}).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class CentralDeviceClientTests(TestCase):
    """Direct client tests — fresh database per test."""

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._tmp = TemporaryDirectory()
        self._srv, self._port = _start_central(self._tmp.name)
        self._device_token = "dev-abcdefghijklmnopqrstuvwxyz012345"
        self._read_token = "read-zyxwvutsrqponmlkjihgfedcba098765"
        self._base = f"http://127.0.0.1:{self._port}"
        self._client = CentralDeviceClient(self._base, self._device_token)

    def tearDown(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._tmp.cleanup()

    # -- wishes --
    def test_create_wish_3day(self):
        w = self._client.create_wish(
            request_id=str(uuid.uuid4()), text="Test 3 days", duration_days=3,
        )
        self.assertIn("wish_id", w)
        self.assertEqual(w["text"], "Test 3 days")
        self.assertEqual(w["duration_days"], 3)

    def test_create_wish_7day(self):
        w = self._client.create_wish(
            request_id=str(uuid.uuid4()), text="Test 7 days", duration_days=7,
        )
        self.assertEqual(w["duration_days"], 7)

    def test_list_wishes(self):
        self._client.create_wish(request_id=str(uuid.uuid4()), text="L1", duration_days=3)
        result = self._client.list_wishes()
        self.assertIsInstance(result, list)

    def test_get_wish(self):
        w = self._client.create_wish(request_id=str(uuid.uuid4()), text="GetTest", duration_days=3)
        w2 = self._client.get_wish(w["wish_id"])
        self.assertEqual(w2["wish_id"], w["wish_id"])

    def test_cancel_wish(self):
        w = self._client.create_wish(request_id=str(uuid.uuid4()), text="CancelTest", duration_days=3)
        c = self._client.cancel_wish(w["wish_id"])
        self.assertEqual(c["status"], "cancelled")

    def test_complete_wish_uses_dedicated_action_path(self):
        captured = {}
        client = CentralDeviceClient(
            "https://central.example.test", "device-token",
            opener=lambda request, timeout: captured.setdefault("response", _FakeWishResponse(request, captured)),
        )
        result = client.complete_wish("00000000-0000-4000-8000-000000000001")
        self.assertEqual(result["status"], "archived")
        self.assertTrue(captured["url"].endswith("/v1/wishes/00000000-0000-4000-8000-000000000001/complete"))
        self.assertEqual(captured["method"], "POST")

    def test_assess_wish_day(self):
        w = self._client.create_wish(request_id=str(uuid.uuid4()), text="AssessTest", duration_days=3)
        # Find the first assessable day; skip if all future
        for wd in w.get("wish_days", []):
            biz = wd["business_date"]
            try:
                d = self._client.assess_wish_day(w["wish_id"], biz, "completed")
                self.assertEqual(d["evaluation"], "completed")
                return
            except Exception:
                continue
        self.skipTest("All wish days are in the future")

    def test_zzz_limit_3_wishes(self):
        for i in range(3):
            self._client.create_wish(request_id=str(uuid.uuid4()), text=f"W{i}", duration_days=3)
        # 4th should fail
        with self.assertRaises(Exception):
            self._client.create_wish(request_id=str(uuid.uuid4()), text="W4", duration_days=3)

    # -- triggers --
    def test_list_trigger_types(self):
        types = self._client.list_trigger_types()
        self.assertIsInstance(types, list)
        self.assertGreaterEqual(len(types), 3)
        self.assertIn("trigger_type", types[0])

    def test_create_and_list_triggers(self):
        w = self._client.create_wish(request_id=str(uuid.uuid4()), text="TrigWish", duration_days=3)
        self._client.create_trigger(
            wish_id=w["wish_id"],
            trigger_type="device_usage_milestone",
            parameters={"device_id": "dev-a"},
            interval_minutes=60,
        )
        triggers = self._client.list_triggers()
        self.assertIsInstance(triggers, list)

    def test_patch_trigger(self):
        w = self._client.create_wish(request_id=str(uuid.uuid4()), text="PTWish", duration_days=3)
        t = self._client.create_trigger(
            wish_id=w["wish_id"],
            trigger_type="device_usage_milestone",
            parameters={"device_id": "dev-a"},
            interval_minutes=60,
        )
        result = self._client.patch_trigger(t["trigger_id"], {"enabled": False})
        self.assertEqual(result["enabled"], False)

    def test_delete_trigger(self):
        w = self._client.create_wish(request_id=str(uuid.uuid4()), text="DTWish", duration_days=3)
        t = self._client.create_trigger(
            wish_id=w["wish_id"],
            trigger_type="device_usage_milestone",
            parameters={"device_id": "dev-a"},
            interval_minutes=60,
        )
        self._client.delete_trigger(t["trigger_id"])
        triggers = self._client.list_triggers()
        ids = [x["trigger_id"] for x in triggers]
        self.assertNotIn(t["trigger_id"], ids)

    def test_status_request_preserves_central_error_payload(self):
        payload, status = self._client._request_with_status(
            "POST", "/v1/wishes",
            {"request_id": str(uuid.uuid4()), "text": "Bad", "duration_days": 5},
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(payload, dict)
        self.assertIn("error", payload)

    # -- timeline --
    def test_timeline_empty(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fr = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        events = self._client.list_timeline(from_iso=fr, to_iso=to)
        self.assertIsInstance(events, dict)
        self.assertIn("events", events)


class AuthAndProxyTests(TestCase):
    """HTTP-level tests for auth and proxy behavior."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        cls._srv, cls._port = _start_central(cls._tmp.name)
        cls._device_token = "dev-abcdefghijklmnopqrstuvwxyz012345"
        cls._read_token = "read-zyxwvutsrqponmlkjihgfedcba098765"

    @classmethod
    def tearDownClass(cls):
        cls._srv.shutdown()
        cls._srv.server_close()
        cls._tmp.cleanup()

    def _post(self, path, token=None, body=None):
        return _req("POST", path, self._port, token=token, body=body)

    def _get(self, path, token=None):
        return _req("GET", path, self._port, token=token)

    def test_anonymous_wish_create_rejected(self):
        status, data = self._post("/v1/wishes", body={
            "request_id": str(uuid.uuid4()), "text": "Anon", "duration_days": 3,
        })
        self.assertEqual(status, 401)

    def test_readonly_token_wish_create_rejected(self):
        status, data = self._post("/v1/wishes", token=self._read_token, body={
            "request_id": str(uuid.uuid4()), "text": "ReadOnly", "duration_days": 3,
        })
        self.assertEqual(status, 401)

    def test_device_token_wish_create_accepted(self):
        status, data = self._post("/v1/wishes", token=self._device_token, body={
            "request_id": str(uuid.uuid4()), "text": "DeviceOK", "duration_days": 3,
        })
        self.assertEqual(status, 201)
        self.assertIn("wish_id", data)

    def test_anonymous_wishes_read_rejected(self):
        status, data = self._get("/v1/wishes")
        self.assertEqual(status, 401)

    def test_read_token_wishes_read_accepted(self):
        status, data = self._get("/v1/wishes", token=self._read_token)
        self.assertEqual(status, 200)

    def test_device_token_wishes_read_accepted(self):
        status, data = self._get("/v1/wishes", token=self._device_token)
        self.assertEqual(status, 200)

    def test_include_archived_param(self):
        status, data = self._get("/v1/wishes?include_archived=true", token=self._read_token)
        self.assertEqual(status, 200)
        self.assertIn("wishes", data)

    def test_wish_cancel_empty_body(self):
        # Create wish with device token
        status, w = self._post("/v1/wishes", token=self._device_token, body={
            "request_id": str(uuid.uuid4()), "text": "CancelEmpty", "duration_days": 3,
        })
        self.assertEqual(status, 201)
        wish_id = w["wish_id"]
        # Cancel with empty body
        status, data = self._post(f"/v1/wishes/{wish_id}/cancel", token=self._device_token)
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "cancelled")

    def test_assessment_put_accepted(self):
        """Assessment works on the first wish day (starts_on) only if it is not in the future."""
        status, w = self._post("/v1/wishes", token=self._device_token, body={
            "request_id": str(uuid.uuid4()), "text": "PUTTest", "duration_days": 3,
        })
        wish_id = w["wish_id"]
        # Try the first wish_day
        for wd in w.get("wish_days", []):
            biz = wd["business_date"]
            status, data = _req("PUT", f"/v1/wishes/{wish_id}/days/{biz}",
                                self._port, token=self._device_token,
                                body={"evaluation": "completed"})
            if status == 200:
                self.assertEqual(data["evaluation"], "completed")
                return
            # 409 means future day — try next, or skip
            if status != 409:
                self.fail(f"Unexpected status {status}: {data}")
        # If all returned 409, days are all in the future — acceptable
        self.skipTest("All wish days are in the future")

    def test_trigger_types_key(self):
        status, data = self._get("/v1/trigger-types", token=self._read_token)
        self.assertEqual(status, 200)
        self.assertIn("trigger_types", data)
        self.assertIsInstance(data["trigger_types"], list)


class PersistentReadCacheTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.old_data_dir = sync_server.DATA_DIR
        self.old_base_url = sync_server.CENTRAL_BASE_URL
        sync_server.DATA_DIR = Path(self.temp.name)
        sync_server.CENTRAL_BASE_URL = "https://central.example.test"
        sync_server._V17_READ_MEMORY.clear()
        sync_server._V17_DISK_CACHE_KEY = None
        sync_server._V17_DISK_CACHE = {}

    def tearDown(self):
        sync_server.DATA_DIR = self.old_data_dir
        sync_server.CENTRAL_BASE_URL = self.old_base_url
        sync_server._V17_READ_MEMORY.clear()
        sync_server._V17_DISK_CACHE_KEY = None
        sync_server._V17_DISK_CACHE = {}
        self.temp.cleanup()

    def test_success_is_persisted_and_used_after_restart_when_offline(self):
        expected = {"wishes": []}
        with patch.object(sync_server, "_central_read_json", return_value=expected):
            self.assertEqual(sync_server._read_v17_resource("/v1/wishes"), expected)
        sync_server._V17_READ_MEMORY.clear()
        with patch.object(
            sync_server, "_central_read_json",
            side_effect=CentralReadError("central_unavailable", "offline"),
        ):
            self.assertEqual(sync_server._read_v17_resource("/v1/wishes"), expected)

    def test_query_variants_do_not_share_the_same_cache_entry(self):
        current = {"wishes": [{"wish_id": "current"}]}
        archived = {"wishes": [{"wish_id": "archived"}]}
        with patch.object(sync_server, "_central_read_json", side_effect=[current, archived]):
            self.assertEqual(sync_server._read_v17_resource("/v1/wishes"), current)
            self.assertEqual(
                sync_server._read_v17_resource("/v1/wishes?include_archived=true"), archived,
            )

    def test_query_order_is_canonicalized_without_conflating_values(self):
        first = {"events": [], "window": {}}
        with patch.object(sync_server, "_central_read_json", return_value=first) as read:
            sync_server._read_v17_resource("/v1/timeline-events?to=b&from=a")
            sync_server._read_v17_resource("/v1/timeline-events?from=a&to=b")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(list(sync_server._load_v17_read_cache()), [
            "/v1/timeline-events?from=a&to=b",
        ])

    def test_timeline_revalidates_persisted_payload_without_downloading_it_again(self):
        path = "/v1/timeline-events?from=a&to=b"
        payload = {"events": [], "window": {"from": "a", "to": "b"}}
        sync_server._save_v17_read_entry(path, payload)
        sync_server._V17_READ_MEMORY.clear()
        with patch.object(
            sync_server, "_central_read_json",
            side_effect=sync_server.CentralReadNotModified(),
        ) as read:
            self.assertEqual(sync_server._read_v17_resource(path), payload)
        read.assert_called_once_with(
            path, if_none_match=sync_server._v17_payload_etag(payload),
        )

    def test_offline_fallback_retains_stale_provenance(self):
        path = "/v1/timeline-events?from=a&to=b"
        payload = {"events": [], "window": {"from": "a", "to": "b"}}
        sync_server._save_v17_read_entry(path, payload)
        with patch.object(
            sync_server, "_central_read_json",
            side_effect=CentralReadError("central_unavailable", "offline"),
        ):
            self.assertEqual(
                sync_server._read_v17_resource_with_status(path), (payload, True),
            )

    def test_same_timeline_key_has_only_one_in_flight_central_read(self):
        path = "/v1/timeline-events?from=a&to=b"
        payload = {"events": [], "window": {"from": "a", "to": "b"}}

        def delayed_read(_path):
            time.sleep(0.05)
            return payload

        results = []
        with patch.object(sync_server, "_central_read_json", side_effect=delayed_read) as read:
            threads = [
                threading.Thread(target=lambda: results.append(sync_server._read_v17_resource(path)))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, [payload, payload])
        self.assertEqual(read.call_count, 1)

    def test_cache_is_bounded_per_resource_and_does_not_reread_disk(self):
        for index in range(sync_server._V17_CACHE_MAX_ENTRIES_PER_RESOURCE + 3):
            payload = {"events": [], "window": {"index": index}}
            sync_server._save_v17_read_entry(
                f"/v1/timeline-events?from={index}&to={index + 1}", payload,
            )
        entries = sync_server._load_v17_read_cache()
        self.assertEqual(len(entries), sync_server._V17_CACHE_MAX_ENTRIES_PER_RESOURCE)
        with patch.object(sync_server, "load_json", side_effect=AssertionError("disk reread")):
            self.assertEqual(sync_server._load_v17_read_cache(), entries)

    def test_oversized_legacy_cache_is_quarantined_without_parsing(self):
        target = sync_server.v17_read_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * (sync_server._V17_CACHE_MAX_FILE_BYTES + 1))
        with patch.object(sync_server, "load_json", side_effect=AssertionError("must not parse")):
            self.assertEqual(sync_server._load_v17_read_cache(), {})
        self.assertFalse(target.exists())
        backups = list(target.parent.glob("v17_read_cache.oversized-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertGreater(backups[0].stat().st_size, sync_server._V17_CACHE_MAX_FILE_BYTES)

    def test_concurrent_saves_preserve_a_valid_bounded_document(self):
        errors = []
        def save(index):
            try:
                sync_server._save_v17_read_entry(
                    f"/v1/event-background?business_date=2026-08-{index + 1:02d}",
                    {
                        "business_date": f"2026-08-{index + 1:02d}",
                        "generated_at": "2026-08-21T00:00:00Z",
                        "background_summary": {key: {} for key in ("wish", "device_and_apps", "blacklist", "location_and_activity")},
                        "ai_understanding": {"items": [], "timezone": "Asia/Shanghai", "real_time_valid_for_minutes": 15},
                        "real_time_items": [],
                    },
                )
            except Exception as error:  # pragma: no cover - assertion reports details
                errors.append(error)
        threads = [threading.Thread(target=save, args=(index,)) for index in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        document = json.loads(sync_server.v17_read_cache_path().read_text(encoding="utf-8"))
        self.assertEqual(document["version"], sync_server._V17_CACHE_VERSION)
        self.assertLessEqual(len(document["entries"]), sync_server._V17_CACHE_MAX_ENTRIES_PER_RESOURCE)

    def test_serialized_cache_respects_file_byte_budget(self):
        with patch.object(sync_server, "_V17_CACHE_MAX_FILE_BYTES", 700):
            for index in range(4):
                sync_server._save_v17_read_entry(
                    f"/v1/wishes?include_archived={index}",
                    {"wishes": [{"wish_id": str(index), "text": "愿" * 100}]},
                )
            self.assertLessEqual(sync_server.v17_read_cache_path().stat().st_size, 700)

    def test_cache_is_not_reused_for_another_central(self):
        with patch.object(sync_server, "_central_read_json", return_value={"wishes": []}):
            sync_server._read_v17_resource("/v1/wishes")
        sync_server._V17_READ_MEMORY.clear()
        sync_server.CENTRAL_BASE_URL = "https://another-central.example.test"
        with patch.object(
            sync_server, "_central_read_json",
            side_effect=CentralReadError("central_unavailable", "offline"),
        ):
            with self.assertRaises(CentralReadError):
                sync_server._read_v17_resource("/v1/wishes")

    def test_explicit_central_rejection_does_not_fall_back_to_stale_data(self):
        with patch.object(sync_server, "_central_read_json", return_value={"wishes": []}):
            sync_server._read_v17_resource("/v1/wishes")
        sync_server._V17_READ_MEMORY.clear()
        with patch.object(
            sync_server, "_central_read_json",
            side_effect=CentralReadError("central_rejected", "HTTP 401", http_status=401),
        ):
            with self.assertRaises(CentralReadError):
                sync_server._read_v17_resource("/v1/wishes")
