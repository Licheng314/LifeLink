import http.client
import hashlib
import importlib.util
import json
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = PROJECT_ROOT / "pc-dashboard" / "sync_server.py"
SPEC = importlib.util.spec_from_file_location("life_radio_sync_server", SERVER_PATH)
sync_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_server)


def shared_settings(day_start_hour=4, version=3, updated_at="2026-08-09T01:02:03Z"):
    return {
        "timezone": "Asia/Shanghai", "day_start_hour": day_start_hour,
        "primary_health_device_id": None, "sleep_local_time": "23:00", "ai_display_name": "AI",
        "morning_report": {"enabled": False, "mode": "after_first_usage", "delay_minutes": 60, "local_time": None},
        "evening_report": {"enabled": False, "local_time": "23:00"},
        "periodic_summary": {"enabled": False, "start_local_time": "10:00", "end_local_time": "22:00", "interval_minutes": 120},
        "settings_version": version, "updated_at": updated_at,
    }


class SyncServerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_data_dir = sync_server.DATA_DIR
        self.previous_identity_path = sync_server.CENTRAL_IDENTITY_PATH
        self.previous_outbox_path = sync_server.CENTRAL_OUTBOX_PATH
        self.previous_display_date_today = sync_server.display_date_today
        sync_server.DATA_DIR = Path(self.temp_dir.name) / "data"
        sync_server.CENTRAL_IDENTITY_PATH = Path(self.temp_dir.name) / "identity.json"
        sync_server.CENTRAL_OUTBOX_PATH = Path(self.temp_dir.name) / "outbox.sqlite3"
        self.server = sync_server.ThreadedHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        sync_server.close_central_outbox()
        sync_server.DATA_DIR = self.previous_data_dir
        sync_server.CENTRAL_IDENTITY_PATH = self.previous_identity_path
        sync_server.CENTRAL_OUTBOX_PATH = self.previous_outbox_path
        sync_server.display_date_today = self.previous_display_date_today
        self.temp_dir.cleanup()

    def request(self, method, path, payload=None, headers=None):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        return response.status, json.loads(response_body.decode("utf-8"))

    def request_text(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        return response.status, body

    def test_calendar_days_proxies_the_selected_inclusive_range(self):
        payload = {
            "timezone": "Asia/Shanghai", "day_start_hour": 4,
            "today_business_date": "2026-08-29",
            "earliest_available_date": "2026-08-01",
            "latest_available_date": "2026-08-29", "days": [],
        }
        with patch.object(sync_server, "read_central_calendar_days", return_value=payload) as read:
            status, body = self.request(
                "GET", "/api/calendar-days?from=2026-08-24&to=2026-08-29",
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        read.assert_called_once_with("2026-08-24", "2026-08-29")

    def test_retired_pc_dashboard_is_not_served_from_the_sync_service(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 410)
        self.assertEqual(body["error"], "pc_dashboard_retired")
        status, _ = self.request_text("/assets/scripts/app.js")
        self.assertEqual(status, 404)

    def test_calendar_days_rejects_missing_or_oversized_ranges(self):
        status, body = self.request("GET", "/api/calendar-days?from=2026-08-01")
        self.assertEqual(status, 400)
        self.assertIn("from and to", body["error"])
        with patch.object(sync_server, "read_central_calendar_days") as read:
            status, body = self.request(
                "GET", "/api/calendar-days?from=2026-08-01&to=2026-09-12",
            )
        self.assertEqual(status, 400)
        self.assertIn("42", body["error"])
        read.assert_not_called()

    def test_locations_uses_selected_business_date(self):
        payload = {"segments": [], "observations": []}
        with patch.object(sync_server, "read_central_locations", return_value=payload) as read:
            status, body = self.request("GET", "/api/locations?date=2026-08-17")
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        read.assert_called_once_with("2026-08-17")

    def test_local_device_status_uses_central_custom_name(self):
        local = {
            "device_id": "desktop-local",
            "device_key": "desktop-local-key",
            "platform": "desktop",
            "display_name": "BF-202606291510",
        }
        central = {
            "online_window_seconds": 600,
            "devices": [{
                "device_id": "desktop-local",
                "platform": "desktop",
                "display_name": "工作电脑BF",
                "status": "connected",
                "last_seen_at": "2026-08-28T02:05:10Z",
                "window": {"event_count": 1, "batch_count": 1, "categories": {}},
            }],
        }
        with (
            patch.object(sync_server, "local_desktop_device_descriptor", return_value=local),
            patch.object(sync_server, "read_central_view", return_value=central),
            patch.object(sync_server, "get_hostname", return_value="BF-202606291510"),
        ):
            payload = sync_server.get_central_device_status_payload("2026-08-28")

        self.assertEqual(payload["local"]["display_name"], "工作电脑BF")
        self.assertEqual(payload["local"]["hostname"], "BF-202606291510")
        self.assertEqual(payload["devices"], [])

    def test_timeline_auth_rejection_never_uses_persisted_fallback(self):
        path = "/v1/timeline-events?from=a&to=b"
        payload = {"events": [], "window": {"from": "a", "to": "b"}}
        sync_server._V17_READ_MEMORY.clear()
        sync_server._V17_DISK_CACHE_KEY = None
        sync_server._V17_DISK_CACHE = {}
        sync_server._save_v17_read_entry(path, payload)
        with patch.object(
            sync_server, "_central_read_json",
            side_effect=sync_server.CentralReadError(
                "central_rejected", "HTTP 401", http_status=401,
            ),
        ):
            status, body = self.request("GET", f"/api/timeline-events?from=a&to=b")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "central_rejected")

    def test_startup_cleanup_removes_only_v17_atomic_temporary_files(self):
        sync_server.DATA_DIR.mkdir(parents=True, exist_ok=True)
        orphan = sync_server.DATA_DIR / ".v17_read_cache.json.abcd.tmp"
        unrelated = sync_server.DATA_DIR / ".health_info_cache.json.abcd.tmp"
        orphan.write_bytes(b"old")
        unrelated.write_bytes(b"keep")

        result = sync_server.cleanup_orphan_v17_cache_temporary_files()

        self.assertEqual(result, {"removed": 1, "bytes_removed": 3})
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())

    def test_history_prune_retires_any_acked_active_aw_mirror_only(self):
        active = {
            "device_key": "desktop-active", "device_id": "desktop-active-id",
            "platform": "desktop", "display_name": "Active",
        }
        other = {
            "device_key": "desktop-other", "device_id": "desktop-other-id",
            "platform": "desktop", "display_name": "Other",
        }
        acked_id, pending_id, recent_id, other_id, custom_id = [str(uuid.uuid4()) for _ in range(5)]

        def write(device, day, event_id, event_type="app.foreground"):
            path = sync_server.get_device_data_file(device["device_key"], event_type, day)
            sync_server.atomic_write_json(path, {
                "device": device, "event_type": event_type,
                "batches": [{"events": [{"event_id": event_id}]}],
            })
            return path

        acked = write(active, "2026-08-18", acked_id)
        pending = write(active, "2026-08-19", pending_id)
        recent = write(active, "2026-08-20", recent_id)
        another = write(other, "2026-08-18", other_id)
        custom = write(active, "2026-08-18", custom_id, "custom.event")

        class FakeOutbox:
            @staticmethod
            def all_events_acked(event_ids):
                return set(event_ids) in ({acked_id}, {recent_id}, {custom_id})

        sync_server.seen_event_ids_cache = ("test", {acked_id})
        with patch.object(sync_server, "local_desktop_device_descriptor", return_value=active), patch.object(sync_server, "get_central_outbox", return_value=FakeOutbox()):
            result = sync_server.prune_acked_local_device_history(
                now=sync_server.parse_utc_datetime("2026-08-21T12:00:00Z"),
            )

        self.assertEqual(result["removed_files"], 2)
        self.assertFalse(acked.exists())
        self.assertTrue(pending.exists())
        self.assertFalse(recent.exists())
        self.assertTrue(another.exists())
        self.assertTrue(custom.exists())
        self.assertIsNone(sync_server.seen_event_ids_cache)

    def test_compacted_aw_fingerprint_preserves_next_revision(self):
        item = {
            "event_id": str(uuid.uuid4()),
            "occurred_at": "2026-08-21T01:00:00Z",
            "event_type": "app.foreground",
            "source": {"kind": "desktop", "collector": "activitywatch"},
            "duration_seconds": 10,
            "payload": {"activitywatch": {"kind": "window", "event_id": 7}},
        }
        outbox = sync_server.get_central_outbox()
        first = sync_server.central_wire_event(item, outbox)
        outbox.upsert_event(first)
        batch = outbox.prepare_batch(sync_server.local_desktop_device_descriptor())
        outbox.acknowledge(
            batch["batch_id"], {"confirmed_event_ids": [item["event_id"]]},
        )
        outbox.compact_confirmed(
            event_types={"app.foreground"},
            completed_before=sync_server.utc_now() + sync_server.timedelta(days=1),
        )

        updated = {**item, "duration_seconds": 20}
        second = sync_server.central_wire_event(updated, outbox)

        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)

    def test_native_and_browser_facts_are_trusted_local_mutable_events(self):
        outbox = sync_server.get_central_outbox()
        native = {
            "event_id": str(uuid.uuid4()), "occurred_at": "2026-08-21T01:00:00Z",
            "event_type": "app.foreground",
            "source": {"kind": "desktop", "collector": "windows_native", "reliability": "observed"},
            "duration_seconds": 10,
            "payload": {"app": {"display_name": "Code.exe", "process_name": "Code.exe"}},
        }
        web = {
            "event_id": str(uuid.uuid4()), "occurred_at": "2026-08-21T01:00:00Z",
            "event_type": "web.foreground",
            "source": {"kind": "desktop", "collector": "browser_extension", "reliability": "observed"},
            "duration_seconds": 10, "payload": {"domain": "example.com"},
        }
        for item in (native, web):
            self.assertTrue(sync_server.is_central_local_event(item))
            first = sync_server.central_wire_event(item, outbox)
            outbox.upsert_event(first)
            second = sync_server.central_wire_event({**item, "duration_seconds": 20}, outbox)
            self.assertEqual(first["revision"], 1)
            self.assertEqual(second["revision"], 2)

    def test_wish_complete_accepts_an_empty_browser_post(self):
        wish_id = "00000000-0000-4000-8000-000000000001"
        archived = {"wish_id": wish_id, "status": "archived"}
        with patch.object(sync_server, "_central_write_json", return_value=(archived, 200)) as request:
            status, body = self.request("POST", f"/api/wishes/{wish_id}/complete")
        self.assertEqual(status, 200)
        self.assertEqual(body, archived)
        request.assert_called_once_with("POST", f"/v1/wishes/{wish_id}/complete", None)

    def test_retired_dashboard_assets_are_not_served_by_the_sync_service(self):
        for path in (
            "/assets/styles/base.css",
            "/assets/scripts/health-info.js",
            "/assets/images/life-link-logo.png",
            "/assets/scripts/../sync_server.py",
            "/assets/scripts/not-registered.js",
        ):
            status, _ = self.request_text(path)
            self.assertEqual(status, 404)

    def test_blacklist_rule_proxy_rejects_extra_path_segments_without_forwarding(self):
        original_request = sync_server.central_media_request
        calls = []
        try:
            sync_server.central_media_request = lambda *args, **kwargs: calls.append((args, kwargs))
            malformed = "/api/blacklist/rules/unrelated/real-rule-id"
            status, _ = self.request("PATCH", malformed, {"label": "wrong"})
            self.assertEqual(status, 404)
            status, _ = self.request("DELETE", malformed)
            self.assertEqual(status, 404)
            self.assertEqual(calls, [])
        finally:
            sync_server.central_media_request = original_request

    def test_ai_reader_management_uses_local_registered_device_proxy(self):
        original_request = sync_server.central_media_request
        calls = []
        reader_id = "00000000-0000-4000-8000-000000000004"
        try:
            def fake_request(method, path, **kwargs):
                calls.append((method, path, kwargs))
                if path == "/v1/ai-readers":
                    return 200, {"central_instance_id": "central-test", "readers": []}
                if path.endswith("/process-status"):
                    return 200, {"reader_id": reader_id, "process_running": True, "process_display_name": "OpenClaw"}
                if path.endswith("/access-logs?limit=10"):
                    return 200, {"reader_id": reader_id, "logs": []}
                if path.endswith("/context-preview?view=compact"):
                    return 200, {"preview_only": True, "context": {"events": []}}
                if path == "/v1/ai-readers/pairings":
                    return 201, {"pairing_text": "secret", "expires_at": "2026-08-20T00:00:00Z", "central_instance_id": "central-test"}
                return 200, {"reader": {"reader_id": reader_id}}

            sync_server.central_media_request = fake_request
            self.assertEqual(self.request("GET", "/api/ai-readers")[0], 200)
            self.assertEqual(self.request("GET", f"/api/ai-readers/{reader_id}/process-status")[0], 200)
            self.assertEqual(self.request("GET", f"/api/ai-readers/{reader_id}/access-logs?limit=10")[0], 200)
            self.assertEqual(self.request("GET", f"/api/ai-readers/{reader_id}/context-preview")[0], 200)
            self.assertEqual(self.request("POST", "/api/ai-readers/pairings", {})[0], 201)
            self.assertEqual(self.request("POST", f"/api/ai-readers/{reader_id}/clear-reading-progress", {})[0], 200)
            self.assertEqual([call[:2] for call in calls], [
                ("GET", "/v1/ai-readers"),
                ("GET", f"/v1/ai-readers/{reader_id}/process-status"),
                ("GET", f"/v1/ai-readers/{reader_id}/access-logs?limit=10"),
                ("GET", f"/v1/ai-readers/{reader_id}/context-preview?view=compact"),
                ("POST", "/v1/ai-readers/pairings"),
                ("POST", f"/v1/ai-readers/{reader_id}/clear-reading-progress"),
            ])
            self.assertTrue(all(call[2].get("use_read") is False for call in calls))
        finally:
            sync_server.central_media_request = original_request

    def test_ai_reader_skill_button_opens_only_an_export_copy(self):
        exported = Path(self.temp_dir.name) / "exports" / "life-link-ai-reader" / "SKILL.md"
        with patch.object(sync_server, "export_ai_reader_skill", return_value=exported) as export:
            status, body = self.request("POST", "/api/ai-reader-skill/open")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"path": str(exported)})
        export.assert_called_once_with(open_location=True)

    def test_ai_reader_skill_export_copies_without_modifying_source(self):
        source = Path(self.temp_dir.name) / "repo-skill" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("skill-copy-test", encoding="utf-8")
        previous_source = sync_server.AI_READER_SKILL_SOURCE
        try:
            sync_server.AI_READER_SKILL_SOURCE = source
            exported = sync_server.export_ai_reader_skill(open_location=False)
        finally:
            sync_server.AI_READER_SKILL_SOURCE = previous_source
        self.assertNotEqual(exported, source)
        self.assertEqual(exported.read_text(encoding="utf-8"), "skill-copy-test")
        self.assertEqual(source.read_text(encoding="utf-8"), "skill-copy-test")

    def test_ai_reader_connection_package_contains_skill_pairing_and_instructions(self):
        source = Path(self.temp_dir.name) / "repo-skill" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("# test reader skill", encoding="utf-8")
        executable = Path(self.temp_dir.name) / "life-link-mcp.exe"
        executable.write_bytes(b"real-test-executable")
        pairing = {
            "schema_version": "life-link-ai-reader-pairing/v1",
            "pairing_id": "pairing-test",
            "pairing_token": "short-lived-secret",
            "claim_url": "http://127.0.0.1:8091/v1/ai-readers/pairings/claim",
        }
        payload = {
            "pairing_text": json.dumps(pairing),
            "expires_at": "2099-08-23T00:00:00Z",
            "central_instance_id": "central-test",
        }
        previous_source = sync_server.AI_READER_SKILL_SOURCE
        previous_executable = sync_server.AI_READER_MCP_EXECUTABLE
        try:
            sync_server.AI_READER_SKILL_SOURCE = source
            sync_server.AI_READER_MCP_EXECUTABLE = executable
            exported = sync_server.create_ai_reader_connection_bundle(
                payload, open_location=False,
            )
        finally:
            sync_server.AI_READER_SKILL_SOURCE = previous_source
            sync_server.AI_READER_MCP_EXECUTABLE = previous_executable

        self.assertTrue(exported.is_file())
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(set(archive.namelist()), {
                "README.md", "manifest.json", "pairing.json",
                "mcp-config.example.json", "reader.json", "life-link-ai-reader/SKILL.md",
                "life-link-mcp.exe",
            })
            self.assertEqual(
                archive.read("life-link-ai-reader/SKILL.md").decode("utf-8"),
                "# test reader skill",
            )
            packaged_pairing = json.loads(archive.read("pairing.json"))
            manifest = json.loads(archive.read("manifest.json"))
            readme = archive.read("README.md").decode("utf-8")
            executable_bytes = archive.read("life-link-mcp.exe")
        self.assertEqual(packaged_pairing, pairing)
        self.assertEqual(manifest["transport"], "stdio")
        self.assertEqual(executable_bytes, b"real-test-executable")
        self.assertEqual(
            manifest["executable_sha256"], hashlib.sha256(executable_bytes).hexdigest(),
        )
        self.assertNotIn("short-lived-secret", json.dumps(manifest))
        self.assertIn("包含真实的 Windows MCP stdio 可执行程序", readme)
        self.assertIn("检查当前真实运行进程的命令行", readme)
        self.assertIn("node.exe` 加 `node_modules/openclaw", readme)
        self.assertIn("首次读取后再修改此文件不会更新", readme)
        self.assertIn("不能只笼统回复“全部 OK”", readme)
        self.assertIn("最终以 Life Link WebUI 是否出现绿色进程提示为准", readme)

    def test_ai_reader_connection_package_replaces_managed_old_exports_only_after_success(self):
        source = Path(self.temp_dir.name) / "repo-skill" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("# test reader skill", encoding="utf-8")
        executable = Path(self.temp_dir.name) / "life-link-mcp.exe"
        executable.write_bytes(b"real-test-executable")
        export_dir = sync_server.DATA_DIR.parent / "exports" / "ai-connections"
        export_dir.mkdir(parents=True)
        legacy = export_dir / "LifeLink-AI-Connection-old.zip"
        previous_mcp = export_dir / "LifeLink-AI-MCP-Connection-old.zip"
        unrelated = export_dir / "keep-me.zip"
        legacy.write_bytes(b"old")
        previous_mcp.write_bytes(b"old")
        unrelated.write_bytes(b"keep")
        payload = {
            "pairing_text": json.dumps({"pairing_id": "pairing-test"}),
            "expires_at": "2099-08-23T00:00:00Z",
            "central_instance_id": "central-test",
        }

        with patch.object(sync_server, "AI_READER_SKILL_SOURCE", source), patch.object(
            sync_server, "AI_READER_MCP_EXECUTABLE", executable,
        ):
            exported = sync_server.create_ai_reader_connection_bundle(
                payload, open_location=False,
            )

        self.assertTrue(exported.is_file())
        self.assertFalse(legacy.exists())
        self.assertFalse(previous_mcp.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(
            list(export_dir.glob("LifeLink-AI-MCP-Connection-*.zip")), [exported],
        )

    def test_ai_reader_connection_package_failure_preserves_previous_export(self):
        export_dir = sync_server.DATA_DIR.parent / "exports" / "ai-connections"
        export_dir.mkdir(parents=True)
        previous_mcp = export_dir / "LifeLink-AI-MCP-Connection-previous.zip"
        previous_mcp.write_bytes(b"keep-on-failure")
        missing_executable = Path(self.temp_dir.name) / "missing-life-link-mcp.exe"

        with patch.object(
            sync_server, "AI_READER_MCP_EXECUTABLE", missing_executable,
        ), self.assertRaises(FileNotFoundError):
            sync_server.create_ai_reader_connection_bundle({}, open_location=False)

        self.assertEqual(previous_mcp.read_bytes(), b"keep-on-failure")

    def test_ai_reader_connection_package_endpoint_creates_pairing_then_opens_zip(self):
        payload = {
            "pairing_text": json.dumps({"pairing_token": "secret"}),
            "expires_at": "2099-08-23T00:00:00Z",
            "central_instance_id": "central-test",
        }
        exported = Path(self.temp_dir.name) / "LifeLink-AI-Connection-test.zip"
        executable = Path(self.temp_dir.name) / "life-link-mcp.exe"
        executable.write_bytes(b"test")
        with patch.object(
            sync_server, "AI_READER_MCP_EXECUTABLE", executable,
        ), patch.object(
            sync_server, "central_media_request", return_value=(201, payload),
        ) as central_request, patch.object(
            sync_server, "create_ai_reader_connection_bundle", return_value=exported,
        ) as create_bundle:
            status, body = self.request(
                "POST", "/api/ai-reader-connection-package/open",
            )

        self.assertEqual(status, 201)
        self.assertEqual(body, {
            "filename": exported.name,
            "path": str(exported),
            "expires_at": payload["expires_at"],
        })
        central_request.assert_called_once_with(
            "POST", "/v1/ai-readers/pairings", body={}, use_read=False,
        )
        create_bundle.assert_called_once_with(payload, open_location=True)

    def test_built_mcp_executable_can_be_packed_when_available(self):
        executable = sync_server.AI_READER_MCP_EXECUTABLE
        if not executable.is_file():
            self.skipTest("life-link-mcp.exe has not been built")
        payload = {
            "pairing_text": json.dumps({
                "central_instance_id": "central-test",
                "claim_url": "http://127.0.0.1:8091/v1/ai-readers/pairings/claim",
                "pairing_id": str(uuid.uuid4()),
                "pairing_token": "short-lived-test-secret",
            }),
            "expires_at": "2099-08-23T00:00:00Z",
            "central_instance_id": "central-test",
        }

        exported = sync_server.create_ai_reader_connection_bundle(
            payload, open_location=False,
        )

        with zipfile.ZipFile(exported) as archive:
            packaged_executable = archive.read("life-link-mcp.exe")
            manifest = json.loads(archive.read("manifest.json"))
        self.assertTrue(packaged_executable.startswith(b"MZ"))
        self.assertGreater(len(packaged_executable), 1_000_000)
        self.assertEqual(
            manifest["executable_sha256"],
            hashlib.sha256(packaged_executable).hexdigest(),
        )

    def test_usage_summary_excludes_location_segment_durations(self):
        device = sync_server.v1_device_descriptor({
            "device_id": "android-usage-location-isolation", "platform": "android", "display_name": "Test phone",
        })
        events = [
            {
                "event_id": str(uuid.uuid4()), "occurred_at": "2026-07-18T01:00:00Z",
                "event_type": "app.foreground", "duration_seconds": 300,
                "payload": {"app": {"display_name": "Reader", "package_name": "example.reader"}},
            },
            {
                "event_id": str(uuid.uuid4()), "occurred_at": "2026-07-18T01:05:00Z",
                "event_type": "location.stay", "duration_seconds": 36_000,
                "payload": {"legacy_data": {"kind": "stay", "latitude": 31.23, "longitude": 121.47}},
            },
        ]
        sync_server.write_device_events(
            events, device=device,
            batch_metadata={"received_at": "2026-07-18T11:05:00Z", "batch_id": "usage-location-isolation"},
        )

        usage = sync_server.get_usage_summary("2026-07-18")
        phone = next(item for item in usage["devices"] if item["device_key"] == device["device_key"])
        self.assertEqual(phone["events"], 1)
        self.assertEqual(phone["apps"], {"Reader": 300})
        self.assertEqual(phone["hourly_online"], {"9": 300})

    def test_usage_summary_trims_desktop_afk_but_keeps_mobile_and_sites(self):
        desktop = sync_server.v1_device_descriptor({
            "device_id": "desktop-afk-trim", "platform": "desktop", "display_name": "Test PC",
        })
        phone = sync_server.v1_device_descriptor({
            "device_id": "android-afk-unchanged", "platform": "android", "display_name": "Test phone",
        })
        desktop_events = [
            {
                "event_id": str(uuid.uuid4()), "occurred_at": "2026-07-18T01:00:00Z",
                "event_type": "app.foreground", "duration_seconds": 600,
                "payload": {
                    "app": {"display_name": "Google Chrome", "package_name": "chrome.exe"},
                    "activitywatch": {"kind": "window", "bucket_id": "aw-watcher-window", "data": {"app": "chrome.exe"}},
                },
            },
            {
                "event_id": str(uuid.uuid4()), "occurred_at": "2026-07-18T01:00:00Z",
                "event_type": "app.foreground", "duration_seconds": 0,
                "payload": {
                    "activitywatch": {"kind": "web", "bucket_id": "aw-watcher-web-chrome", "data": {"url": "https://www.bilibili.com/video/1"}},
                },
            },
            {
                "event_id": str(uuid.uuid4()), "occurred_at": "2026-07-18T01:04:00Z",
                "event_type": "app.foreground", "duration_seconds": 180,
                "payload": {"activitywatch": {"kind": "afk", "data": {"status": "afk"}}},
            },
        ]
        phone_events = [{
            "event_id": str(uuid.uuid4()), "occurred_at": "2026-07-18T01:00:00Z",
            "event_type": "app.foreground", "duration_seconds": 600,
            "payload": {"app": {"display_name": "Reader"}},
        }]
        sync_server.write_device_events(
            desktop_events, device=desktop,
            batch_metadata={"received_at": "2026-07-18T02:00:00Z", "batch_id": "desktop-afk-trim"},
        )
        sync_server.write_device_events(
            phone_events, device=phone,
            batch_metadata={"received_at": "2026-07-18T02:00:00Z", "batch_id": "phone-afk-unchanged"},
        )

        usage = sync_server.get_usage_summary("2026-07-18")
        by_id = {item["device_key"]: item for item in usage["devices"]}
        self.assertEqual(by_id[desktop["device_key"]]["apps"], {"Google Chrome": 420})
        self.assertEqual(by_id[desktop["device_key"]]["hourly"], {"9": 420})
        self.assertEqual(by_id[desktop["device_key"]]["sites"], {"bilibili.com": 420})
        self.assertEqual(by_id[desktop["device_key"]]["hourly_online"], {"9": 420})
        self.assertEqual(by_id[phone["device_key"]]["apps"], {"Reader": 600})
        self.assertEqual(by_id[phone["device_key"]]["hourly"], {"9": 600})

    def test_usage_summary_clips_cross_day_and_caps_overlapping_windows(self):
        desktop = sync_server.v1_device_descriptor({
            "device_id": "desktop-boundary", "platform": "desktop", "display_name": "Boundary PC",
        })
        day_start, _ = sync_server.business_day_bounds("2026-07-18")
        events = [
            {
                "event_id": str(uuid.uuid4()),
                "occurred_at": sync_server.utc_timestamp(day_start - sync_server.timedelta(minutes=5)),
                "event_type": "app.foreground", "duration_seconds": 1200,
                "payload": {"app": {"display_name": "Editor"}, "activitywatch": {"kind": "window", "data": {"app": "Editor"}}},
            },
            {
                "event_id": str(uuid.uuid4()),
                "occurred_at": sync_server.utc_timestamp(day_start),
                "event_type": "app.foreground", "duration_seconds": 3600,
                "payload": {"app": {"display_name": "Terminal"}, "activitywatch": {"kind": "window", "data": {"app": "Terminal"}}},
            },
        ]
        sync_server.write_device_events(
            events, device=desktop,
            batch_metadata={"received_at": sync_server.utc_timestamp(day_start), "batch_id": "boundary"},
        )
        usage = sync_server.get_usage_summary("2026-07-18")
        item = next(entry for entry in usage["devices"] if entry["device_key"] == desktop["device_key"])
        self.assertEqual(item["apps"]["Editor"], 900)
        self.assertLessEqual(max(item["hourly"].values()), 3600)

    def test_chrome_url_markers_label_following_chrome_foreground_time(self):
        start = sync_server.datetime(2026, 7, 19, 1, 0, tzinfo=sync_server.timezone.utc)
        segments = sync_server.derive_chrome_domain_segments(
            [(start, 1_200)],
            [
                (start + sync_server.timedelta(minutes=1), "bilibili.com"),
                (start + sync_server.timedelta(minutes=11), "zhihu.com"),
                (start + sync_server.timedelta(minutes=25), "outside-session.example"),
            ],
        )
        self.assertEqual(segments, [
            ("bilibili.com", start + sync_server.timedelta(minutes=1), 600),
            ("zhihu.com", start + sync_server.timedelta(minutes=11), 540),
        ])
        hourly_sites = {}
        for domain, occurred, duration in segments:
            sync_server.add_site_duration_to_hourly(hourly_sites, domain, occurred, duration)
        self.assertEqual(hourly_sites, {
            "9": {"bilibili.com": 600, "zhihu.com": 540},
        })

    def test_chrome_url_marker_in_short_watcher_gap_labels_next_real_interval(self):
        start = sync_server.datetime(2026, 7, 19, 1, 0, tzinfo=sync_server.timezone.utc)
        segments = sync_server.derive_chrome_domain_segments(
            [(start, 60), (start + sync_server.timedelta(seconds=62), 58)],
            [
                (start + sync_server.timedelta(seconds=10), "bilibili.com"),
                (start + sync_server.timedelta(seconds=61), "zhihu.com"),
            ],
        )

        self.assertEqual(segments, [
            ("bilibili.com", start + sync_server.timedelta(seconds=10), 50),
            ("zhihu.com", start + sync_server.timedelta(seconds=62), 58),
        ])
        self.assertEqual(sum(duration for _, _, duration in segments), 108)

    def test_live_usage_uses_one_aw_snapshot_and_zero_duration_url_markers(self):
        now = sync_server.datetime(2026, 7, 19, 14, 30, tzinfo=sync_server.timezone.utc)
        status = sync_server.build_live_usage_status({
            "aw-watcher-window_test": [{
                "timestamp": "2026-07-19T14:05:00+00:00", "duration": 1_500,
                "data": {"app": "chrome.exe", "title": "video"},
            }],
            "aw-watcher-web-chrome_test": [{
                "timestamp": "2026-07-19T14:10:00+00:00", "duration": 0,
                "data": {"url": "https://www.bilibili.com/video/test"},
            }],
            "aw-watcher-afk_test": [{
                "timestamp": "2026-07-19T14:00:00+00:00", "duration": 1_800,
                "data": {"status": "not-afk"},
            }],
        }, now=now, completed_today_app_seconds=3_600, completed_today_blacklist_seconds=600)

        self.assertEqual(status["current_app"], "chrome.exe")
        self.assertEqual(status["current_site"], "bilibili.com")
        self.assertEqual(status["current_hour_app_seconds"], 1_500)
        self.assertEqual(status["current_hour_blacklist_seconds"], 1_200)
        self.assertEqual(status["today_app_seconds"], 5_100)
        self.assertEqual(status["today_blacklist_seconds"], 1_800)
        self.assertTrue(status["current_is_blacklisted"])
        self.assertEqual(status["blacklist_reason"], "site")

    def test_completed_local_usage_totals_excludes_current_hour_and_remote_devices(self):
        totals = sync_server.completed_local_usage_totals({
            "devices": [
                {
                    "is_local": True,
                    "hourly": {"13": 50, "14": 100, "15": 200},
                    "hourly_apps": {
                        "14": {"steam.exe": 80},
                        "15": {"steam.exe": 200},
                    },
                    "hourly_sites": {
                        "14": {"bilibili.com": 50},
                        "15": {"bilibili.com": 200},
                    },
                },
                {
                    "is_local": False,
                    "hourly": {"14": 9_999},
                    "hourly_apps": {"14": {"steam.exe": 9_999}},
                    "hourly_sites": {},
                },
            ],
        }, current_hour=15)

        self.assertEqual(totals, (150, 100))

    def test_live_usage_carries_pre_hour_domain_across_continuous_chrome_session(self):
        now = sync_server.datetime(2026, 7, 19, 15, 20, tzinfo=sync_server.timezone.utc)
        status = sync_server.build_live_usage_status({
            "aw-watcher-window_test": [
                {
                    "timestamp": "2026-07-19T14:55:00+00:00", "duration": 300,
                    "data": {"app": "chrome.exe", "title": "video"},
                },
                {
                    "timestamp": "2026-07-19T15:00:00+00:00", "duration": 1_200,
                    "data": {"app": "chrome.exe", "title": "video"},
                },
            ],
            "aw-watcher-web-chrome_test": [{
                "timestamp": "2026-07-19T14:56:00+00:00", "duration": 0,
                "data": {"url": "https://www.bilibili.com/video/test"},
            }],
            "aw-watcher-afk_test": [{
                "timestamp": "2026-07-19T15:00:00+00:00", "duration": 1_200,
                "data": {"status": "not-afk"},
            }],
        }, now=now)

        self.assertEqual(status["current_site"], "bilibili.com")
        self.assertEqual(status["current_hour_app_seconds"], 1_200)
        self.assertEqual(status["current_hour_blacklist_seconds"], 1_200)
        self.assertTrue(status["current_is_blacklisted"])

    def test_live_usage_keeps_url_across_long_chrome_watcher_gap_without_app_switch(self):
        now = sync_server.datetime(2026, 7, 19, 14, 30, tzinfo=sync_server.timezone.utc)
        status = sync_server.build_live_usage_status({
            "aw-watcher-window_test": [
                {
                    "timestamp": "2026-07-19T14:05:00+00:00", "duration": 60,
                    "data": {"app": "chrome.exe", "title": "video"},
                },
                {
                    "timestamp": "2026-07-19T14:29:00+00:00", "duration": 60,
                    "data": {"app": "chrome.exe", "title": "video"},
                },
            ],
            "aw-watcher-web-chrome_test": [{
                "timestamp": "2026-07-19T14:05:10+00:00", "duration": 0,
                "data": {"url": "https://www.bilibili.com/video/test"},
            }],
            "aw-watcher-afk_test": [{
                "timestamp": "2026-07-19T14:00:00+00:00", "duration": 1_800,
                "data": {"status": "not-afk"},
            }],
        }, now=now)

        self.assertEqual(status["current_site"], "bilibili.com")
        self.assertEqual(status["current_hour_blacklist_seconds"], 110)
        self.assertTrue(status["current_is_blacklisted"])

    def test_live_usage_clears_old_url_after_non_chrome_app_switch(self):
        now = sync_server.datetime(2026, 7, 19, 14, 30, tzinfo=sync_server.timezone.utc)
        status = sync_server.build_live_usage_status({
            "aw-watcher-window_test": [
                {
                    "timestamp": "2026-07-19T14:05:00+00:00", "duration": 60,
                    "data": {"app": "chrome.exe", "title": "video"},
                },
                {
                    "timestamp": "2026-07-19T14:20:00+00:00", "duration": 60,
                    "data": {"app": "QQ.exe", "title": "chat"},
                },
                {
                    "timestamp": "2026-07-19T14:29:00+00:00", "duration": 60,
                    "data": {"app": "chrome.exe", "title": "new session"},
                },
            ],
            "aw-watcher-web-chrome_test": [{
                "timestamp": "2026-07-19T14:05:10+00:00", "duration": 0,
                "data": {"url": "https://www.bilibili.com/video/test"},
            }],
            "aw-watcher-afk_test": [{
                "timestamp": "2026-07-19T14:00:00+00:00", "duration": 1_800,
                "data": {"status": "not-afk"},
            }],
        }, now=now)

        self.assertEqual(status["current_site"], "无")
        self.assertEqual(status["current_hour_blacklist_seconds"], 50)
        self.assertFalse(status["current_is_blacklisted"])

    def test_live_usage_resumes_recent_url_after_brief_app_switch(self):
        now = sync_server.datetime(2026, 7, 19, 14, 30, tzinfo=sync_server.timezone.utc)
        status = sync_server.build_live_usage_status({
            "aw-watcher-window_test": [
                {
                    "timestamp": "2026-07-19T14:27:00+00:00", "duration": 60,
                    "data": {"app": "chrome.exe", "title": "video"},
                },
                {
                    "timestamp": "2026-07-19T14:28:05+00:00", "duration": 10,
                    "data": {"app": "ChatGPT.exe", "title": "chat"},
                },
                {
                    "timestamp": "2026-07-19T14:28:16+00:00", "duration": 0,
                    "data": {"app": "explorer.exe", "title": ""},
                },
                {
                    "timestamp": "2026-07-19T14:29:00+00:00", "duration": 60,
                    "data": {"app": "chrome.exe", "title": "video"},
                },
            ],
            "aw-watcher-web-chrome_test": [{
                "timestamp": "2026-07-19T14:27:10+00:00", "duration": 0,
                "data": {"url": "https://www.bilibili.com/video/test"},
            }],
            "aw-watcher-afk_test": [{
                "timestamp": "2026-07-19T14:00:00+00:00", "duration": 1_800,
                "data": {"status": "not-afk"},
            }],
        }, now=now)

        self.assertEqual(status["current_site"], "bilibili.com")
        self.assertEqual(status["current_hour_blacklist_seconds"], 110)
        self.assertTrue(status["current_is_blacklisted"])

    def test_live_usage_endpoint_returns_snapshot(self):
        original = sync_server.get_live_usage_status
        try:
            sync_server.get_live_usage_status = lambda: {
                "status": "ok", "current_app": "Reader",
                "current_hour_app_seconds": 120, "current_site": "无",
                "current_hour_blacklist_seconds": 0, "current_is_blacklisted": False,
            }
            status, payload = self.request("GET", "/api/live-usage")
        finally:
            sync_server.get_live_usage_status = original
        self.assertEqual(status, 200)
        self.assertEqual(payload["current_app"], "Reader")

    def test_live_usage_exposes_continuous_afk_state_for_sedentary_reset(self):
        now = sync_server.datetime(2026, 7, 19, 14, 30, tzinfo=sync_server.timezone.utc)
        status = sync_server.build_live_usage_status({
            "aw-watcher-window_test": [{
                "timestamp": "2026-07-19T14:20:00+00:00", "duration": 600,
                "data": {"app": "chrome.exe", "title": "video"},
            }],
            "aw-watcher-afk_test": [{
                "timestamp": "2026-07-19T14:24:00+00:00", "duration": 360,
                "data": {"status": "afk"},
            }],
        }, now=now)

        self.assertEqual(status["activity_state"], "afk")
        self.assertEqual(status["current_afk_seconds"], 360)
        self.assertEqual(status["current_app"], "空闲")
        self.assertEqual(status["current_hour_app_seconds"], 240)

    def test_local_custom_event_is_stored_and_listed(self):
        status, response = self.request("POST", "/api/custom-events", {
            "event_key": "application.started",
            "title": "Life Link 已启动",
            "detail": "桌面组件已就绪",
            "metadata": {"reason": "manual-test"},
        })
        self.assertEqual(status, 201)
        stored_event = response["event"]
        self.assertEqual(stored_event["event_type"], "custom.event")
        self.assertEqual(stored_event["source"]["collector"], "life_radio_app")

        date_str = sync_server.business_date(
            sync_server.parse_utc_datetime(stored_event["occurred_at"])
        )
        status, summary = self.request("GET", f"/api/custom-events?date={date_str}")
        self.assertEqual(status, 200)
        self.assertEqual(len(summary["events"]), 1)
        self.assertEqual(summary["events"][0]["event_key"], "application.started")
        self.assertTrue(summary["devices"][0]["is_local"])

    def test_health_advertises_durable_local_device(self):
        status, health = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["device"]["platform"], "desktop")
        self.assertTrue(health["device"]["device_id"].startswith("desktop-"))
        self.assertEqual(
            health["device"]["display_name"],
            sync_server.get_hostname(),
        )

    def test_shared_settings_cache_drives_business_day_and_survives_refresh_failure(self):
        confirmed = shared_settings()
        original_base_url = sync_server.CENTRAL_BASE_URL
        original_token = sync_server.get_central_token
        original_client = sync_server.CENTRAL_CLIENT_CLASS
        try:
            sync_server.save_shared_settings_cache(confirmed)
            self.assertEqual(sync_server.get_day_start_hour(), 4)
            sync_server.CENTRAL_BASE_URL = "https://central.example.test"
            sync_server.get_central_token = lambda: "device-token"

            class OfflineClient:
                def __init__(self, *args):
                    pass

                def get_shared_settings(self):
                    raise sync_server.CentralReadError("central_unavailable", "offline")

            sync_server.CENTRAL_CLIENT_CLASS = OfflineClient
            self.assertIsNone(sync_server.refresh_shared_settings())
            self.assertEqual(sync_server.get_day_start_hour(), 4)
        finally:
            sync_server.CENTRAL_BASE_URL = original_base_url
            sync_server.get_central_token = original_token
            sync_server.CENTRAL_CLIENT_CLASS = original_client

    def test_invalid_shared_settings_cache_timestamp_falls_back_to_zero(self):
        sync_server.shared_settings_cache_path().parent.mkdir(parents=True, exist_ok=True)
        sync_server.shared_settings_cache_path().write_text(json.dumps({
            "timezone": "Asia/Shanghai", "day_start_hour": 4,
            "settings_version": 3, "updated_at": "not-a-timeZ",
        }), encoding="utf-8")
        self.assertIsNone(sync_server.load_shared_settings_cache())
        self.assertEqual(sync_server.get_day_start_hour(), 0)

    def test_device_wish_and_trigger_mutations_use_post_transport_upstream(self):
        original_write = sync_server._central_write_json
        calls = []
        try:
            def fake_write(method, path, body):
                calls.append((method, path, body))
                return ({"ok": True}, 200)

            sync_server._central_write_json = fake_write
            wish_id = "00000000-0000-4000-8000-000000000001"
            trigger_id = "00000000-0000-4000-8000-000000000002"
            device_id = "android-install-00000000-0000-4000-8000-000000000003"
            self.assertEqual(self.request("PATCH", f"/api/device-management/{device_id}", {"display_name": "Phone"})[0], 200)
            self.assertEqual(self.request("DELETE", f"/api/device-management/{device_id}")[0], 200)
            self.assertEqual(self.request("PATCH", f"/api/wishes/{wish_id}", {"text": "updated"})[0], 200)
            self.assertEqual(self.request("DELETE", f"/api/wishes/{wish_id}")[0], 200)
            self.assertEqual(self.request("PATCH", f"/api/event-triggers/{trigger_id}", {"enabled": False})[0], 200)
            self.assertEqual(self.request("DELETE", f"/api/event-triggers/{trigger_id}")[0], 200)
            self.assertEqual(calls, [
                ("POST", f"/v1/devices/{device_id}", {"display_name": "Phone"}),
                ("POST", f"/v1/devices/{device_id}/delete", None),
                ("POST", f"/v1/wishes/{wish_id}", {"text": "updated"}),
                ("POST", f"/v1/wishes/{wish_id}/delete", None),
                ("POST", f"/v1/event-triggers/{trigger_id}", {"enabled": False}),
                ("POST", f"/v1/event-triggers/{trigger_id}/delete", None),
            ])
        finally:
            sync_server._central_write_json = original_write

    def test_device_management_list_proxies_central_resource(self):
        original_read = sync_server._read_v17_resource_with_status
        calls = []
        try:
            local_device_id = sync_server.local_desktop_device_descriptor()["device_id"]
            def fake_read(path):
                calls.append(path)
                return {"devices": [{
                    "device_id": local_device_id, "platform": "desktop",
                    "display_name": "Office", "reported_name": "PC",
                    "custom_name": "Office", "is_current": True,
                }]}, False

            sync_server._read_v17_resource_with_status = fake_read
            status, body = self.request("GET", "/api/device-management")
            self.assertEqual(status, 200)
            self.assertEqual(body["devices"][0]["display_name"], "Office")
            self.assertTrue(body["devices"][0]["is_current"])
            self.assertEqual(calls, ["/v1/devices"])
        finally:
            sync_server._read_v17_resource_with_status = original_read

    def test_device_management_cache_payload_is_strictly_validated(self):
        valid = {"devices": [{
            "device_id": "desktop-a", "platform": "desktop",
            "display_name": "Office", "reported_name": "PC",
            "custom_name": "Office", "is_current": True,
        }]}
        self.assertTrue(sync_server._valid_v17_payload("/v1/devices", valid))
        invalid = json.loads(json.dumps(valid))
        invalid["devices"][0].pop("is_current")
        self.assertFalse(sync_server._valid_v17_payload("/v1/devices", invalid))

    def test_pc_login_startup_status_and_update_are_local_only(self):
        with patch.object(sync_server.pc_windows_startup, "status", return_value={"enabled": True, "registered": True, "blocked_by_windows": False, "state": "enabled"}):
            status, body = self.request("GET", "/api/runtime/login-startup")
        self.assertEqual(status, 200)
        self.assertEqual(body["enabled"], True)

        with patch.object(sync_server.pc_windows_startup, "set_enabled", return_value={"enabled": False, "registered": False, "blocked_by_windows": False, "state": "disabled"}) as set_enabled:
            status, body = self.request("POST", "/api/runtime/login-startup", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["enabled"], False)
        set_enabled.assert_called_once_with(False)

    def test_connection_package_reveal_opens_its_actual_export_folder(self):
        source = Path(sync_server.__file__).read_text(encoding="utf-8")
        self.assertIn("os.startfile(str(path.parent))", source)
        self.assertNotIn('f"/select,{path}"', source)

    def test_settings_api_proxies_central_patch_then_updates_read_only_cache(self):
        original_base_url = sync_server.CENTRAL_BASE_URL
        original_token = sync_server.get_central_token
        original_client = sync_server.CENTRAL_CLIENT_CLASS
        calls = []
        try:
            sync_server.CENTRAL_BASE_URL = "https://central.example.test"
            sync_server.get_central_token = lambda: "registered-device-token"

            class SharedSettingsClient:
                def __init__(self, base_url, token):
                    self.token = token

                def get_shared_settings(self):
                    calls.append(("GET", self.token))
                    return shared_settings()

                def update_shared_settings(self, hour):
                    calls.append(("PATCH", hour, self.token))
                    return shared_settings(hour, 4, "2026-08-09T01:03:03Z")

            sync_server.CENTRAL_CLIENT_CLASS = SharedSettingsClient
            status, current = self.request("GET", "/api/settings")
            self.assertEqual(status, 200)
            self.assertEqual(current["day_start_hour"], 4)
            status, updated = self.request("POST", "/api/settings", {"day_start_hour": 5})
            self.assertEqual(status, 200)
            self.assertEqual(updated["settings_version"], 4)
            self.assertEqual(sync_server.get_day_start_hour(), 5)
            self.assertEqual(calls, [
                ("GET", "registered-device-token"),
                ("PATCH", 5, "registered-device-token"),
            ])
        finally:
            sync_server.CENTRAL_BASE_URL = original_base_url
            sync_server.get_central_token = original_token
            sync_server.CENTRAL_CLIENT_CLASS = original_client

    def test_settings_api_does_not_claim_success_when_central_patch_fails(self):
        original_base_url = sync_server.CENTRAL_BASE_URL
        original_token = sync_server.get_central_token
        original_client = sync_server.CENTRAL_CLIENT_CLASS
        try:
            sync_server.save_shared_settings_cache(shared_settings())
            sync_server.CENTRAL_BASE_URL = "https://central.example.test"
            sync_server.get_central_token = lambda: "registered-device-token"

            class FailingClient:
                def __init__(self, *args):
                    pass

                def update_shared_settings(self, hour):
                    raise sync_server.CentralReadError("central_unavailable", "offline")

            sync_server.CENTRAL_CLIENT_CLASS = FailingClient
            status, response = self.request("POST", "/api/settings", {"day_start_hour": 5})
            self.assertEqual(status, 502)
            self.assertEqual(response["error"], "central_unavailable")
            self.assertEqual(sync_server.get_day_start_hour(), 4)
        finally:
            sync_server.CENTRAL_BASE_URL = original_base_url
            sync_server.get_central_token = original_token
            sync_server.CENTRAL_CLIENT_CLASS = original_client
