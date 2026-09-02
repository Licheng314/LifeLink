import base64
import http.client
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

import central_client_setup_server as setup_server


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CENTRAL_SERVER_ROOT = PROJECT_ROOT / "central-server"
if str(CENTRAL_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(CENTRAL_SERVER_ROOT))

from central.config import CentralConfig
from central.http import create_server as create_central_server
from central.invitations import create_invitation


INVITE_TOKEN = "invite-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
UPLOAD_TOKEN = "upload-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
READ_TOKEN = "read-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-extra"


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


class CentralClientSetupServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.identity_path = self.root / "identity.json"
        self.config_path = self.root / "client" / "config.json"
        self.device_id = f"desktop-{uuid.uuid4()}"
        self.identity_path.write_text(json.dumps({
            "version": 1,
            "device_id": self.device_id,
            "created_at": "2026-08-01T00:00:00Z",
        }), encoding="utf-8")
        self.invitation_id = str(uuid.uuid4())

    def tearDown(self):
        self.directory.cleanup()

    def invitation_payload(self, **changes):
        payload = {
            "v": 1,
            "invitation_id": self.invitation_id,
            "central_base_url": "https://central.example.test",
            "invitation_token": INVITE_TOKEN,
            "scope": "dashboard",
            "expires_at": "2099-08-01T00:00:00Z",
        }
        payload.update(changes)
        return payload

    def invitation_code(self, **changes):
        raw = json.dumps(
            self.invitation_payload(**changes),
            separators=(",", ":"),
        ).encode("utf-8")
        return "LR1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def profile(self, **changes):
        profile = {
            "schema_version": "life-radio-client-profile-v1",
            "central_base_url": "https://central.example.test",
            "device": {
                "device_id": self.device_id,
                "platform": "desktop",
                "display_name": "Remote PC",
            },
            "upload_token": UPLOAD_TOKEN,
            "read_token": READ_TOKEN,
            "issued_at": "2026-08-01T00:01:00Z",
        }
        profile.update(changes)
        return profile

    def test_lr1_preview_exposes_metadata_but_not_invitation_secret(self):
        invitation = setup_server.parse_invitation_code(self.invitation_code())
        preview = setup_server.invitation_preview(invitation, {
            "device_id": self.device_id,
            "platform": "desktop",
            "display_name": "Remote PC",
        })

        self.assertEqual(invitation.invitation_id, self.invitation_id)
        self.assertNotIn(INVITE_TOKEN, repr(invitation))
        self.assertEqual(preview["central_base_url"], "https://central.example.test")
        self.assertEqual(preview["scope"], "dashboard")
        self.assertNotIn(INVITE_TOKEN, json.dumps(preview))

    def test_claim_uses_bearer_and_exact_frozen_body(self):
        invitation = setup_server.parse_invitation_code(self.invitation_code())
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse(self.profile())

        profile = setup_server.claim_invitation(
            invitation,
            self.profile()["device"],
            opener=opener,
        )

        request = captured["request"]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://central.example.test/v1/enrollments/claim")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {INVITE_TOKEN}")
        self.assertEqual(body, {
            "schema_version": "life-radio-enrollment-claim-v1",
            "invitation_id": self.invitation_id,
            "device": self.profile()["device"],
        })
        self.assertNotIn(INVITE_TOKEN, json.dumps(body))
        self.assertEqual(profile["upload_token"], UPLOAD_TOKEN)

    def test_returned_profile_must_match_local_identity_and_scope(self):
        invitation = setup_server.parse_invitation_code(self.invitation_code())
        wrong = self.profile()
        wrong["device"]["device_id"] = f"desktop-{uuid.uuid4()}"
        with self.assertRaisesRegex(setup_server.EnrollmentClaimError, "校验失败"):
            setup_server.claim_invitation(
                invitation,
                self.profile()["device"],
                opener=lambda request, timeout: FakeResponse(wrong),
            )

        upload_invitation = setup_server.parse_invitation_code(
            self.invitation_code(scope="upload"),
        )
        with self.assertRaisesRegex(setup_server.EnrollmentClaimError, "超出"):
            setup_server.claim_invitation(
                upload_invitation,
                self.profile()["device"],
                opener=lambda request, timeout: FakeResponse(self.profile()),
            )

    def request_json(self, server, path, payload):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=3,
        )
        body = json.dumps(payload).encode("utf-8")
        connection.request(
            "POST", path, body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_loopback_setup_preview_claim_persists_and_stops(self):
        server = setup_server.create_setup_server(
            config_path=self.config_path,
            identity_path=self.identity_path,
            port=0,
        )
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            preview_status, preview = self.request_json(
                server, "/api/setup/preview",
                {"invite_code": self.invitation_code()},
            )
            self.assertEqual(preview_status, 200)
            self.assertNotIn(INVITE_TOKEN, json.dumps(preview))
            with mock.patch.object(
                setup_server, "claim_invitation", return_value=self.profile(),
            ):
                claim_status, claimed = self.request_json(
                    server, "/api/setup/claim",
                    {"preview_id": preview["preview_id"]},
                )
            self.assertEqual(claim_status, 200)
            self.assertEqual(claimed["status"], "configured")
            self.assertEqual(claimed["message"], "配置成功，正在启动 Life Link。")
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        finally:
            server.shutdown()
            server.server_close()
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["device"]["device_id"], self.device_id)
        self.assertEqual(stored["schema_version"], "life-radio-client-config-v1")

    def test_setup_html_uses_post_and_no_url_or_browser_storage_for_secret(self):
        source = setup_server.SETUP_HTML.read_text(encoding="utf-8")
        self.assertIn("Life Link 中央客户端设置", source)
        self.assertIn("连接 Life Link 中央服务", source)
        self.assertIn("/api/setup/preview", source)
        self.assertIn("/api/setup/claim", source)
        self.assertIn("method: 'POST'", source)
        self.assertIn("inviteInput.value = ''", source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn("URLSearchParams", source)
        self.assertNotIn("location.search", source)

    def test_real_central_claim_returns_identity_bound_profile(self):
        central_config_path = self.root / "central" / "config.json"
        central_database = self.root / "central" / "life_radio.sqlite3"
        read_token = "central-read-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        existing_upload = "existing-upload-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        central_config_path.parent.mkdir(parents=True, exist_ok=True)
        central_config_path.write_text(json.dumps({
            "host": "127.0.0.1",
            "port": 0,
            "database_path": str(central_database),
            "token_bindings": {
                existing_upload: f"desktop-{uuid.uuid4()}",
            },
            "read_token": read_token,
        }), encoding="utf-8")
        config = CentralConfig(
            database_path=central_database,
            host="127.0.0.1",
            port=0,
            token_bindings={existing_upload: f"desktop-{uuid.uuid4()}"},
            read_token=read_token,
            config_path=central_config_path,
        )
        central = create_central_server(config, ("127.0.0.1", 0))
        thread = threading.Thread(target=central.serve_forever, daemon=True)
        thread.start()
        try:
            issued = create_invitation(
                central.store,
                central_base_url="https://central.example.test",
                scope="dashboard",
            )
            invitation = setup_server.parse_invitation_code(
                issued.code,
            )
            device = {
                "device_id": self.device_id,
                "platform": "desktop",
                "display_name": "Remote PC",
            }
            loopback_opener = build_opener(ProxyHandler({}))

            def forward_https_to_test_server(request, timeout):
                parsed = urlparse(request.full_url)
                forwarded = Request(
                    f"http://127.0.0.1:{central.server_port}{parsed.path}",
                    data=request.data,
                    headers=dict(request.header_items()),
                    method=request.get_method(),
                )
                return loopback_opener.open(forwarded, timeout=timeout)

            profile = setup_server.claim_invitation(
                invitation, device, opener=forward_https_to_test_server,
            )
        finally:
            central.shutdown()
            central.server_close()
            thread.join(timeout=2)

        self.assertEqual(profile["device"]["device_id"], self.device_id)
        self.assertEqual(profile["central_base_url"], invitation.central_base_url)
        self.assertTrue(profile["upload_token"])
        self.assertEqual(profile["read_token"], read_token)


if __name__ == "__main__":
    unittest.main()
