import json
import io
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from central.config import CentralConfig
from central.http import create_server
from central.ai_connection_package import ConnectionPackage, create_connection_package
from central.management import create_management_server
from configure_tailscale_endpoint import TailscaleSetupError
import central_windows_startup


TOKEN = "device-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
READ_TOKEN = "read-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class CentralManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.path = root / "config.json"
        self.original_endpoint = {"version": 1, "provider": "public_domain", "base_url": "https://old.example", "verified_at": "2026-01-01T00:00:00Z"}
        self.path.write_text(json.dumps({"token_bindings": {TOKEN: "desktop-test"}, "read_token": READ_TOKEN,
            "database_path": str(root / "central.sqlite3"), "public_endpoint": self.original_endpoint}), encoding="utf-8")
        self.config = CentralConfig(database_path=root / "central.sqlite3", token_bindings={TOKEN: "desktop-test"},
            read_token=READ_TOKEN, config_path=self.path)
        self.data = create_server(self.config, ("127.0.0.1", 0))
        self.data_thread = threading.Thread(target=self.data.serve_forever, daemon=True); self.data_thread.start()
        self.original_endpoint["central_instance_id"] = self.data.store.ai_readers.central_instance_id()
        self.path.write_text(json.dumps({"token_bindings": {TOKEN: "desktop-test"}, "read_token": READ_TOKEN,
            "database_path": str(root / "central.sqlite3"), "public_endpoint": self.original_endpoint}), encoding="utf-8")
        self.stopped = threading.Event()
        self.management = create_management_server(self.data, self.config, ("127.0.0.1", 0), self.stopped.set)
        self.thread = threading.Thread(target=self.management.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.management.server_port}"
        self.data_base = f"http://127.0.0.1:{self.data.server_port}"
        self.client = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.management.shutdown(); self.management.server_close(); self.thread.join(timeout=2)
        self.data.shutdown(); self.data.server_close(); self.data_thread.join(timeout=2); self.temp.cleanup()

    def request(self, path, body=None, *, method=None, csrf=True, origin=True, authorization=None):
        headers = {}
        if origin: headers["Origin"] = self.base
        if csrf: headers["X-CSRF-Token"] = self.management.csrf_token
        if authorization: headers["Authorization"] = authorization
        request = Request(self.base + path, data=(json.dumps(body).encode() if body is not None else None), headers=headers,
                          method=method or ("POST" if body is not None else "GET"))
        try:
            with self.client.open(request) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_status_is_loopback_only_and_has_no_secret(self):
        status, payload = self.request("/api/status", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["role"], "life-link-central-management")
        self.assertEqual(payload["data_api"]["port"], self.config.port)
        self.assertNotIn(TOKEN, json.dumps(payload))
        bad = Request(self.base + "/api/status", headers={"Host": "evil.example"})
        with self.assertRaises(HTTPError) as raised: self.client.open(bad)
        self.assertEqual(raised.exception.code, 400)

    def test_page_csp_allows_dashboard_inline_styles_but_not_inline_scripts(self):
        with self.client.open(Request(self.base + "/")) as response:
            policy = response.headers["Content-Security-Policy"]
        self.assertIn("style-src 'self' 'unsafe-inline'", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)

    def test_dashboard_reads_use_live_central_projections_without_browser_tokens(self):
        store = self.data.store
        with mock.patch.object(store, "calendar_days", return_value={"days": ["calendar"]}) as calendar, \
             mock.patch.object(store, "list_timeline", return_value={"events": ["timeline"]}) as timeline, \
             mock.patch.object(store, "read_usage", return_value={"devices": ["usage"]}) as usage, \
             mock.patch.object(store, "read_locations", return_value={"stays": ["location"]}) as locations, \
             mock.patch.object(store, "read_health_info", return_value={"sleep": "health"}) as health, \
             mock.patch.object(store, "list_managed_devices", return_value=[{"device_id": "desktop-test"}]) as devices:
            cases = (
                ("/api/dashboard/calendar-days?from=2026-08-01&to=2026-08-07", {"days": ["calendar"]}),
                ("/api/dashboard/timeline-events?from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z", {"events": ["timeline"]}),
                ("/api/dashboard/usage?from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z", {"devices": ["usage"]}),
                ("/api/dashboard/locations?from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z", {"stays": ["location"]}),
                ("/api/dashboard/health-info?date=2026-08-01", {"sleep": "health"}),
                ("/api/dashboard/devices", None),
            )
            for path, expected in cases:
                status, payload = self.request(path, csrf=False, origin=False)
                self.assertEqual(status, 200)
                if expected is None:
                    self.assertIsInstance(payload.get("devices"), list)
                    self.assertEqual(len(payload["devices"]), 1)
                    self.assertEqual(payload["devices"][0]["device_id"], "desktop-test")
                    self.assertIsInstance(payload["devices"][0].get("is_current"), bool)
                else:
                    self.assertEqual(payload, expected)
                self.assertNotIn(TOKEN, json.dumps(payload))
                self.assertNotIn(READ_TOKEN, json.dumps(payload))
        calendar.assert_called_once_with("2026-08-01", "2026-08-07")
        timeline.assert_called_once()
        usage.assert_called_once()
        locations.assert_called_once()
        health.assert_called_once()
        devices.assert_called_once_with()

    def test_dashboard_reads_reject_invalid_or_extra_query_parameters(self):
        for path in (
            "/api/dashboard/calendar-days?from=2026-08-01",
            "/api/dashboard/health-info?date=2026-8-1",
            "/api/dashboard/devices?unexpected=true",
            "/api/dashboard/usage?from=2026-08-01T00:00:00Z&to=2026-08-03T00:00:00Z",
        ):
            status, payload = self.request(path, csrf=False, origin=False)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_dashboard_query")

    def test_copied_dashboard_read_compatibility_stays_in_central_process(self):
        status, settings = self.request("/api/settings", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertIsInstance(settings["day_start_hour"], int)
        status, devices = self.request("/api/devices", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertIn("devices", devices)
        status, calendar = self.request("/api/calendar-days?from=2026-08-01&to=2026-08-07", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertIn("days", calendar)

    def test_copied_dashboard_timeline_uses_a_private_conditional_response(self):
        path = "/api/timeline-events?from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z"
        with self.client.open(Request(self.base + path)) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            etag = response.headers["ETag"]
            first_body = response.read()
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))
        self.assertTrue(first_body)

        request = Request(self.base + path, headers={"If-None-Match": etag})
        with self.assertRaises(HTTPError) as raised:
            self.client.open(request)
        self.assertEqual(raised.exception.code, 304)
        self.assertEqual(raised.exception.headers["Cache-Control"], "no-store")
        self.assertEqual(raised.exception.headers["ETag"], etag)

    def test_copied_dashboard_timeline_script_revalidates_its_in_memory_snapshot(self):
        script = (Path(__file__).resolve().parents[1] / "management-web" / "assets" / "scripts" / "wishes-events.js").read_text(encoding="utf-8")
        self.assertIn("const eventsTimelineEtags = new Map()", script)
        self.assertIn("'If-None-Match': etag", script)
        self.assertIn("if (resp.status === 304) return false", script)

    def test_ai_recent_access_indicator_uses_the_reader_access_window(self):
        script = (Path(__file__).resolve().parents[1] / "management-web" / "assets" / "scripts" / "wishes-events.js").read_text(encoding="utf-8")
        self.assertIn("30 * 60 * 1000", script)
        self.assertIn("reader.last_requested_at || log?.requested_at", script)
        self.assertIn("ai-reader-detection-dot", script)

    def test_copied_dashboard_reports_central_health_and_ai_access_records(self):
        status, health = self.request("/api/central-health", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertTrue(health["connected"])
        pairing = self.data.store.ai_readers.create_pairing(
            claim_url="https://central.example.test/v1/ai-readers/pairings/claim"
        )
        pairing_payload = json.loads(pairing.text)
        profile = self.data.store.ai_readers.claim_pairing(
            pairing_token=pairing_payload["pairing_token"],
            claim={
                "schema_version": 1,
                "pairing_id": pairing_payload["pairing_id"],
                "reader": {"type": "test", "instance_id": "dashboard-reader", "display_name": "Dashboard Reader", "process_identity": None, "process_binding": None},
            },
        )
        reader_id = profile["reader_id"]
        status, process = self.request(f"/api/ai-readers/{reader_id}/process-status", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertFalse(process["process_running"])
        status, logs = self.request(f"/api/ai-readers/{reader_id}/access-logs?limit=10", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertEqual(logs["reader_id"], reader_id)

    def test_ai_reader_skill_is_readable_without_exporting_a_host_file(self):
        status, payload = self.request("/api/ai-reader-skill", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertIn("Life Link", payload["skill"])

    def test_ai_mcp_config_preview_contains_only_editable_local_placeholders(self):
        status, payload = self.request("/api/ai-connection-mcp-config", csrf=False, origin=False)
        self.assertEqual(status, 200)
        config = payload["mcp_config"]["mcpServers"]["life-link"]
        self.assertEqual(config["command"], "<PYTHON_COMMAND>")
        self.assertIn("<LIFE_LINK_MCP_DIR>/life_link_mcp.py", config["args"])
        self.assertNotIn(TOKEN, json.dumps(payload))
        self.assertNotIn(READ_TOKEN, json.dumps(payload))

    def test_central_map_tile_proxy_keeps_key_out_of_browser_url(self):
        fake_tile = mock.MagicMock()
        fake_tile.read.return_value = b"png-data"
        fake_tile.headers.get.return_value = "image/png"
        fake_tile.__enter__.return_value = fake_tile
        fake_tile.__exit__.return_value = False
        with mock.patch("central.management.urlopen", return_value=fake_tile) as opened:
            request = Request(self.base + "/map-tiles/vec/3/4/2.png")
            with self.client.open(request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"png-data")
                self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")
        self.assertIn("tk=" + self.config.tianditu_key, opened.call_args.args[0].full_url)

    def test_central_map_tile_proxy_reuses_public_process_cache(self):
        fake_tile = mock.MagicMock()
        fake_tile.read.return_value = b"png-data"
        fake_tile.headers.get.return_value = "image/png"
        fake_tile.__enter__.return_value = fake_tile
        fake_tile.__exit__.return_value = False
        with mock.patch("central.management.urlopen", return_value=fake_tile) as opened:
            for _ in range(2):
                with self.client.open(self.base + "/map-tiles/vec/3/4/2.png") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"png-data")
            self.assertEqual(opened.call_count, 1)

    def test_https_web_session_proxies_map_tiles_with_public_cache_only(self):
        create = Request(
            self.data_base + "/v1/web-sessions", data=b"{}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            method="POST",
        )
        with self.client.open(create) as response:
            web_url = json.loads(response.read())["web_url"]
        bootstrap = parse_qs(urlparse(web_url).fragment)["lifelink_bootstrap"][0]

        claim = Request(
            self.data_base + "/v1/web-sessions/claim",
            data=json.dumps({"bootstrap_token": bootstrap}).encode(),
            headers={"Origin": "https://old.example", "Content-Type": "application/json"},
            method="POST",
        )
        with self.client.open(claim) as response:
            self.assertEqual(response.status, 204)
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]

        fake_tile = mock.MagicMock()
        fake_tile.read.return_value = b"png-data"
        fake_tile.headers.get.return_value = "image/png"
        fake_tile.__enter__.return_value = fake_tile
        fake_tile.__exit__.return_value = False
        with mock.patch("central.management.urlopen", return_value=fake_tile):
            tile = Request(self.data_base + "/map-tiles/vec/3/4/2.png", headers={"Cookie": cookie})
            with self.client.open(tile) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"png-data")
                self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")

        api = Request(self.data_base + "/api/status", headers={"Cookie": cookie})
        with self.client.open(api) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_copied_dashboard_settings_write_keeps_csrf_boundary(self):
        status, payload = self.request("/api/settings", {"day_start_hour": 4}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_rejected")
        status, payload = self.request("/api/settings", {"day_start_hour": 4})
        self.assertEqual(status, 200)
        self.assertEqual(payload["day_start_hour"], 4)

    def test_copied_dashboard_blacklist_write_keeps_csrf_boundary(self):
        body = {"rule_type": "domain", "pattern": "example.test", "label": "Example", "platform_scope": "web", "enabled": True}
        status, payload = self.request("/api/blacklist/rules", body, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_rejected")
        status, payload = self.request("/api/blacklist/rules", body)
        self.assertEqual(status, 201)
        self.assertEqual(payload["pattern"], "example.test")

    def test_copied_dashboard_trigger_write_keeps_csrf_boundary(self):
        body = {"request_id": "trigger-request-1", "wish_id": None, "trigger_type": "blacklist_usage_milestone", "config_version": 1, "parameters": {"platform_scope": "all"}, "interval_minutes": 60, "enabled": True}
        status, payload = self.request("/api/event-triggers", body, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_rejected")
        status, payload = self.request("/api/event-triggers", body)
        self.assertEqual(status, 201)
        self.assertEqual(payload["trigger_type"], "blacklist_usage_milestone")

    def test_copied_dashboard_wish_writes_use_central_store(self):
        create = {
            "request_id": "91ee99de-a832-4c9c-989a-867607d35e72",
            "text": "中央管理测试心愿",
            "duration_days": 3,
            "ai_tracking_enabled": False,
        }
        status, payload = self.request("/api/wishes", create, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_rejected")
        status, wish = self.request("/api/wishes", create)
        self.assertEqual(status, 201)
        self.assertEqual(wish["text"], create["text"])
        status, changed = self.request(f"/api/wishes/{wish['wish_id']}", {"text": "已修改的中央心愿"}, method="PATCH")
        self.assertEqual(status, 200)
        self.assertEqual(changed["text"], "已修改的中央心愿")
        status, day = self.request(f"/api/wishes/{wish['wish_id']}/days/{wish['starts_on']}",
                                   {"evaluation": "completed"}, method="PUT")
        self.assertEqual(status, 200)
        self.assertEqual(day["evaluation"], "completed")
        status, payload = self.request(f"/api/wishes/{wish['wish_id']}/complete", method="POST")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "wish_not_completable")
        status, _ = self.request(f"/api/wishes/{wish['wish_id']}", method="DELETE")
        self.assertEqual(status, 204)

    def test_central_login_startup_is_a_csrf_protected_central_service_setting(self):
        with mock.patch("central.management.central_windows_startup.status", return_value={"enabled": True, "state": "enabled"}):
            status, payload = self.request("/api/runtime/login-startup", csrf=False, origin=False)
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        status, payload = self.request("/api/runtime/login-startup", {"enabled": False}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_rejected")
        with mock.patch("central.management.central_windows_startup.set_enabled", return_value={"enabled": False, "state": "disabled"}) as set_enabled:
            status, payload = self.request("/api/runtime/login-startup", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        set_enabled.assert_called_once_with(False)

    def test_central_login_startup_reports_unsupported_platform_explicitly(self):
        state = central_windows_startup.status(registry=None)
        self.assertFalse(state["supported"])
        self.assertFalse(state["enabled"])
        self.assertEqual(state["state"], "unsupported")

    def test_mutations_require_origin_and_csrf(self):
        status, payload = self.request("/api/device-invitations", {}, csrf=False)
        self.assertEqual(status, 403); self.assertEqual(payload["error"], "csrf_rejected")
        status, payload = self.request("/api/device-invitations", {})
        self.assertEqual(status, 201); self.assertTrue(payload["code"].startswith("LR1."))

    def test_invitation_requires_a_verified_external_endpoint(self):
        current = json.loads(self.path.read_text(encoding="utf-8"))
        current.pop("public_endpoint")
        self.path.write_text(json.dumps(current), encoding="utf-8")

        status, payload = self.request("/api/device-invitations", {})

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "invitation_unavailable")

    def test_ai_connection_package_downloads_central_zip_without_exposing_credentials(self):
        with mock.patch(
            "central.management.verify_public_endpoint",
            return_value={"base_url": "https://old.example", "central_instance_id": self.original_endpoint["central_instance_id"]},
        ), mock.patch(
            "central.management.create_connection_package",
            return_value=ConnectionPackage("life-link-ai.zip", "2099-01-01T00:00:00Z", b"zip-data"),
        ):
            headers = {"Origin": self.base, "X-CSRF-Token": self.management.csrf_token}
            request = Request(self.base + "/api/ai-connection-package", data=b"{}", headers=headers, method="POST")
            with self.client.open(request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "application/zip")
                self.assertIn('filename="life-link-ai.zip"', response.headers["Content-Disposition"])
                self.assertEqual(response.read(), b"zip-data")

    def test_ai_connection_package_requires_reverified_network(self):
        with mock.patch("central.management.verify_public_endpoint", side_effect=ValueError("TLS failed")):
            status, payload = self.request("/api/ai-connection-package", {})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "ai_connection_package_unavailable")
        self.assertIn("TLS failed", payload["message"])

    def test_central_package_contains_portable_mcp_and_https_pairing(self):
        package = create_connection_package(
            store=self.data.store, external_origin="https://central.example.test",
        )
        self.assertTrue(package.filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(package.payload)) as archive:
            self.assertEqual(set(archive.namelist()), {
                "README.md", "manifest.json", "pairing.json", "mcp-config.json",
                "reader.json", "life_link_mcp.py", "life-link-ai-reader/SKILL.md",
            })
            pairing = json.loads(archive.read("pairing.json"))
            config = json.loads(archive.read("mcp-config.json"))
            readme = archive.read("README.md").decode("utf-8")
        self.assertEqual(pairing["claim_url"], "https://central.example.test/v1/ai-readers/pairings/claim")
        self.assertEqual(config["mcpServers"]["life-link"]["command"], "<PYTHON_COMMAND>")
        self.assertIn("<LIFE_LINK_MCP_DIR>", readme)
        self.assertNotIn(pairing["pairing_token"], readme)

    def test_tailscale_detection_returns_candidate_without_saving(self):
        with mock.patch(
            "central.management.detect_https_endpoint",
            return_value="https://central.tail-example.ts.net:8443",
        ) as detect:
            status, payload = self.request("/api/network/tailscale/detect", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "detected")
        self.assertEqual(payload["base_url"], "https://central.tail-example.ts.net:8443")
        detect.assert_called_once_with()
        saved = json.loads(self.path.read_text(encoding="utf-8"))["public_endpoint"]
        self.assertEqual(saved, self.original_endpoint)

    def test_failed_tailscale_detection_keeps_existing_endpoint(self):
        with mock.patch(
            "central.management.detect_https_endpoint",
            side_effect=TailscaleSetupError("未找到 Tailscale。请先安装并登录 Tailscale。"),
        ):
            status, payload = self.request("/api/network/tailscale/detect", {})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "tailscale_detection_failed")
        self.assertIn("未找到 Tailscale", payload["message"])
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["public_endpoint"],
            self.original_endpoint,
        )

    def test_copied_dashboard_keeps_pc_navigation_baseline(self):
        web_root = Path(__file__).resolve().parents[1] / "management-web"
        page = (web_root / "index.html").read_text(encoding="utf-8")
        for name in ("timeline-events", "app-usage", "location", "health-info"):
            self.assertIn(f'data-page="{name}"', page)
            self.assertIn(f'id="page-{name}"', page)
        self.assertIn('id="business-calendar-days"', page)
        self.assertIn('/assets/scripts/wishes-events.js', page)
        self.assertIn('/assets/scripts/shared-ui.js', page)
        self.assertIn('data-page="central-management"', page)
        self.assertIn('id="page-central-management"', page)
        self.assertNotIn('id="page-sync"', page)
        self.assertNotIn('sidebar-server-status', page)
        self.assertIn('id="sync-device-cards"', page)
        self.assertIn('class="central-device-divider"', page)
        self.assertIn('id="day-boundary-hour"', page)
        self.assertIn('id="central-login-startup"', page)
        self.assertIn('其他设置', page)
        timeline_script = (web_root / 'assets' / 'scripts' / 'wishes-events.js').read_text(encoding='utf-8')
        management_script = (web_root / 'assets' / 'scripts' / 'central-management.js').read_text(encoding='utf-8')
        devices_script = (web_root / 'assets' / 'scripts' / 'devices.js').read_text(encoding='utf-8')
        self.assertIn('id="ai-mcp-config-open"', timeline_script)
        self.assertIn("await showMcpConfig();", management_script)
        self.assertIn('class="mcp-placeholder"', management_script)
        self.assertIn('state.supported === false', devices_script)
        self.assertIn('仅支持 Windows', devices_script)
        self.assertNotIn('id="central-create-ai-package"', page)
        self.assertIn('/assets/scripts/central-management.js', page)
        self.assertIn('/assets/styles/central-management.css', page)
        management_css = (web_root / 'assets' / 'styles' / 'central-management.css').read_text(encoding='utf-8')
        self.assertIn('#page-central-management > #sync-device-cards', management_css)
        self.assertIn('overflow-x: auto', management_css)
        self.assertIn('input[type="checkbox"]', management_css)
        self.assertIn('width: 16px', management_css)
        self.assertIn('.mcp-config-hint { margin: 0; color: var(--text);', management_css)

    def test_copied_dashboard_uses_local_vendored_runtime_assets(self):
        web_root = Path(__file__).resolve().parents[1] / "management-web"
        page = (web_root / "index.html").read_text(encoding="utf-8")
        management = (Path(__file__).resolve().parents[1] / "central" / "management.py").read_text(encoding="utf-8")
        self.assertIn('/assets/vendor/chart.umd.min.js', page)
        self.assertIn('/assets/vendor/lucide.js', page)
        self.assertNotIn('cdn.jsdelivr.net', page)
        self.assertNotIn('unpkg.com', page)
        for asset in ('chart.umd.min.js', 'lucide.js'):
            self.assertTrue((web_root / 'assets' / 'vendor' / asset).is_file())
            self.assertIn(asset, management)
        self.assertTrue((web_root / 'assets' / 'vendor' / 'leaflet' / 'leaflet.js').is_file())
        self.assertIn('assets/vendor/leaflet/leaflet.js', management)
        self.assertTrue((web_root / 'assets' / 'styles' / 'central-management.css').is_file())
        self.assertIn('assets/styles/central-management.css', management)

    def test_central_dashboard_is_independent_of_retired_pc_static_source(self):
        repo_root = Path(__file__).resolve().parents[2]
        central_assets = repo_root / "central-server" / "management-web" / "assets"
        for asset_type, name in (
            ("styles", "base.css"),
            ("styles", "components.css"),
            ("styles", "wishes-events.css"),
            ("scripts", "usage.js"),
        ):
            self.assertTrue((central_assets / asset_type / name).is_file())
        self.assertFalse((repo_root / "pc-dashboard" / "dashboard.html").exists())
        self.assertFalse((repo_root / "pc-dashboard" / "web").exists())

    def test_failed_network_candidate_keeps_existing_endpoint(self):
        with mock.patch("central.management.verify_public_endpoint", side_effect=ValueError("different server")):
            status, _ = self.request("/api/network/verify", {"provider": "tailscale", "base_url": "https://new.example"})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["public_endpoint"], self.original_endpoint)

    def test_verified_network_is_saved_atomically_after_check(self):
        with mock.patch("central.management.verify_public_endpoint", return_value={"base_url": "https://new.example", "central_instance_id": "central-test"}):
            status, payload = self.request("/api/network/verify", {"provider": "https_tunnel", "base_url": "https://new.example"})
        self.assertEqual(status, 200); self.assertEqual(payload["public_endpoint"]["base_url"], "https://new.example")
        self.assertEqual(payload["public_endpoint"]["central_instance_id"], "central-test")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["public_endpoint"]["provider"], "https_tunnel")

    def test_shutdown_requires_explicit_management_token(self):
        with mock.patch.dict(os.environ, {"LIFE_LINK_MANAGEMENT_TOKEN": "shutdown-test-token"}, clear=False):
            status, _ = self.request(
                "/api/shutdown", {}, csrf=False, origin=False,
                authorization="Bearer wrong",
            )
            self.assertEqual(status, 401)
            status, payload = self.request(
                "/api/shutdown", {}, csrf=False, origin=False,
                authorization="Bearer shutdown-test-token",
            )
        self.assertEqual(status, 202); self.assertEqual(payload["status"], "shutting_down")
        self.assertTrue(self.stopped.wait(1))
