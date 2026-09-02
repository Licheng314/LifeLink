import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import sync_server
from central_client import CentralReadClient, CentralReadError


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    status = 200

    def __init__(self, payload): self.payload = payload
    def read(self): return json.dumps(self.payload).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *args): return False


class HealthInfoWebUiTests(unittest.TestCase):
    @staticmethod
    def _health_payload(date):
        return {
            "date": date,
            "timezone": "Asia/Shanghai",
            "sleep": {"status": "insufficient_data"},
            "steps": {"devices": []},
        }

    def test_central_reader_uses_health_info_path_and_bearer(self):
        captured = {}
        def open_request(request, timeout):
            captured["request"] = request
            return _Response({"date": "2026-08-13", "timezone": "Asia/Shanghai", "sleep": {}, "steps": {}})
        payload = CentralReadClient("https://central.example.test", "read-secret", opener=open_request).read_health_info("2026-08-13")
        self.assertEqual(payload["date"], "2026-08-13")
        self.assertIn("/v1/health-info?date=2026-08-13", captured["request"].full_url)
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer read-secret")

    def test_central_reader_does_not_leak_token_on_auth_failure(self):
        secret = "health-read-secret"
        def fail(request, timeout): raise HTTPError(request.full_url, 403, secret, Message(), None)
        with self.assertRaises(CentralReadError) as caught:
            CentralReadClient("https://central.example.test", secret, opener=fail).read_health_info("2026-08-13")
        self.assertEqual(caught.exception.category, "auth_error")
        self.assertNotIn(secret, str(caught.exception))

    def test_static_page_keeps_central_sleep_but_no_legacy_sleep_demo(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        scripts = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "web" / "scripts").glob("*.js"))
        self.assertIn('data-page="health-info"', html)
        self.assertIn('/assets/scripts/health-info.js', html)
        self.assertNotIn('/assets/scripts/sleep.js', html)
        self.assertNotIn("sleepWeekly", scripts)
        self.assertNotIn("睡眠评分", html)
        self.assertIn('id="health-sleep-card"', html)
        self.assertIn('id="health-sleep-week"', html)
        self.assertIn("/api/health-info?date=", scripts)
        self.assertIn("Promise.all", scripts)
        self.assertIn("X-Life-Radio-Cache", scripts)
        self.assertIn("function healthEscape", scripts)
        self.assertIn("healthEscape(selected.display_name", scripts)
        self.assertIn("function healthBoundaryApps", scripts)
        self.assertIn("睡前最后应用", scripts)
        self.assertIn("起床后第一应用", scripts)

    def test_health_webui_declares_single_device_steps_and_dynamic_week_chart(self):
        scripts = (ROOT / "web" / "scripts" / "health-info.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "styles" / "components.css").read_text(encoding="utf-8")
        self.assertIn("function healthDevice", scripts)
        self.assertIn("status === 'available'", scripts)
        self.assertNotIn("hourly_steps", scripts)
        self.assertIn("当日总步数", scripts)
        self.assertIn("renderTodaySteps", scripts)
        self.assertIn("function healthWeekStart", scripts)
        self.assertIn("getUTCDay()", scripts)
        self.assertIn("renderWeekSteps", scripts)
        self.assertIn("healthSleepPoint", scripts)
        self.assertIn("health-sleep-chart-wrap", scripts)
        self.assertIn("renderSleepWeek", scripts)
        self.assertIn("health-chart-wrap", styles)

    def test_health_chart_uses_shared_palette_loaded_first(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        health = (ROOT / "web" / "scripts" / "health-info.js").read_text(encoding="utf-8")
        shared = (ROOT / "web" / "scripts" / "shared-ui.js").read_text(encoding="utf-8")
        self.assertLess(html.index('/assets/scripts/shared-ui.js'), html.index('/assets/scripts/health-info.js'))
        self.assertIn("green:", shared)
        self.assertIn("chartColors.green", health)
        self.assertIn("chartColors.purple", health)

    def test_proxy_cache_isolated_by_requested_date(self):
        class SuccessfulClient:
            def __init__(self, *_): pass
            def read_health_info(self, date): return self_payloads[date]

        self_payloads = {
            "2026-08-12": self._health_payload("2026-08-12"),
            "2026-08-13": self._health_payload("2026-08-13"),
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.multiple(
            sync_server,
            DATA_DIR=Path(temporary),
            CENTRAL_BASE_URL="https://central.example.test",
            CENTRAL_READ_CLIENT_CLASS=SuccessfulClient,
        ), mock.patch.object(sync_server, "get_central_read_token", return_value="read-token"):
            sync_server._HEALTH_INFO_MEMORY.clear()
            for date in self_payloads:
                payload, stale = sync_server.read_central_health_info(date)
                self.assertEqual(payload["date"], date)
                self.assertFalse(stale)
            entries = sync_server._load_health_info_cache()
            self.assertEqual(set(entries), set(self_payloads))

    def test_health_page_restore_uses_navigation_load_hook(self):
        shared = (ROOT / "web" / "scripts" / "shared-ui.js").read_text(encoding="utf-8")
        health = (ROOT / "web" / "scripts" / "health-info.js").read_text(encoding="utf-8")
        self.assertIn("page === 'health-info'", shared)
        self.assertIn("requestHealthInfoLoad().catch(console.warn)", shared)
        self.assertIn("let healthInfoLoadPromise = null", health)
        self.assertNotIn("addEventListener('click', () => loadHealthInfo()", health)

    def test_unavailable_central_uses_only_same_date_cache(self):
        class UnavailableClient:
            def __init__(self, *_): pass
            def read_health_info(self, date):
                raise CentralReadError("central_unavailable", "offline")

        with tempfile.TemporaryDirectory() as temporary, mock.patch.multiple(
            sync_server,
            DATA_DIR=Path(temporary),
            CENTRAL_BASE_URL="https://central.example.test",
            CENTRAL_READ_CLIENT_CLASS=UnavailableClient,
        ), mock.patch.object(sync_server, "get_central_read_token", return_value="read-token"):
            sync_server._HEALTH_INFO_MEMORY.clear()
            sync_server._save_health_info_cache("2026-08-12", self._health_payload("2026-08-12"))
            cached, stale = sync_server.read_central_health_info("2026-08-12")
            self.assertEqual(cached["date"], "2026-08-12")
            self.assertTrue(stale)
            with self.assertRaises(CentralReadError):
                sync_server.read_central_health_info("2026-08-13")


if __name__ == "__main__":
    unittest.main()
