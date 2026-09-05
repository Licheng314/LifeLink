"""Focused tests for the source-checkout first-run guide."""

from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from unittest import mock

import central_first_run as setup


class CentralFirstRunTests(unittest.TestCase):
    @mock.patch.object(setup, "first_free_port", return_value=8094)
    @mock.patch.object(setup, "port_state", return_value="occupied")
    def test_fresh_install_switches_from_occupied_default_automatically(
        self, _state, _free,
    ) -> None:
        self.assertEqual(setup.select_central_port({}), (8094, True))

    @mock.patch.object(setup, "first_free_port", return_value=8094)
    @mock.patch.object(setup, "port_state", return_value="occupied")
    def test_existing_install_requires_consent_before_port_change(
        self, _state, _free,
    ) -> None:
        selected = setup.select_central_port(
            {"port": 8091}, ask=lambda _prompt: "y",
        )
        self.assertEqual(selected, (8094, True))

    @mock.patch.object(setup, "first_free_port", return_value=8094)
    @mock.patch.object(setup, "port_state", return_value="occupied")
    def test_existing_install_can_refuse_port_change(self, _state, _free) -> None:
        with self.assertRaisesRegex(setup.SetupError, "端口冲突"):
            setup.select_central_port({"port": 8091}, ask=lambda _prompt: "n")

    def test_run_opens_management_webui_without_forcing_remote_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "port": 8091,
                "setup": {"version": 1, "remote_mode": "tailscale"},
                "public_endpoint": {
                    "provider": "tailscale",
                    "base_url": "https://central.example",
                },
            }), encoding="utf-8")
            with (
                mock.patch.object(setup, "default_config_path", return_value=config_path),
                mock.patch.object(setup, "select_central_port", return_value=(8091, False)),
                mock.patch.object(setup, "ensure_server_configuration", return_value=config_path),
                mock.patch.object(setup, "_start_launcher"),
                mock.patch.object(setup.webbrowser, "open", return_value=True) as open_browser,
            ):
                result = setup.run(
                    root / "LifeLink Central Service.exe",
                    ask=lambda _prompt: "",
                )
            self.assertEqual(result, 0)
            open_browser.assert_called_once_with(setup.MANAGEMENT_URL)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["setup"], {"version": 1, "remote_mode": "tailscale"})
            self.assertEqual(
                saved["public_endpoint"]["base_url"], "https://central.example",
            )

    def test_management_probe_requires_the_exact_management_role(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "status": "ok", "role": "life-link-central-management",
        }).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(setup, "build_opener", return_value=opener):
            self.assertTrue(setup.management_is_ready())

        response.__enter__.return_value.read.return_value = json.dumps({
            "status": "ok", "role": "central",
        }).encode("utf-8")
        with mock.patch.object(setup, "build_opener", return_value=opener):
            self.assertFalse(setup.management_is_ready())

    def test_loopback_pc_profile_tracks_confirmed_port_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "client" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({
                "central_base_url": "http://127.0.0.1:8091",
                "upload_token": "preserved-secret",
            }), encoding="utf-8")
            with mock.patch.object(setup, "default_data_dir", return_value=root / "central"):
                changed = setup._update_local_pc_endpoint(8091, 8092)
            payload = json.loads(config.read_text(encoding="utf-8"))
            self.assertTrue(changed)
            self.assertEqual(payload["central_base_url"], "http://127.0.0.1:8092")
            self.assertEqual(payload["upload_token"], "preserved-secret")


if __name__ == "__main__":
    unittest.main()
