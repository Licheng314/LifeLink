import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


MODULE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = MODULE_ROOT / "life_link_mcp.py"
SPEC = importlib.util.spec_from_file_location("life_link_mcp", MODULE_PATH)
life_link_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(life_link_mcp)


class LifeLinkMCPTests(unittest.TestCase):
    def test_default_state_uses_unique_user_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"LIFE_LINK_DATA_ROOT": directory}, clear=False):
                self.assertEqual(
                    life_link_mcp.default_state_dir(),
                    Path(directory) / "ai" / "mcp",
                )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package_dir = self.root / "package"
        self.state_dir = self.root / "state"
        self.package_dir.mkdir()
        self.profile_id = str(uuid.uuid4())
        (self.package_dir / "manifest.json").write_text(json.dumps({
            "schema_version": life_link_mcp.PACKAGE_SCHEMA,
            "mcp_profile_id": self.profile_id,
        }), encoding="utf-8")
        self.pairing = {
            "central_instance_id": "central-test",
            "claim_url": "http://127.0.0.1:8091/v1/ai-readers/pairings/claim",
            "pairing_id": str(uuid.uuid4()),
            "pairing_token": "one-time-secret",
            "claim_request_body_template": {
                "schema_version": "life-radio-ai-reader-pairing-claim-v1",
            },
        }
        (self.package_dir / "pairing.json").write_text(
            json.dumps(self.pairing), encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def make_reader(self, responses):
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return responses.pop(0)

        package = life_link_mcp.ConnectionPackage(self.package_dir)
        store = life_link_mcp.StateStore(self.state_dir, self.profile_id)
        return life_link_mcp.LifeLinkReader(package, store, http_json=fake_http), calls

    def test_first_read_claims_then_saves_cursor_without_plaintext_secrets(self):
        profile = {
            "access_token": "long-secret-token",
            "reader_id": str(uuid.uuid4()),
            "expires_at": "2099-01-01T00:00:00Z",
            "context_url": "http://127.0.0.1:8091/v1/read/ai/context",
        }
        context = {
            "background": ["背景"], "current": [], "events": [],
            "understanding": {"version": "sha256:test", "unchanged": False},
            "next_cursor": "opaque-secret-cursor",
        }
        reader, calls = self.make_reader([(200, profile), (200, context)])

        returned = reader.read_context({"name": "OpenClaw", "version": "1"})

        self.assertEqual(returned, context)
        self.assertEqual([item[0] for item in calls], ["POST", "GET"])
        self.assertEqual(calls[0][2]["body"]["reader"]["type"], "mcp.openclaw")
        self.assertFalse((self.package_dir / "pairing.json").exists())
        raw_state = next((self.state_dir / "profiles").glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("long-secret-token", raw_state)
        self.assertNotIn("opaque-secret-cursor", raw_state)
        self.assertEqual(reader.status()["connected"], True)

    def test_superseded_cursor_is_cleared_and_retried_once(self):
        reader, _ = self.make_reader([])
        secret = {
            "access_token": "token", "reader_id": str(uuid.uuid4()),
            "expires_at": "2099-01-01T00:00:00Z",
            "context_url": "http://127.0.0.1:8091/v1/read/ai/context",
            "next_cursor": "old-cursor", "understanding_version": "old-version",
        }
        reader.state_store.save(
            central_instance_id="central-test",
            reader={"type": "mcp.test", "instance_id": f"mcp:{self.profile_id}", "display_name": "Test"},
            secret=secret,
        )
        calls = []

        def fake_http(method, url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return 409, {"error": "cursor_superseded"}
            return 200, {
                "events": [], "understanding": {"version": "new-version", "unchanged": False},
                "next_cursor": "new-cursor",
            }

        reader.http_json = fake_http
        reader.read_context({"name": "test"})
        self.assertIn("cursor=old-cursor", calls[0])
        self.assertNotIn("cursor=", calls[1])
        self.assertEqual(reader.state_store.load()["secret"]["next_cursor"], "new-cursor")

    def test_update_check_uses_saved_cursor_without_changing_state(self):
        reader, calls = self.make_reader([(200, {"update_mcp": True})])
        secret = {
            "access_token": "token", "reader_id": str(uuid.uuid4()),
            "expires_at": "2099-01-01T00:00:00Z",
            "context_url": "http://127.0.0.1:8091/v1/read/ai/context",
            "next_cursor": "saved-cursor", "understanding_version": "version",
        }
        reader.state_store.save(
            central_instance_id="central-test",
            reader={"type": "mcp.test", "instance_id": f"mcp:{self.profile_id}", "display_name": "Test"},
            secret=secret,
        )
        before = reader.state_store.load()
        self.assertEqual(reader.check_updates(), {"connected": True, "update_mcp": True})
        self.assertIn("/v1/read/ai/updates?cursor=saved-cursor", calls[0][1])
        self.assertEqual(reader.state_store.load(), before)

    def test_reader_file_can_supply_generic_process_binding_without_app_detection(self):
        reader_document = {
            "schema_version": "life-link-mcp-reader/v1",
            "reader": {
                "type": "personal-agent",
                "instance_id": "stable-openclaw-reader",
                "display_name": "Talo (OpenClaw)",
                "process_binding": {
                    "strategy": "hosted-argument",
                    "display_name": "OpenClaw",
                    "process_name": "node.exe",
                    "argument_path_segments": ["node_modules", "openclaw"],
                },
            },
        }
        (self.package_dir / "reader.json").write_text(
            json.dumps(reader_document), encoding="utf-8",
        )
        package = life_link_mcp.ConnectionPackage(self.package_dir)

        identity = package.reader_identity({"name": "unrelated-host-name"})

        self.assertEqual(identity, reader_document["reader"])

    def test_stdio_protocol_has_only_json_rpc_on_stdout(self):
        messages = "\n".join(json.dumps(item) for item in (
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "lifelink_connection_status", "arguments": {}},
            },
        )) + "\n"
        process = subprocess.run(
            [
                sys.executable, str(MODULE_PATH), "serve",
                "--package-dir", str(self.package_dir),
                "--state-dir", str(self.state_dir),
            ],
            input=messages, text=True, encoding="utf-8", capture_output=True, timeout=5,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        tool_names = [tool["name"] for tool in responses[1]["result"]["tools"]]
        self.assertEqual(tool_names, ["lifelink_connection_status", "lifelink_check_updates", "lifelink_read_context"])
        self.assertFalse(responses[2]["result"]["structuredContent"]["connected"])

    def test_built_windows_executable_serves_the_same_stdio_protocol(self):
        executable = MODULE_ROOT / "dist" / "life-link-mcp.exe"
        if not executable.is_file():
            self.skipTest("Windows executable has not been built")
        messages = "\n".join(json.dumps(item) for item in (
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "exe-test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )) + "\n"
        process = subprocess.run(
            [
                str(executable), "serve", "--package-dir", str(self.package_dir),
                "--state-dir", str(self.state_dir),
            ],
            input=messages, text=True, encoding="utf-8", capture_output=True, timeout=15,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "life-link-mcp")
        self.assertEqual(len(responses[1]["result"]["tools"]), 3)


if __name__ == "__main__":
    unittest.main()
