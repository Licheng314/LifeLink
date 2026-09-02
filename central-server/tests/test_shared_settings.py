import http.client
import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from central.config import CentralConfig
from central.http import create_server


DASHBOARD_TOKEN = "dashboard-device-token-0123456789-ABCDEFGHI"
SECOND_DASHBOARD_TOKEN = "dashboard-device-token-2-0123456789-ABCDEFG"
READ_TOKEN = "shared-settings-read-token-0123456789-ABCDEFGHIJK"
UPLOAD_TOKEN = "upload-only-device-token-0123456789-ABCDEFGHIJK"


class SharedSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "central.sqlite3"
        self.config = CentralConfig(
            database_path=self.database,
            host="127.0.0.1",
            port=0,
            token_bindings={
                DASHBOARD_TOKEN: "desktop-settings-a",
                SECOND_DASHBOARD_TOKEN: "desktop-settings-b",
                UPLOAD_TOKEN: "android-upload-only",
            },
            read_token=READ_TOKEN,
        )
        self._start_server()
        now = datetime.now(timezone.utc)
        invitation_id = str(uuid.uuid4())
        invitation_token = "upload-invitation-token-0123456789-ABCDEFGHIJK"
        self.server.store.create_client_invitation(
            invitation_id=invitation_id,
            invitation_token=invitation_token,
            scope="upload",
            central_base_url="https://central.example.test",
            created_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        )
        self.server.store.claim_client_invitation(
            invitation_id=invitation_id,
            invitation_token=invitation_token,
            device_id="android-upload-only",
            platform="android",
            display_name="Upload only",
            claimed_at=now.isoformat().replace("+00:00", "Z"),
            credential_provider=lambda *_: (UPLOAD_TOKEN, None),
        )

    def tearDown(self):
        self._stop_server()
        self.directory.cleanup()

    def _start_server(self):
        self.server = create_server(self.config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _restart_server(self):
        self._stop_server()
        self._start_server()

    def request(self, method, path, *, token=None, payload=None):
        headers = {}
        body = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read().decode("utf-8")
        connection.close()
        return response.status, json.loads(content)

    def test_initializes_the_singleton_and_preserves_it_across_restart(self):
        status, initial = self.request("GET", "/v1/settings/shared", token=DASHBOARD_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(initial["timezone"], "Asia/Shanghai")
        self.assertEqual(initial["day_start_hour"], 0)
        self.assertIsNone(initial["primary_health_device_id"])
        self.assertEqual(initial["settings_version"], 1)
        self.assertTrue(initial["updated_at"].endswith("Z"))

        self.assertEqual(
            self.request("PATCH", "/v1/settings/shared", token=DASHBOARD_TOKEN, payload={"day_start_hour": 4})[0],
            200,
        )
        self._restart_server()
        status, restored = self.request("GET", "/v1/settings/shared", token=DASHBOARD_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(restored["day_start_hour"], 4)
        self.assertEqual(restored["settings_version"], 2)

    def test_patch_is_idempotent_and_strictly_validates_the_only_writable_field(self):
        status, changed = self.request(
            "PATCH", "/v1/settings/shared", token=DASHBOARD_TOKEN, payload={"day_start_hour": 7},
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["settings_version"], 2)

        status, repeated = self.request(
            "PATCH", "/v1/settings/shared", token=DASHBOARD_TOKEN, payload={"day_start_hour": 7},
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated, changed)

        for payload in (
            {},
            {"day_start_hour": 7, "timezone": "Asia/Shanghai"},
            {"day_start_hour": True},
            {"day_start_hour": "7"},
            {"day_start_hour": 7.0},
            {"day_start_hour": -1},
            {"day_start_hour": 24},
        ):
            with self.subTest(payload=payload):
                status, body = self.request(
                    "PATCH", "/v1/settings/shared", token=DASHBOARD_TOKEN, payload=payload,
                )
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    def test_post_has_the_same_idempotent_update_semantics_as_patch(self):
        status, changed = self.request(
            "POST", "/v1/settings/shared", token=DASHBOARD_TOKEN,
            payload={"day_start_hour": 6},
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["day_start_hour"], 6)
        self.assertEqual(changed["settings_version"], 2)

        status, repeated = self.request(
            "POST", "/v1/settings/shared", token=DASHBOARD_TOKEN,
            payload={"day_start_hour": 6},
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated, changed)

        self.assertEqual(
            self.request(
                "POST", "/v1/settings/shared", token=READ_TOKEN,
                payload={"day_start_hour": 7},
            )[0],
            401,
        )

    def test_primary_health_device_accepts_only_active_android_and_is_idempotent(self):
        with self.server.store._connection() as connection:
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            connection.execute(
                """INSERT OR IGNORE INTO devices(device_id, platform, display_name, custom_name, first_seen_at, last_seen_at, retired_at)
                   VALUES('android-upload-only', 'android', 'Upload only', NULL, ?, ?, NULL)""",
                (now, now),
            )
        status, changed = self.request(
            "POST", "/v1/settings/shared", token=DASHBOARD_TOKEN,
            payload={"primary_health_device_id": "android-upload-only"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["primary_health_device_id"], "android-upload-only")
        status, repeated = self.request(
            "POST", "/v1/settings/shared", token=DASHBOARD_TOKEN,
            payload={"primary_health_device_id": "android-upload-only"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated, changed)
        self.assertEqual(self.request(
            "POST", "/v1/settings/shared", token=DASHBOARD_TOKEN,
            payload={"primary_health_device_id": "desktop-settings-a"},
        )[0], 400)
        status, cleared = self.request(
            "POST", "/v1/settings/shared", token=DASHBOARD_TOKEN,
            payload={"primary_health_device_id": None},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(cleared["primary_health_device_id"])

    def test_read_and_write_permissions_are_separate(self):
        for token in (None, "invalid-shared-settings-token-0123456789-ABCDE"):
            status, _ = self.request("GET", "/v1/settings/shared", token=token)
            self.assertEqual(status, 401)

        self.assertEqual(self.request("GET", "/v1/settings/shared", token=READ_TOKEN)[0], 200)
        self.assertEqual(self.request("GET", "/v1/settings/shared", token=DASHBOARD_TOKEN)[0], 200)
        self.assertEqual(self.request("GET", "/v1/settings/shared", token=UPLOAD_TOKEN)[0], 200)
        status, _ = self.request(
            "PATCH", "/v1/settings/shared", token=READ_TOKEN, payload={"day_start_hour": 8},
        )
        self.assertEqual(status, 401)
        self.assertEqual(
            self.request(
                "PATCH", "/v1/settings/shared", token=UPLOAD_TOKEN, payload={"day_start_hour": 8},
            )[0],
            200,
        )
        self.assertEqual(
            self.request("PATCH", "/v1/settings/shared", payload={"day_start_hour": 8})[0],
            401,
        )

    def test_concurrent_different_updates_do_not_lose_version_increments(self):
        gate = threading.Barrier(3)
        results = []

        def update(token, hour):
            gate.wait()
            results.append(self.request(
                "PATCH", "/v1/settings/shared", token=token, payload={"day_start_hour": hour},
            ))

        workers = [
            threading.Thread(target=update, args=(DASHBOARD_TOKEN, 5)),
            threading.Thread(target=update, args=(SECOND_DASHBOARD_TOKEN, 9)),
        ]
        for worker in workers:
            worker.start()
        gate.wait()
        for worker in workers:
            worker.join(timeout=5)

        self.assertEqual(sorted(status for status, _ in results), [200, 200])
        self.assertEqual(sorted(body["settings_version"] for _, body in results), [2, 3])
        status, current = self.request("GET", "/v1/settings/shared", token=DASHBOARD_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn(current["day_start_hour"], {5, 9})
        self.assertEqual(current["settings_version"], 3)


if __name__ == "__main__":
    unittest.main()
