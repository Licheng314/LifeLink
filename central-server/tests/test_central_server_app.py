import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import central_server_app


class CentralServerAppTests(unittest.TestCase):
    def _response(self, payload, status=200):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__.return_value = response
        return response

    def test_management_readiness_requires_management_role(self):
        opener = mock.Mock()
        opener.open.return_value = self._response({"role": "central"})
        self.assertFalse(central_server_app.management_is_ready(opener=opener))
        opener.open.return_value = self._response({
            "status": "ok", "role": "life-link-central-management",
        })
        self.assertTrue(central_server_app.management_is_ready(opener=opener))
        self.assertEqual(
            opener.open.call_args.args[0],
            "http://127.0.0.1:8092/api/status",
        )

    def test_controlled_shutdown_sends_capability_header(self):
        opener = mock.Mock()
        opener.open.return_value = self._response({"ok": True}, status=204)
        self.assertTrue(central_server_app.request_managed_shutdown("high-entropy-token", opener=opener))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8092/api/shutdown")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer high-entropy-token")

    def test_tray_has_exactly_three_visible_menu_actions(self):
        source = Path(central_server_app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("tkinter", source)
        self.assertNotIn("messagebox", source)
        self.assertIn('"打开 WebUI"', source)
        self.assertIn('"重启服务器"', source)
        self.assertIn('"关闭服务器"', source)
        self.assertNotIn("生成设备配对码", source)
        self.assertNotIn("生成 AI 配对包", source)
        self.assertNotIn("登录后自动启动（点击", source)

    def test_tray_uses_native_message_loop_and_packaged_icon(self):
        source = Path(central_server_app.__file__).read_text(encoding="utf-8")
        self.assertIn("Shell_NotifyIconW", source)
        self.assertIn("GetMessageW", source)
        self.assertIn("MessageBoxW", source)
        self.assertEqual(
            central_server_app.TRAY_ICON_FILE,
            Path(central_server_app.__file__).resolve().parent / "assets" / "life-link-server-tray.ico",
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows tray smoke test")
    def test_native_tray_window_can_be_created_and_closed(self):
        tray = central_server_app.CentralServerTray(mock.Mock())
        try:
            self.assertTrue(tray.hwnd)
        finally:
            tray.close()

    @mock.patch("central_server_app.management_is_ready", side_effect=[False, True])
    @mock.patch("central_server_app.CentralConfig.from_environment")
    @mock.patch("central_server_app.ensure_server_configuration", return_value=Path("config.json"))
    @mock.patch("central_server_app.subprocess.Popen")
    def test_child_gets_private_management_token(
        self, popen, ensure_config, config_from_env, ready,
    ):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("central_server_app.default_data_dir", return_value=Path(directory)):
                config_from_env.return_value = mock.Mock(port=8091)
                child = mock.Mock()
                child.poll.return_value = None
                popen.return_value = child
                app = central_server_app.CentralServerApp()

                app.start_server()

                environment = popen.call_args.kwargs["env"]
                self.assertGreaterEqual(len(environment["LIFE_LINK_MANAGEMENT_TOKEN"]), 48)
                self.assertEqual(app.management_token, environment["LIFE_LINK_MANAGEMENT_TOKEN"])
                self.assertEqual(popen.call_args.args[0][1], str(central_server_app.SERVER_SCRIPT))
                app.stop_server()

    @mock.patch("central_server_app.request_managed_shutdown", return_value=True)
    def test_stop_prefers_controlled_shutdown_for_owned_child(self, shutdown):
        child = mock.Mock()
        child.poll.return_value = None
        app = central_server_app.CentralServerApp()
        app.process = child
        app.management_token = "owned-token"

        app.stop_server()

        shutdown.assert_called_once_with("owned-token")
        child.wait.assert_called_once_with(timeout=5)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    @mock.patch("central_server_app.request_managed_shutdown", return_value=False)
    def test_stop_only_forces_owned_child_after_timeout(self, _shutdown):
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("central", 5), None]
        app = central_server_app.CentralServerApp()
        app.process = child
        app.management_token = "owned-token"

        app.stop_server()

        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()

    @mock.patch("central_server_app.webbrowser.open")
    def test_second_shell_opens_management_url_without_starting_child(self, browser):
        app = central_server_app.CentralServerApp()
        with mock.patch.object(app, "acquire_single_instance", return_value=False), \
                mock.patch.object(app, "management_is_ready", return_value=True):
            self.assertEqual(app.run(), 0)
        browser.assert_called_once_with("http://127.0.0.1:8092")

    @mock.patch("central_server_app.webbrowser.open")
    def test_second_shell_does_not_open_an_unverified_management_port(self, browser):
        app = central_server_app.CentralServerApp()
        with mock.patch.object(app, "acquire_single_instance", return_value=False), \
                mock.patch.object(app, "management_is_ready", return_value=False), \
                mock.patch.object(app, "_show_message") as show_message:
            self.assertEqual(app.run(), 1)
        browser.assert_not_called()
        show_message.assert_called_once()

    def test_unexpected_exits_are_limited_to_three_retries(self):
        app = central_server_app.CentralServerApp()
        app.process = mock.Mock()
        app.process.poll.return_value = 1
        with mock.patch.object(app, "_show_message") as show_message:
            app.monitor()
            self.assertEqual(app.restart_attempts, 1)
            self.assertIsNotNone(app.next_restart_at)
            app.restart_attempts = 3
            app.process = mock.Mock()
            app.process.poll.return_value = 1
            app.monitor()
        show_message.assert_called_once()

    def test_reused_external_server_is_not_restarted_without_ownership(self):
        app = central_server_app.CentralServerApp()
        app.reused_existing_server = True
        with mock.patch.object(app, "_show_message") as show_message, \
                mock.patch.object(app, "stop_server") as stop_server, \
                mock.patch.object(app, "start_server") as start_server:
            app.restart_server()
        show_message.assert_called_once()
        stop_server.assert_not_called()
        start_server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
