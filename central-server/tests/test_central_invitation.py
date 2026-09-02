import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import central_invitation
from central.config import CentralConfig
from central.http import create_server
from central.invitations import claim_invitation, create_invitation, decode_invitation
from central.storage import CentralStore, InvitationExpired
from central_endpoint import save_endpoint


BOOTSTRAP_TOKEN = "bootstrap-upload-token-0123456789-ABCDEFGHIJK"
READ_TOKEN = "central-read-token-0123456789-ABCDEFGHIJKLMN"
DEVICE_ID = "desktop-12345678-1234-5678-9234-567812345678"
OTHER_DEVICE_ID = "desktop-87654321-4321-4678-a234-567812345678"
ANDROID_DEVICE_ID = "android-install-11111111-2222-4333-8444-555555555555"


class CentralInvitationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "central.sqlite3"
        self.config_path = self.root / "config.json"
        self.endpoint_path = self.root / "public_endpoint.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": 0,
                    "database_path": str(self.database),
                    "token_bindings": {BOOTSTRAP_TOKEN: "bootstrap-device"},
                    "read_token": READ_TOKEN,
                }
            ),
            encoding="utf-8",
        )
        save_endpoint(
            self.endpoint_path,
            "custom",
            "https://central.example.test",
        )
        self.config = CentralConfig.from_environment(
            {"LIFE_RADIO_CENTRAL_CONFIG": str(self.config_path)}
        )
        self.server = create_server(self.config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def create(self, *, scope="dashboard", now=None, lifetime=timedelta(hours=24)):
        created = create_invitation(
            self.server.store,
            central_base_url="https://central.example.test",
            scope=scope,
            now=now,
            lifetime=lifetime,
        )
        return created, decode_invitation(created.code)

    def claim(
        self,
        invitation,
        *,
        device_id=DEVICE_ID,
        platform="desktop",
        display_name="Remote PC",
        token=None,
    ):
        body = {
            "schema_version": "life-radio-enrollment-claim-v1",
            "invitation_id": invitation["invitation_id"],
            "device": {
                "device_id": device_id,
                "platform": platform,
                "display_name": display_name,
            },
        }
        encoded = json.dumps(body).encode("utf-8")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(
            "POST",
            "/v1/enrollments/claim",
            body=encoded,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token or invitation['invitation_token']}",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_cli_emits_one_line_and_database_stores_only_invitation_hash(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = central_invitation.main(
                [
                    "--config",
                    str(self.config_path),
                    "--endpoint-config",
                    str(self.endpoint_path),
                    "--no-clipboard",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        code = stdout.getvalue().strip()
        self.assertEqual(stdout.getvalue(), code + "\n")
        self.assertTrue(code.startswith("LR1."))
        invitation = decode_invitation(code)
        self.assertEqual(
            set(invitation),
            {
                "v",
                "invitation_id",
                "central_base_url",
                "invitation_token",
                "scope",
                "expires_at",
            },
        )
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT token_hash, scope FROM client_invitations WHERE invitation_id = ?",
                (invitation["invitation_id"],),
            ).fetchone()
            stored_values = "|".join(
                str(value)
                for stored_row in connection.execute(
                    "SELECT * FROM client_invitations"
                ).fetchall()
                for value in stored_row
                if value is not None
            )
        finally:
            connection.close()
        self.assertEqual(len(row[0]), 64)
        self.assertEqual(row[1], "dashboard")
        self.assertNotEqual(row[0], invitation["invitation_token"])
        self.assertNotIn(invitation["invitation_token"], stored_values)
        self.assertNotIn(code, stored_values)

    def test_dashboard_claim_is_idempotent_and_permanent_without_restart(self):
        _, invitation = self.create(scope="dashboard")

        status, first = self.claim(invitation)
        status_retry, retry = self.claim(
            invitation,
            display_name="A name that must not replace the first claim",
        )

        self.assertEqual(status, 200)
        self.assertEqual(status_retry, 200)
        self.assertEqual(retry, first)
        self.assertEqual(first["schema_version"], "life-radio-client-profile-v1")
        self.assertEqual(first["central_base_url"], "https://central.example.test")
        self.assertEqual(first["device"]["device_id"], DEVICE_ID)
        self.assertEqual(first["read_token"], READ_TOKEN)
        self.assertEqual(
            self.server.store.device_for_token(first["upload_token"]), DEVICE_ID
        )

        external = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(external["token_bindings"][first["upload_token"]], DEVICE_ID)
        restarted = CentralConfig.from_environment(
            {"LIFE_RADIO_CENTRAL_CONFIG": str(self.config_path)}
        )
        restarted_store = CentralStore(
            restarted.database_path, restarted.token_bindings
        )
        self.assertEqual(restarted_store.device_for_token(first["upload_token"]), DEVICE_ID)

    def test_upload_scope_omits_read_token(self):
        _, invitation = self.create(scope="upload")

        status, profile = self.claim(invitation)

        self.assertEqual(status, 200)
        self.assertNotIn("read_token", profile)
        self.assertEqual(self.server.store.device_for_token(profile["upload_token"]), DEVICE_ID)

    def test_android_claim_uses_same_dashboard_scope_and_is_idempotent(self):
        _, invitation = self.create(scope="dashboard")

        status, first = self.claim(
            invitation,
            device_id=ANDROID_DEVICE_ID,
            platform="android",
            display_name="My phone",
        )
        retry_status, retry = self.claim(
            invitation,
            device_id=ANDROID_DEVICE_ID,
            platform="android",
            display_name="Renamed phone",
        )

        self.assertEqual(status, 200)
        self.assertEqual(retry_status, 200)
        self.assertEqual(retry, first)
        self.assertEqual(first["device"]["device_id"], ANDROID_DEVICE_ID)
        self.assertEqual(first["device"]["platform"], "android")
        self.assertEqual(first["read_token"], READ_TOKEN)
        self.assertEqual(
            self.server.store.device_for_token(first["upload_token"]),
            ANDROID_DEVICE_ID,
        )

    def test_claim_rejects_platform_mismatches_and_non_installation_ids(self):
        invalid_devices = (
            (ANDROID_DEVICE_ID, "desktop"),
            (DEVICE_ID, "android"),
            ("com.example.reader", "android"),
            ("My phone", "android"),
            ("android-install-not-a-uuid", "android"),
        )
        for device_id, platform in invalid_devices:
            with self.subTest(device_id=device_id, platform=platform):
                _, invitation = self.create(scope="upload")
                status, error = self.claim(
                    invitation,
                    device_id=device_id,
                    platform=platform,
                )
                self.assertEqual(status, 400)
                self.assertEqual(error["error"], "invalid_claim")

    def test_other_device_is_rejected_after_claim(self):
        _, invitation = self.create()
        self.assertEqual(self.claim(invitation)[0], 200)

        status, error = self.claim(invitation, device_id=OTHER_DEVICE_ID)

        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "invitation_already_claimed")
        self.assertNotIn(invitation["invitation_token"], json.dumps(error))

    def test_concurrent_different_devices_have_exactly_one_winner(self):
        _, invitation = self.create()
        gate = threading.Barrier(3)
        results = []

        def run_claim(device_id):
            gate.wait()
            results.append(self.claim(invitation, device_id=device_id))

        workers = [
            threading.Thread(target=run_claim, args=(DEVICE_ID,)),
            threading.Thread(target=run_claim, args=(OTHER_DEVICE_ID,)),
        ]
        for worker in workers:
            worker.start()
        gate.wait()
        for worker in workers:
            worker.join(timeout=5)

        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        profile = next(payload for status, payload in results if status == 200)
        conflict = next(payload for status, payload in results if status == 409)
        self.assertEqual(conflict["error"], "invitation_already_claimed")
        external = json.loads(self.config_path.read_text(encoding="utf-8"))
        claimed_bindings = {
            token: device_id
            for token, device_id in external["token_bindings"].items()
            if device_id in {DEVICE_ID, OTHER_DEVICE_ID}
        }
        self.assertEqual(claimed_bindings, {profile["upload_token"]: profile["device"]["device_id"]})

    def test_expired_and_invalid_invitations_have_distinct_safe_errors(self):
        _, expired = self.create(
            now=datetime.now(timezone.utc) - timedelta(days=2),
            lifetime=timedelta(hours=1),
        )
        status, error = self.claim(expired)
        self.assertEqual(status, 410)
        self.assertEqual(error["error"], "invitation_expired")

        _, valid = self.create()
        wrong_token = "wrong-invitation-token-0123456789-ABCDEFGHIJK"
        status, error = self.claim(valid, token=wrong_token)
        self.assertEqual(status, 401)
        self.assertEqual(error["error"], "invalid_invitation")
        self.assertNotIn(wrong_token, json.dumps(error))

    def test_idempotent_retry_is_allowed_only_before_expiration(self):
        started = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        _, invitation = self.create(now=started, lifetime=timedelta(hours=1))
        claim = {
            "invitation_id": invitation["invitation_id"],
            "device": {
                "device_id": DEVICE_ID,
                "platform": "desktop",
                "display_name": "Remote PC",
            },
        }
        first = claim_invitation(
            self.server.store,
            self.config,
            invitation_token=invitation["invitation_token"],
            claim=claim,
            now=started + timedelta(minutes=30),
        )
        self.assertEqual(first["device"]["device_id"], DEVICE_ID)

        with self.assertRaises(InvitationExpired):
            claim_invitation(
                self.server.store,
                self.config,
                invitation_token=invitation["invitation_token"],
                claim=claim,
                now=started + timedelta(hours=2),
            )

    def test_core_invitation_creator_rejects_non_https_url(self):
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            create_invitation(
                self.server.store,
                central_base_url="http://central.example.test",
            )

    def test_dashboard_claim_without_read_token_is_not_consumed(self):
        _, invitation = self.create(scope="dashboard")
        external = json.loads(self.config_path.read_text(encoding="utf-8"))
        read_token = external.pop("read_token")
        self.config_path.write_text(json.dumps(external), encoding="utf-8")

        status, error = self.claim(invitation)

        self.assertEqual(status, 503)
        self.assertEqual(error["error"], "enrollment_not_configured")
        unchanged = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn(DEVICE_ID, unchanged["token_bindings"].values())

        unchanged["read_token"] = read_token
        self.config_path.write_text(json.dumps(unchanged), encoding="utf-8")
        status, _ = self.claim(invitation)
        self.assertEqual(status, 200)

    def test_invalid_claim_body_is_rejected_before_consuming_invitation(self):
        _, invitation = self.create()
        invalid_id = str(uuid.uuid4())
        body = {
            "schema_version": "life-radio-enrollment-claim-v1",
            "invitation_id": invitation["invitation_id"],
            "device": {
                "device_id": invalid_id,
                "platform": "desktop",
                "display_name": "Invalid",
            },
        }
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(
            "POST",
            "/v1/enrollments/claim",
            body=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {invitation['invitation_token']}",
            },
        )
        response = connection.getresponse()
        error = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(error["error"], "invalid_claim")

        status, _ = self.claim(invitation)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
