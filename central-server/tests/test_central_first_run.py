"""Focused tests for the source-checkout first-run guide."""

from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from unittest import mock

import central_first_run as setup


class CentralFirstRunTests(unittest.TestCase):
    def test_complete_marker_is_explicit_and_versioned(self) -> None:
        self.assertFalse(setup.setup_complete({}))
        self.assertFalse(setup.setup_complete({"setup": {"version": 0}}))
        self.assertTrue(setup.setup_complete({"setup": {"version": 1}}))

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

    @mock.patch.object(setup, "create_client_invitation")
    @mock.patch.object(setup, "_paired_device_count", return_value=1)
    def test_existing_device_prevents_new_pairing_code(self, _count, create) -> None:
        setup._offer_first_invitation(mock.Mock(), "https://central.example")
        create.assert_not_called()

    @mock.patch.object(setup, "configure_tailscale")
    @mock.patch.object(setup, "_existing_endpoint", return_value="https://central.example")
    def test_existing_endpoint_is_kept_by_default(self, _endpoint, tailscale) -> None:
        result = setup.configure_connection(
            8091, ask=lambda _prompt: "", previous_provider="tailscale",
        )
        self.assertEqual(result, ("tailscale", "https://central.example"))
        tailscale.assert_not_called()

    @mock.patch.object(setup, "configure_tailscale", return_value="https://new.example")
    @mock.patch.object(setup, "_existing_endpoint", return_value="https://old.example")
    def test_existing_endpoint_can_be_refreshed_with_tailscale(
        self, _endpoint, tailscale,
    ) -> None:
        result = setup.configure_connection(
            8091, ask=lambda _prompt: "2", previous_provider="tailscale",
        )
        self.assertEqual(result, ("tailscale", "https://new.example"))
        tailscale.assert_called_once_with(central_port=8091, previous_central_port=None)

    @mock.patch.object(setup, "configure_tailscale", return_value="https://new.example")
    @mock.patch.object(setup, "_existing_endpoint", return_value=None)
    def test_missing_endpoint_requires_a_remote_choice(self, _endpoint, tailscale) -> None:
        answers = iter(["", "3", "1"])
        result = setup.configure_connection(8091, ask=lambda _prompt: next(answers))
        self.assertEqual(result, ("tailscale", "https://new.example"))
        tailscale.assert_called_once()

    @mock.patch.object(setup, "save_endpoint")
    @mock.patch.object(setup, "read_token", return_value="x" * 32)
    @mock.patch.object(
        setup, "probe_endpoint", return_value={"base_url": "https://fallback.example"},
    )
    @mock.patch.object(
        setup, "configure_tailscale",
        side_effect=setup.TailscaleSetupError("not ready"),
    )
    @mock.patch.object(setup, "_existing_endpoint", return_value="https://old.example")
    def test_failed_refresh_returns_to_menu_and_preserves_old_until_success(
        self, _endpoint, _tailscale, _probe, _token, save,
    ) -> None:
        answers = iter(["2", "3", "https://fallback.example"])
        result = setup.configure_connection(
            8091, ask=lambda _prompt: next(answers), previous_provider="tailscale",
        )
        self.assertEqual(result, ("https_tunnel", "https://fallback.example"))
        save.assert_called_once_with(
            setup.default_endpoint_path(), "peanuthull", "https://fallback.example",
        )

    def test_completed_install_still_checks_connection(self) -> None:
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
                mock.patch.object(setup.CentralConfig, "from_environment", return_value=mock.Mock()),
                mock.patch.object(
                    setup, "configure_connection",
                    return_value=("tailscale", "https://central.example"),
                ) as configure,
            ):
                result = setup.run(
                    root / "LifeLink Central Service.exe",
                    ask=lambda _prompt: "",
                )
            self.assertEqual(result, 0)
            configure.assert_called_once_with(
                8091,
                ask=mock.ANY,
                force_reconfigure=False,
                previous_port=8091,
                previous_provider="tailscale",
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["setup"], {"version": 1, "remote_mode": "tailscale"})

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
