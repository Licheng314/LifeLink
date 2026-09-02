import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import central_server_app
import central_windows_startup


class CentralServerAppTests(unittest.TestCase):
    def test_login_startup_uses_formal_executable_without_secrets(self):
        command = central_windows_startup.startup_command(
            Path("C:/Life Link/central-server/LifeLink Central Service.exe"),
            environ={"WINDIR": "C:/Windows"},
        )
        self.assertEqual(
            command,
            '"C:\\Life Link\\central-server\\LifeLink Central Service.exe"',
        )
        self.assertNotIn("token", command.lower())

    @mock.patch.object(central_windows_startup, "_windows_blocked", return_value=False)
    @mock.patch.object(central_windows_startup, "_preference", return_value=True)
    @mock.patch.object(central_windows_startup, "shortcut_path", return_value=Path("missing.lnk"))
    def test_missing_requested_startup_is_reported_separately(
        self, _shortcut, _preference, _blocked,
    ):
        state = central_windows_startup.status()
        self.assertEqual(state["state"], "missing")
        self.assertTrue(state["requested_enabled"])
        self.assertFalse(state["enabled"])

    @mock.patch.object(central_windows_startup, "_windows_blocked", return_value=True)
    @mock.patch.object(central_windows_startup, "_preference", return_value=True)
    @mock.patch.object(central_windows_startup, "shortcut_path", return_value=Path("missing.lnk"))
    def test_windows_block_is_visible_even_if_manager_removed_shortcut(
        self, _shortcut, _preference, _blocked,
    ):
        state = central_windows_startup.status()
        self.assertEqual(state["state"], "blocked")
        self.assertTrue(state["blocked_by_windows"])

    def test_shortcut_creation_cannot_succeed_without_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "LifeLink Central Service.exe"
            target.touch()
            with (
                mock.patch.object(central_windows_startup, "startup_target", return_value=target),
                mock.patch.object(central_windows_startup.subprocess, "run"),
            ):
                with self.assertRaisesRegex(RuntimeError, "启动项未生成"):
                    central_windows_startup._create_shortcut(
                        environ={"APPDATA": directory},
                    )

    @mock.patch.object(central_windows_startup, "ensure_default_enabled", return_value=True)
    def test_startup_registration_entry_uses_generated_launcher(self, ensure):
        self.assertEqual(central_windows_startup.main(), 0)
        ensure.assert_called_once_with()

    def test_tray_uses_the_packaged_server_icon(self):
        self.assertEqual(
            central_server_app.TRAY_ICON_FILE,
            Path(central_server_app.__file__).resolve().parent
            / "assets" / "life-link-server-tray.ico",
        )
        self.assertTrue(central_server_app.TRAY_ICON_FILE.is_file())
        self.assertEqual(
            central_server_app.TRAY_ICON_FILE.read_bytes()[:4], b"\x00\x00\x01\x00",
        )
        source = Path(central_server_app.__file__).read_text(encoding="utf-8")
        self.assertIn("LoadImageW", source)

    def test_default_config_lives_in_unique_user_data_directory(self):
        self.assertEqual(
            central_server_app.default_config_path(),
            central_server_app.default_data_dir() / "config.json",
        )

    @mock.patch("central_server_app.health_is_ready", return_value=True)
    def test_tray_health_check_uses_configured_server_port(self, probe):
        app = object.__new__(central_server_app.CentralServerApp)
        app.server_port = 9123

        self.assertTrue(app.health_is_ready())
        self.assertEqual(app.health_url, "http://127.0.0.1:9123/v1/health")
        probe.assert_called_once_with(port=9123)

    def test_tray_exposes_device_pairing_and_ai_package_but_not_ai_status(self):
        source = Path(central_server_app.__file__).read_text(encoding="utf-8")
        self.assertIn("生成设备配对码", source)
        self.assertIn("生成 AI 配对包", source)
        self.assertNotIn("生成 AI 匹配码", source)
        self.assertNotIn("查看 AI 读取状态", source)
        self.assertTrue(hasattr(central_server_app.CentralServerApp, "generate_invitation"))
        self.assertTrue(hasattr(central_server_app.CentralServerApp, "generate_mcp_connection_package"))
        self.assertFalse(hasattr(central_server_app.CentralServerApp, "show_ai_reader_status"))
        self.assertTrue(hasattr(central_server_app.CentralServerApp, "toggle_login_startup"))

    def test_mcp_package_request_uses_local_pc_service(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "filename": "LifeLink-AI-MCP-Connection-new.zip",
            "expires_at": "2099-01-01T00:00:00Z",
        }).encode("utf-8")
        response.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = response

        result = central_server_app.request_mcp_connection_package(opener=opener)

        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, central_server_app.MCP_CONNECTION_PACKAGE_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(result["filename"], "LifeLink-AI-MCP-Connection-new.zip")

    @mock.patch("central_server_app.messagebox.showinfo")
    @mock.patch("central_server_app.copy_to_clipboard", return_value=True)
    @mock.patch("central_server_app.create_client_invitation")
    @mock.patch("central_server_app.ensure_server_configuration")
    @mock.patch("central_server_app.default_endpoint_path")
    def test_tray_invitation_is_created_and_copied(
        self, endpoint_path, ensure_config, create_invitation, copy_clipboard, showinfo,
    ):
        endpoint_path.return_value = Path("endpoint.json")
        ensure_config.return_value = Path("config.json")
        create_invitation.return_value = SimpleNamespace(code="LR1.test-invitation")
        app = object.__new__(central_server_app.CentralServerApp)

        app.generate_invitation()

        create_invitation.assert_called_once_with(
            config_path=Path("config.json"), endpoint_path=Path("endpoint.json"),
        )
        copy_clipboard.assert_called_once_with("LR1.test-invitation")
        showinfo.assert_called_once()

    @mock.patch("central_server_app.messagebox.showwarning")
    @mock.patch("central_server_app.copy_to_clipboard", return_value=False)
    @mock.patch("central_server_app.create_client_invitation")
    @mock.patch(
        "central_server_app.ensure_server_configuration",
        return_value=Path("config.json"),
    )
    @mock.patch(
        "central_server_app.default_endpoint_path",
        return_value=Path("endpoint.json"),
    )
    def test_tray_invitation_reports_clipboard_failure(
        self, _endpoint_path, _ensure_config, create_invitation, _copy_clipboard, showwarning,
    ):
        create_invitation.return_value = SimpleNamespace(code="LR1.test-invitation")
        app = object.__new__(central_server_app.CentralServerApp)

        app.generate_invitation()

        showwarning.assert_called_once()

    @mock.patch("central_server_app.messagebox.showinfo")
    @mock.patch("central_server_app.request_mcp_connection_package")
    def test_tray_mcp_package_is_generated_and_revealed(
        self, create_package, showinfo,
    ):
        create_package.return_value = {
            "filename": "LifeLink-AI-MCP-Connection-new.zip",
        }
        app = object.__new__(central_server_app.CentralServerApp)

        app.generate_mcp_connection_package()

        create_package.assert_called_once_with()
        showinfo.assert_called_once()
        self.assertIn("将压缩包发送给 AI 来完成连接", showinfo.call_args.args[1])

    @mock.patch("central_server_app.messagebox.showerror")
    @mock.patch(
        "central_server_app.request_mcp_connection_package",
        side_effect=OSError("PC service unavailable"),
    )
    def test_tray_mcp_package_failure_asks_to_start_pc_client(
        self, _create_package, showerror,
    ):
        app = object.__new__(central_server_app.CentralServerApp)

        app.generate_mcp_connection_package()

        showerror.assert_called_once()
        self.assertIn("请先启动 Life Link PC 客户端", showerror.call_args.args[1])

    @mock.patch("central_server_app.central_windows_startup.ensure_default_enabled")
    def test_missing_configuration_is_initialized_without_client_project(self, _startup):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "central" / "config.json"
            identity_path = root / "central" / "server_identity.json"

            selected = central_server_app.ensure_server_configuration(
                config_path, identity_path, port=8097,
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            identity = json.loads(identity_path.read_text(encoding="utf-8"))

            self.assertEqual(selected, config_path.resolve())
            self.assertTrue(identity["device_id"].startswith("central-server-"))
            self.assertIn(identity["device_id"], config["token_bindings"].values())
            self.assertTrue(config["read_token"])
            self.assertEqual(config["port"], 8097)

    @mock.patch("central_server_app.central_windows_startup.ensure_default_enabled")
    def test_existing_configuration_is_preserved(self, _startup):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            payload = {
                "host": "127.0.0.1",
                "port": 8091,
                "database_path": str(root / "data.sqlite3"),
                "token_bindings": {
                    "upload-token-0123456789-ABCDEFGHIJKLMN": "desktop-existing"
                },
                "read_token": "read-token-0123456789-ABCDEFGHIJKLMNOP",
            }
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            central_server_app.ensure_server_configuration(
                config_path, root / "server_identity.json",
            )

            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")), payload,
            )
            identity = json.loads(
                (root / "server_identity.json").read_text(encoding="utf-8")
            )
            self.assertTrue(identity["device_id"].startswith("central-server-"))

if __name__ == "__main__":
    unittest.main()
