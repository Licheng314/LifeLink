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
from central.storage import CentralStore


DESKTOP_TOKEN = "desktop-management-token-0123456789-ABCDEFG"
PHONE_TOKEN = "android-management-token-0123456789-ABCDEFGH"
READ_TOKEN = "device-management-read-token-0123456789-ABCDE"


class DeviceManagementHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_path = root / "central.json"
        self.database_path = root / "central.sqlite3"
        self.config_path.write_text(json.dumps({
            "database_path": str(self.database_path),
            "host": "127.0.0.1",
            "port": 0,
            "token_bindings": {
                DESKTOP_TOKEN: "desktop-a",
                PHONE_TOKEN: "android-b",
            },
            "read_token": READ_TOKEN,
        }), encoding="utf-8")
        config = CentralConfig(
            database_path=self.database_path,
            host="127.0.0.1",
            port=0,
            token_bindings={DESKTOP_TOKEN: "desktop-a", PHONE_TOKEN: "android-b"},
            read_token=READ_TOKEN,
            config_path=self.config_path,
        )
        self.server = create_server(config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.upload("desktop-a", "desktop", "Office PC", DESKTOP_TOKEN)
        self.upload("android-b", "android", "Phone Model", PHONE_TOKEN)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, token, payload=None, batch_id=None):
        headers = {"Authorization": f"Bearer {token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if batch_id:
            headers["Idempotency-Key"] = batch_id
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8")) if raw else None

    def upload(self, device_id, platform, display_name, token):
        batch_id = str(uuid.uuid4())
        payload = {
            "schema_version": "v1",
            "batch_id": batch_id,
            "device": {"device_id": device_id, "platform": platform, "display_name": display_name},
            "sent_at": "2026-08-11T12:00:00Z",
            "events": [{
                "event_id": str(uuid.uuid4()),
                "occurred_at": "2026-08-11T12:00:00Z",
                "event_type": "app.foreground",
                "source": {
                    "kind": platform, "collector": "activitywatch" if platform == "desktop" else "usage_stats",
                    "reliability": "observed",
                },
                "duration_seconds": 60,
                "payload": {"app": {"display_name": "Reader"}},
            }],
        }
        status, response = self.request("POST", "/v1/events/batches", token, payload, batch_id)
        self.assertEqual(status, 200, response)

    def test_rename_survives_upload_and_updates_timeline_projection(self):
        status, renamed = self.request(
            "POST", "/v1/devices/android-b", DESKTOP_TOKEN,
            {"display_name": "随身手机"},
        )
        self.assertEqual(status, 200, renamed)
        self.assertEqual(renamed["reported_name"], "Phone Model")
        self.assertEqual(renamed["display_name"], "随身手机")
        self.assertFalse(renamed["is_current"])

        self.upload("android-b", "android", "New Phone Model", PHONE_TOKEN)
        status, roster = self.request("GET", "/v1/devices", READ_TOKEN)
        self.assertEqual(status, 200, roster)
        phone = next(item for item in roster["devices"] if item["device_id"] == "android-b")
        self.assertFalse(phone["is_current"])
        self.assertEqual(phone["reported_name"], "New Phone Model")
        self.assertEqual(phone["custom_name"], "随身手机")
        self.assertEqual(phone["display_name"], "随身手机")
        status, device_roster = self.request("GET", "/v1/devices", DESKTOP_TOKEN)
        self.assertEqual(status, 200, device_roster)
        self.assertTrue(next(item for item in device_roster["devices"] if item["device_id"] == "desktop-a")["is_current"])
        usage = self.server.store.read_usage(
            datetime(2026, 8, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 12, 0, tzinfo=timezone.utc),
        )
        usage_phone = next(item for item in usage["devices"] if item["device_id"] == "android-b")
        self.assertEqual(usage_phone["display_name"], "随身手机")

        now = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
        with self.server.store._connection() as connection:
            self.server.store._append_timeline(
                connection, occurred_at="2026-08-11T12:10:00Z",
                event_key="device_usage_milestone", category="trigger", importance="normal",
                title="设备 Phone Model 累计使用 60 分钟", source_kind="central",
                source_device_id=None, wish_id=None, trigger_id=None,
                subject={"device_id": "android-b", "milestone_minutes": 60}, evidence={},
                detail="本业务日设备使用时长已达到 240 分钟",
                dedupe_key="device-management-title-test",
            )
        timeline = self.server.store.list_timeline(now, now + timedelta(hours=1))
        self.assertEqual(timeline["events"][0]["title"], "设备使用·随身手机")
        self.assertEqual(timeline["events"][0]["detail"], "本业务日设备使用时长已达到 4小时")
        self.assertEqual(timeline["events"][0]["device_display_name"], "随身手机")

    def test_delete_revokes_credentials_preserves_facts_and_disables_bound_trigger(self):
        trigger = self.server.store.create_trigger(
            request_id=str(uuid.uuid4()), request_hash=content_hash({"test": "device"}),
            wish_id=None, trigger_type="device_usage_milestone", config_version=1,
            parameters={"device_id": "android-b"}, interval_minutes=60, enabled=True,
        )
        status, _ = self.request("POST", "/v1/devices/android-b/delete", DESKTOP_TOKEN)
        self.assertEqual(status, 204)
        self.assertIsNone(self.server.store.device_for_token(PHONE_TOKEN))
        bindings = json.loads(self.config_path.read_text(encoding="utf-8"))["token_bindings"]
        self.assertNotIn(PHONE_TOKEN, bindings)
        self.assertIn(DESKTOP_TOKEN, bindings)

        status, roster = self.request("GET", "/v1/devices", READ_TOKEN)
        self.assertEqual(status, 200)
        self.assertNotIn("android-b", {item["device_id"] for item in roster["devices"]})
        status, body = self.request("GET", "/v1/wishes", PHONE_TOKEN)
        self.assertEqual(status, 401, body)
        with self.server.store._connection() as connection:
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM events WHERE device_id='android-b'").fetchone()[0], 0)
        stored_trigger = next(item for item in self.server.store.list_triggers() if item["trigger_id"] == trigger["trigger_id"])
        self.assertFalse(stored_trigger["enabled"])
        current_usage = self.server.store.read_usage(
            datetime(2026, 8, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIn("android-b", {item["device_id"] for item in current_usage["devices"]})
        later_usage = self.server.store.read_usage(
            datetime(2026, 8, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 0, tzinfo=timezone.utc),
        )
        self.assertNotIn("android-b", {item["device_id"] for item in later_usage["devices"]})

        # Transport retries are idempotent after persistent cleanup.
        self.assertEqual(self.request("POST", "/v1/devices/android-b/delete", DESKTOP_TOKEN)[0], 204)

    def test_current_device_cannot_delete_itself(self):
        status, body = self.request("DELETE", "/v1/devices/desktop-a", DESKTOP_TOKEN)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "cannot_delete_current_device")


class DeviceRetirementRestartTests(unittest.TestCase):
    def test_stale_binding_cannot_revive_retired_device(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "central.sqlite3"
            store = CentralStore(database, {PHONE_TOKEN: "android-b"})
            with store._connection() as connection:
                connection.execute(
                    """INSERT INTO devices(device_id, platform, display_name, first_seen_at, last_seen_at)
                       VALUES ('android-b', 'android', 'Phone', '2026-08-11T12:00:00Z', '2026-08-11T12:00:00Z')"""
                )
            self.assertTrue(store.retire_device("android-b"))
            restarted = CentralStore(database, {PHONE_TOKEN: "android-b"})
            self.assertIsNone(restarted.device_for_token(PHONE_TOKEN))

    def test_new_invitation_reactivates_same_identity_with_new_token_only(self):
        new_token = "replacement-device-token-0123456789-ABCDEFGH"
        with tempfile.TemporaryDirectory() as temp:
            store = CentralStore(Path(temp) / "central.sqlite3", {PHONE_TOKEN: "android-b"})
            with store._connection() as connection:
                connection.execute(
                    """INSERT INTO devices(device_id, platform, display_name, custom_name, first_seen_at, last_seen_at)
                       VALUES ('android-b', 'android', 'Phone', '随身手机', '2026-08-11T12:00:00Z', '2026-08-11T12:00:00Z')"""
                )
            self.assertTrue(store.retire_device("android-b"))
            invitation_id = str(uuid.uuid4())
            store.create_client_invitation(
                invitation_id=invitation_id,
                invitation_token="one-time-invitation-token-0123456789-ABCDE",
                scope="dashboard", central_base_url="https://central.example.test",
                created_at="2026-08-11T12:00:00Z", expires_at="2026-08-12T12:00:00Z",
            )
            store.claim_client_invitation(
                invitation_id=invitation_id,
                invitation_token="one-time-invitation-token-0123456789-ABCDE",
                device_id="android-b", platform="android", display_name="Phone 2",
                claimed_at="2026-08-11T13:00:00Z",
                credential_provider=lambda _device, _create, _scope: (new_token, READ_TOKEN),
            )
            self.assertIsNone(store.device_for_token(PHONE_TOKEN))
            self.assertEqual(store.device_for_token(new_token), "android-b")
            device = store.list_managed_devices()[0]
            self.assertEqual(device["display_name"], "随身手机")


class CredentialProvisionSafetyTests(unittest.TestCase):
    def _seed_device_token(self, store, device_id, token):
        digest = CentralStore.token_hash(token)
        with store._connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO devices(device_id, platform, display_name, first_seen_at, last_seen_at)
                   VALUES (?, 'android', 'Phone', '2026-08-11T12:00:00Z', '2026-08-11T12:00:00Z')""",
                (device_id,),
            )
            connection.execute(
                """INSERT INTO device_tokens(token_hash, device_id, created_at, revoked_at)
                   VALUES (?, ?, '2026-08-11T12:00:00Z', NULL)
                   ON CONFLICT(token_hash) DO UPDATE SET device_id=excluded.device_id, revoked_at=NULL""",
                (digest, device_id),
            )

    def test_constructor_with_empty_bindings_keeps_existing_tokens(self):
        """Opening an existing DB with {} must not wipe registered devices."""
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "central.sqlite3"
            bootstrap = CentralStore(database, {})
            self._seed_device_token(bootstrap, "android-b", PHONE_TOKEN)
            self.assertEqual(bootstrap.device_for_token(PHONE_TOKEN), "android-b")

            reopened = CentralStore(database, {})
            self.assertEqual(reopened.device_for_token(PHONE_TOKEN), "android-b")

    def test_reconcile_revokes_unlisted_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "central.sqlite3"
            store = CentralStore(database, {PHONE_TOKEN: "android-b"})
            self._seed_device_token(store, "android-b", PHONE_TOKEN)
            second_token = "second-device-token-0123456789-ABCDEFGH"
            self._seed_device_token(store, "android-c", second_token)

            store.reconcile_credentials({PHONE_TOKEN: "android-b"})
            self.assertEqual(store.device_for_token(PHONE_TOKEN), "android-b")
            self.assertIsNone(store.device_for_token(second_token))

    def test_reconcile_refuses_empty_bindings_when_active_tokens_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "central.sqlite3"
            store = CentralStore(database, {PHONE_TOKEN: "android-b"})
            self._seed_device_token(store, "android-b", PHONE_TOKEN)
            with self.assertRaises(ValueError):
                store.reconcile_credentials({})
            # Token must survive the refused reconcile.
            self.assertEqual(store.device_for_token(PHONE_TOKEN), "android-b")

    def test_reconcile_does_not_revive_retired_device_token(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "central.sqlite3"
            store = CentralStore(database, {PHONE_TOKEN: "android-b"})
            self._seed_device_token(store, "android-b", PHONE_TOKEN)
            store.retire_device("android-b")
            store.reconcile_credentials({PHONE_TOKEN: "android-b"})
            self.assertIsNone(store.device_for_token(PHONE_TOKEN))

if __name__ == "__main__":
    unittest.main()
