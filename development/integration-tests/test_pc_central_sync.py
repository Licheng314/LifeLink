import http.client
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "central-server"))
sys.path.insert(0, str(PROJECT_ROOT / "pc-dashboard"))

from central.config import CentralConfig
from central.http import create_server


SERVER_PATH = PROJECT_ROOT / "pc-dashboard" / "sync_server.py"
SPEC = importlib.util.spec_from_file_location("life_radio_pc_central_test", SERVER_PATH)
sync_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_server)


def aw_event(event_id=None, *, duration=10, occurred_at="2026-07-31T01:00:00Z"):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "occurred_at": occurred_at,
        "event_type": "app.foreground",
        "source": {
            "kind": "desktop", "collector": "activitywatch",
            "reliability": "observed",
        },
        "duration_seconds": duration,
        "payload": {
            "app": {"package_name": "chrome.exe", "display_name": "Chrome"},
            "activitywatch": {"kind": "window", "event_id": 7},
        },
        "_received_at": "2026-07-31T01:01:00Z",
    }


def custom_event(event_id=None, *, occurred_at="2026-07-31T02:00:00Z"):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "occurred_at": occurred_at,
        "event_type": "custom.event",
        "source": {
            "kind": "desktop", "collector": "life_radio_app",
            "reliability": "observed",
        },
        "payload": {"event_key": "test.event", "title": "Test"},
        "_received_at": "2026-07-31T02:00:01Z",
    }


class PcCentralSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        sync_server.close_central_outbox()
        sync_server.DATA_DIR = root / "data"
        sync_server.CENTRAL_OUTBOX_PATH = root / "outbox.sqlite3"
        sync_server.CENTRAL_IDENTITY_PATH = root / "identity.json"
        sync_server.CENTRAL_BASE_URL = ""
        sync_server.CENTRAL_MAX_BATCHES_PER_RUN = 20
        sync_server.display_date_today = lambda: "2026-07-31"
        self.central_servers = []
        self.previous_central_token = os.environ.get("LIFE_RADIO_CENTRAL_TOKEN")
        self.previous_central_read_token = os.environ.get(
            "LIFE_RADIO_CENTRAL_READ_TOKEN"
        )
        os.environ.pop("LIFE_RADIO_CENTRAL_TOKEN", None)
        os.environ.pop("LIFE_RADIO_CENTRAL_READ_TOKEN", None)

    def tearDown(self):
        for server, thread in self.central_servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        sync_server.close_central_outbox()
        if self.previous_central_token is None:
            os.environ.pop("LIFE_RADIO_CENTRAL_TOKEN", None)
        else:
            os.environ["LIFE_RADIO_CENTRAL_TOKEN"] = self.previous_central_token
        if self.previous_central_read_token is None:
            os.environ.pop("LIFE_RADIO_CENTRAL_READ_TOKEN", None)
        else:
            os.environ["LIFE_RADIO_CENTRAL_READ_TOKEN"] = (
                self.previous_central_read_token
            )
        self.directory.cleanup()

    def request_json(self, server, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=3,
        )
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def post_json(self, server, path, payload):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=3,
        )
        body = json.dumps(payload).encode("utf-8")
        connection.request(
            "POST", path, body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response_body = json.loads(response.read())
        connection.close()
        return response.status, response_body

    def test_activitywatch_events_receive_explicit_monotonic_revision(self):
        event_id = str(uuid.uuid4())
        first = aw_event(event_id, duration=10)
        unchanged = aw_event(event_id, duration=10)
        updated = aw_event(event_id, duration=25)

        first_result = sync_server.enqueue_local_central_events(
            [first], provenance="activitywatch",
        )
        unchanged_result = sync_server.enqueue_local_central_events(
            [unchanged], provenance="activitywatch",
        )
        updated_result = sync_server.enqueue_local_central_events(
            [updated], provenance="activitywatch",
        )
        stored = sync_server.get_central_outbox().event_status(event_id)
        document = json.loads(stored["event_json"])

        self.assertEqual(first_result["changed"], 1)
        self.assertEqual(unchanged_result["unchanged"], 1)
        self.assertEqual(updated_result["changed"], 1)
        self.assertEqual(document["revision"], 2)
        self.assertEqual(document["duration_seconds"], 25)

    def test_two_day_bootstrap_excludes_remote_and_older_local_events(self):
        local_device = sync_server.local_desktop_device_descriptor()
        remote_device = sync_server.v1_device_descriptor({
            "device_id": "desktop-22222222-2222-4222-8222-222222222222",
            "platform": "desktop",
            "display_name": "Remote PC",
        })
        today = aw_event(occurred_at="2026-07-31T01:00:00Z")
        yesterday = custom_event(occurred_at="2026-07-30T02:00:00Z")
        older = aw_event(occurred_at="2026-07-29T01:00:00Z")
        remote = aw_event(occurred_at="2026-07-31T03:00:00Z")
        sync_server.write_device_events(
            [today, yesterday, older],
            device=local_device,
            batch_metadata={"batch_id": "local", "received_at": "2026-07-31T03:00:00Z"},
        )
        sync_server.write_device_events(
            [remote],
            device=remote_device,
            batch_metadata={"batch_id": "remote", "received_at": "2026-07-31T03:00:00Z"},
        )

        result = sync_server.bootstrap_local_central_outbox()
        outbox = sync_server.get_central_outbox()

        self.assertEqual(result["scanned"], 2)
        self.assertEqual(outbox.status()["total"], 2)
        self.assertIsNotNone(outbox.event_status(today["event_id"]))
        self.assertIsNotNone(outbox.event_status(yesterday["event_id"]))
        self.assertIsNone(outbox.event_status(older["event_id"]))
        self.assertIsNone(outbox.event_status(remote["event_id"]))
        self.assertEqual(
            sync_server.bootstrap_local_central_outbox()["status"], "complete",
        )

    def test_custom_event_is_enqueued_but_mixed_remote_input_is_excluded(self):
        local, error = sync_server.create_local_custom_event({
            "event_key": "application.started",
            "title": "Life Link started",
        })
        remote_like = aw_event()
        excluded = sync_server.enqueue_local_central_events(
            [remote_like], provenance="received_remote",
        )

        self.assertIsNone(error)
        self.assertIsNotNone(
            sync_server.get_central_outbox().event_status(local["event_id"]),
        )
        self.assertEqual(excluded["excluded"], 1)
        self.assertIsNone(
            sync_server.get_central_outbox().event_status(remote_like["event_id"]),
        )
        with self.assertRaises(TypeError):
            sync_server.enqueue_local_central_events([remote_like])

    def test_real_local_central_upload(self):
        token = "desktop-test-token-0123456789-abcdef"
        device = sync_server.local_desktop_device_descriptor()
        central_database = Path(self.directory.name) / "central.sqlite3"
        central = create_server(CentralConfig(
            database_path=central_database,
            host="127.0.0.1",
            port=0,
            token_bindings={token: device["device_id"]},
        ))
        central_thread = threading.Thread(target=central.serve_forever, daemon=True)
        central_thread.start()
        self.central_servers.append((central, central_thread))
        sync_server.CENTRAL_BASE_URL = f"http://127.0.0.1:{central.server_port}"
        os.environ["LIFE_RADIO_CENTRAL_TOKEN"] = token
        sync_server.collect_activitywatch_events = lambda: {
            "status": "offline", "queued": 0, "updated": 0,
        }

        item = aw_event()
        sync_server.enqueue_local_central_events(
            [item], provenance="activitywatch",
        )

        result = sync_server.sync_central_once()

        self.assertIsNone(result["error"])
        self.assertEqual(result["uploads"][0]["status"], "ok")
        connection = sqlite3.connect(central_database)
        try:
            row = connection.execute(
                "SELECT device_id, revision FROM events WHERE event_id = ?",
                (item["event_id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (device["device_id"], 1))

    def test_central_status_and_immediate_sync_local_api_exist(self):
        invoked = threading.Event()
        original_sync = sync_server.sync_central_once
        sync_server.sync_central_once = lambda force_retry=False: invoked.set()
        server = sync_server.ThreadedHTTPServer(
            ("127.0.0.1", 0), sync_server.SyncHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3,
            )
            connection.request("GET", "/api/sync/central")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["mode"], "central")

            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3,
            )
            connection.request("POST", "/api/sync/central")
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 202)
            self.assertEqual(body["mode"], "central")
            self.assertTrue(invoked.wait(2))
        finally:
            sync_server.sync_central_once = original_sync
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_central_read_proxy_uses_business_window_and_stable_local_identity(self):
        local = sync_server.local_desktop_device_descriptor()
        remote_id = "desktop-22222222-2222-4222-8222-222222222222"
        captured = {}

        class FakeReadClient:
            def __init__(self, base_url, token):
                captured["base_url"] = base_url
                captured["token"] = token

            def read_view(
                self, view, *, from_utc, to_utc, local_device_id=None,
            ):
                captured.setdefault("calls", []).append({
                    "view": view,
                    "from": from_utc,
                    "to": to_utc,
                    "local_device_id": local_device_id,
                })
                if view == "devices":
                    return {
                        "online_window_seconds": 900,
                        "devices": [
                            {
                                "device_id": local["device_id"],
                                "device_key": "wrong-local-key",
                                "display_name": "Duplicate local card",
                                "platform": "desktop",
                                "status": "connected",
                                "last_seen_at": "2026-07-31T10:00:00Z",
                                "event_count": 1,
                                "batch_count": 1,
                                "categories": {"app.foreground": 1},
                            },
                            {
                                "device_id": remote_id,
                                "device_key": "server-derived-key",
                                "display_name": local["display_name"],
                                "platform": "desktop",
                                "status": "connected",
                                "last_seen_at": "2026-07-31T09:00:00Z",
                                "event_count": 4,
                                "batch_count": 2,
                                "categories": {"app.foreground": 4},
                            },
                        ],
                    }
                return {
                    "devices": [
                        {
                            "device_id": remote_id,
                            "device_key": "wrong-remote-key",
                            "display_name": local["display_name"],
                            "platform": "desktop",
                            "events": 4,
                            "apps": {"Remote App": 120},
                        },
                        {
                            "device_id": local["device_id"],
                            "device_key": "wrong-local-key",
                            "display_name": local["display_name"],
                            "platform": "desktop",
                            "events": 2,
                            "apps": {"Local App": 60},
                        },
                    ],
                    "all": {"events": 6, "apps": {"All": 180}},
                }

        original_client = sync_server.CENTRAL_READ_CLIENT_CLASS
        original_day_start = sync_server.get_day_start_hour
        sync_server.CENTRAL_READ_CLIENT_CLASS = FakeReadClient
        sync_server.get_day_start_hour = lambda: 4
        sync_server.CENTRAL_BASE_URL = "https://central.example.test"
        os.environ["LIFE_RADIO_CENTRAL_READ_TOKEN"] = "read-only-secret"
        server = sync_server.ThreadedHTTPServer(
            ("127.0.0.1", 0), sync_server.SyncHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            device_status, devices = self.request_json(
                server, "/api/devices?date=2026-07-31",
            )
            usage_status, usage = self.request_json(
                server, "/api/usage?date=2026-07-31",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            sync_server.CENTRAL_READ_CLIENT_CLASS = original_client
            sync_server.get_day_start_hour = original_day_start

        self.assertEqual((device_status, usage_status), (200, 200))
        self.assertEqual(devices["local"]["device_id"], local["device_id"])
        self.assertEqual(len(devices["devices"]), 1)
        self.assertEqual(devices["devices"][0]["device_id"], remote_id)
        self.assertEqual(
            devices["devices"][0]["device_key"],
            sync_server.device_storage_key(remote_id, "desktop"),
        )
        self.assertEqual(devices["devices"][0]["today"]["event_count"], 4)
        self.assertEqual(usage["devices"][0]["device_id"], local["device_id"])
        self.assertTrue(usage["devices"][0]["is_local"])
        self.assertFalse(usage["devices"][1]["is_local"])
        self.assertEqual(usage["devices"][0]["hourly"], {})
        for call in captured["calls"]:
            self.assertEqual(call["from"], "2026-07-30T20:00:00Z")
            self.assertEqual(call["to"], "2026-07-31T20:00:00Z")
            self.assertEqual(call["local_device_id"], local["device_id"])
        self.assertNotIn("read-only-secret", json.dumps(devices))
        self.assertNotIn("read-only-secret", json.dumps(usage))

    def test_central_read_proxy_returns_explanatory_503_and_502(self):
        sync_server.CENTRAL_BASE_URL = "https://central.example.test"
        server = sync_server.ThreadedHTTPServer(
            ("127.0.0.1", 0), sync_server.SyncHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, missing = self.request_json(server, "/api/devices")
            self.assertEqual(status, 503)
            self.assertEqual(missing["error"], "central_read_not_configured")

            os.environ["LIFE_RADIO_CENTRAL_READ_TOKEN"] = "secret"

            class FailingReadClient:
                def __init__(self, base_url, token):
                    pass

                def read_view(self, *args, **kwargs):
                    raise sync_server.CentralReadError(
                        "central_unavailable", "central read connection failed",
                    )

            original_client = sync_server.CENTRAL_READ_CLIENT_CLASS
            sync_server.CENTRAL_READ_CLIENT_CLASS = FailingReadClient
            try:
                status, failed = self.request_json(server, "/api/usage")
            finally:
                sync_server.CENTRAL_READ_CLIENT_CLASS = original_client
            self.assertEqual(status, 502)
            self.assertEqual(failed["error"], "central_unavailable")
            self.assertNotIn("secret", json.dumps(failed))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

if __name__ == "__main__":
    unittest.main()
