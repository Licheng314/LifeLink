import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "central-server"))
sys.path.insert(0, str(PROJECT_ROOT / "pc-dashboard"))

from central.config import CentralConfig
from central.http import create_server
from central.invitations import create_invitation
from central_client import CentralClient, CentralReadClient
from central_client_setup import load_client_config, write_client_profile
from central_client_setup_server import claim_invitation, parse_invitation_code
from outbox import Outbox


class OnlineEnrollmentEndToEndTests(unittest.TestCase):
    def test_pc_claims_one_line_invitation_then_uploads_and_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device_id = f"desktop-{uuid.uuid4()}"
            identity_path = root / "identity.json"
            config_path = root / "client.json"
            read_token = "central-read-token-0123456789-ABCDEFGHIJKLMN"
            bootstrap_token = "bootstrap-device-token-0123456789-ABCDEFG"
            central_config_path = root / "central.json"
            identity_path.write_text(json.dumps({
                "version": 1,
                "device_id": device_id,
                "created_at": "2026-08-01T00:00:00Z",
            }), encoding="utf-8")
            central_config_path.write_text(json.dumps({
                "host": "127.0.0.1",
                "port": 0,
                "database_path": str(root / "central.sqlite3"),
                "token_bindings": {bootstrap_token: f"desktop-{uuid.uuid4()}"},
                "read_token": read_token,
            }), encoding="utf-8")

            central = create_server(CentralConfig(
                database_path=root / "central.sqlite3",
                host="127.0.0.1",
                port=0,
                token_bindings={bootstrap_token: f"desktop-{uuid.uuid4()}"},
                read_token=read_token,
                config_path=central_config_path,
            ))
            thread = threading.Thread(target=central.serve_forever, daemon=True)
            thread.start()
            try:
                issued = create_invitation(
                    central.store,
                    central_base_url="https://central.example.test",
                    scope="dashboard",
                )
                invitation = parse_invitation_code(issued.code)
                loopback = build_opener(ProxyHandler({}))

                def forward(request, timeout):
                    parsed = urlparse(request.full_url)
                    return loopback.open(Request(
                        f"http://127.0.0.1:{central.server_port}{parsed.path}",
                        data=request.data,
                        headers=dict(request.header_items()),
                        method=request.get_method(),
                    ), timeout=timeout)

                profile = claim_invitation(invitation, {
                    "device_id": device_id,
                    "platform": "desktop",
                    "display_name": "Remote PC",
                }, opener=forward)
                write_client_profile(
                    profile,
                    config_path=config_path,
                    identity_path=identity_path,
                )
                stored = load_client_config(
                    config_path,
                    identity_path=identity_path,
                )

                event_id = str(uuid.uuid4())
                event = {
                    "event_id": event_id,
                    "occurred_at": "2026-08-01T04:00:00Z",
                    "event_type": "app.foreground",
                    "source": {"kind": "desktop", "collector": "activitywatch", "reliability": "observed"},
                    "duration_seconds": 90,
                    "revision": 1,
                    "payload": {"app": {"package_name": "chrome.exe", "display_name": "Google Chrome"}},
                }
                with Outbox(root / "outbox.sqlite3") as outbox:
                    outbox.upsert_event(event)
                    result = CentralClient(
                        f"http://127.0.0.1:{central.server_port}",
                        stored["upload_token"],
                    ).sync_once(outbox, stored["device"])
                    state = outbox.event_status(event_id)

                usage = CentralReadClient(
                    f"http://127.0.0.1:{central.server_port}",
                    stored["read_token"],
                ).read_view(
                    "usage",
                    from_utc="2026-08-01T00:00:00Z",
                    to_utc="2026-08-02T00:00:00Z",
                    local_device_id=device_id,
                )
            finally:
                central.shutdown()
                central.server_close()
                thread.join(timeout=2)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(state["state"], "acked")
            remote = next(item for item in usage["devices"] if item["device_id"] == device_id)
            self.assertTrue(remote["is_local"])
            self.assertEqual(remote["apps"]["Google Chrome"], 90)


if __name__ == "__main__":
    unittest.main()
