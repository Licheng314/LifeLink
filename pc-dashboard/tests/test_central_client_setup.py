import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import central_client_setup as setup
import start_central_client as starter


UPLOAD_TOKEN = "upload-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
READ_TOKEN = "read-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-extra"


class FakeDesktopProcess:
    def wait(self):
        return 0


class CentralClientSetupTests(unittest.TestCase):
    def test_wait_for_loopback_port_release_returns_when_port_is_free(self):
        with mock.patch.object(starter.socket, "socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.connect_ex.return_value = 10061
            starter.wait_for_loopback_port_release(8090, timeout=0.01)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.identity_path = self.root / "identity.json"
        self.profile_path = self.root / "issued-profile.json"
        self.config_path = self.root / "client" / "config.json"
        self.device_id = f"desktop-{uuid.uuid4()}"
        self.identity_path.write_text(json.dumps({
            "version": 1,
            "device_id": self.device_id,
            "created_at": "2026-08-01T00:00:00Z",
        }), encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def profile(self, **changes):
        payload = {
            "schema_version": "life-radio-client-profile-v1",
            "central_base_url": "https://life-radio.example.test",
            "device": {
                "device_id": self.device_id,
                "platform": "desktop",
                "display_name": "Remote PC",
            },
            "upload_token": UPLOAD_TOKEN,
            "read_token": READ_TOKEN,
            "issued_at": "2026-08-01T00:01:00Z",
        }
        payload.update(changes)
        return payload

    def test_default_config_lives_in_unique_user_data_directory(self):
        self.assertEqual(
            setup.default_client_config_path({"USERPROFILE": str(self.root)}),
            self.root / "LifeLink" / "client" / "config.json",
        )

    def test_default_path_imports_legacy_config_without_losing_credentials(self):
        legacy_path = self.root / "legacy" / "config.json"
        legacy_path.parent.mkdir(parents=True)
        legacy = self.profile()
        legacy["schema_version"] = setup.CLIENT_CONFIG_SCHEMA
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")

        with (
            patch.object(setup, "default_client_config_path", return_value=self.config_path),
            patch.object(
                setup, "legacy_installation_config_path",
                return_value=self.root / "missing-install-config.json",
            ),
            patch.object(setup, "legacy_client_config_path", return_value=legacy_path),
        ):
            setup.ensure_client_config()

        imported = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(imported["upload_token"], UPLOAD_TOKEN)
        self.assertEqual(imported["read_token"], READ_TOKEN)

    def write_profile(self, payload=None):
        self.profile_path.write_text(
            json.dumps(payload or self.profile()), encoding="utf-8",
        )

    def test_claimed_profile_is_identity_bound_and_securely_persisted(self):
        destination = setup.write_client_profile(
            self.profile(),
            config_path=self.config_path,
            identity_path=self.identity_path,
        )
        stored = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual(stored["schema_version"], "life-radio-client-config-v1")
        self.assertEqual(stored["device"]["device_id"], self.device_id)
        self.assertEqual(stored["central_base_url"], "https://life-radio.example.test")
        self.assertEqual(stored["upload_token"], UPLOAD_TOKEN)
        self.assertEqual(stored["read_token"], READ_TOKEN)
        self.assertEqual(stored["port"], 8090)
        self.assertTrue(stored["app_usage_collection_enabled"])

    def test_first_start_creates_one_runtime_config_without_credentials(self):
        destination = setup.ensure_client_config(self.config_path)
        stored = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual(stored["schema_version"], setup.CLIENT_CONFIG_SCHEMA)
        self.assertEqual(stored["port"], 8090)
        self.assertIn("tianditu_key", stored)
        self.assertNotIn("upload_token", stored)
        self.assertNotIn("read_token", stored)

    def test_existing_paired_config_is_migrated_without_losing_credentials(self):
        legacy = self.profile()
        legacy["schema_version"] = setup.CLIENT_CONFIG_SCHEMA
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps(legacy), encoding="utf-8")

        setup.ensure_client_config(self.config_path)
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))

        self.assertEqual(stored["upload_token"], UPLOAD_TOKEN)
        self.assertEqual(stored["read_token"], READ_TOKEN)
        self.assertNotIn("activitywatch_url", stored)
        self.assertTrue(stored["app_usage_collection_enabled"])

    def test_pairing_preserves_user_runtime_settings(self):
        setup.ensure_client_config(self.config_path)
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        stored.update({
            "port": 9180,
            "activitywatch_url": "http://127.0.0.1:5610/api/0",
            "app_usage_collection_enabled": False,
            "tianditu_key": "user-map-key",
        })
        self.config_path.write_text(json.dumps(stored), encoding="utf-8")

        setup.write_client_profile(
            self.profile(),
            config_path=self.config_path,
            identity_path=self.identity_path,
        )
        paired = json.loads(self.config_path.read_text(encoding="utf-8"))

        self.assertEqual(paired["port"], 9180)
        self.assertNotIn("activitywatch_url", paired)
        self.assertFalse(paired["app_usage_collection_enabled"])
        self.assertEqual(paired["tianditu_key"], "user-map-key")
        self.assertEqual(paired["upload_token"], UPLOAD_TOKEN)

    def test_invalid_existing_config_is_not_overwritten(self):
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "valid UTF-8 JSON"):
            setup.ensure_client_config(self.config_path)

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), "{broken")

    def test_mismatched_profile_is_rejected_without_writing_config(self):
        payload = self.profile()
        payload["device"]["device_id"] = f"desktop-{uuid.uuid4()}"
        with self.assertRaisesRegex(ValueError, "does not match"):
            setup.write_client_profile(
                payload,
                config_path=self.config_path,
                identity_path=self.identity_path,
            )
        self.assertFalse(self.config_path.exists())

    def test_https_is_required_except_for_production_loopback(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            setup.validate_central_base_url("http://central.example.test")
        self.assertEqual(
            setup.validate_central_base_url("http://127.0.0.1:8091"),
            "http://127.0.0.1:8091",
        )
        self.assertEqual(
            setup.validate_central_base_url(
                "http://127.0.0.1:8091", allow_loopback_http=True,
            ),
            "http://127.0.0.1:8091",
        )
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            setup.validate_central_base_url(
                "http://central.example.test", allow_loopback_http=True,
            )

    def test_optional_read_token_does_not_inherit_a_stale_secret(self):
        profile = self.profile()
        profile.pop("read_token")

        environment = starter.client_environment(profile, {
            "LIFE_RADIO_CENTRAL_READ_TOKEN": "stale-secret",
        })

        self.assertNotIn("LIFE_RADIO_CENTRAL_READ_TOKEN", environment)
        self.assertEqual(environment["LIFE_RADIO_CENTRAL_TOKEN"], UPLOAD_TOKEN)

    def test_remote_start_launches_desktop_only_with_private_environment(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return FakeDesktopProcess()

        result = starter.start_desktop_client(self.profile(), popen=fake_popen)

        self.assertEqual(result, 0)
        self.assertEqual(Path(captured["command"][1]).name, "desktop_app.py")
        self.assertNotIn("central_server.py", " ".join(captured["command"]))
        environment = captured["kwargs"]["env"]
        self.assertEqual(environment["LIFE_RADIO_HOST"], "127.0.0.1")
        self.assertNotIn("LIFE_RADIO_RUNTIME_ROLE", environment)
        self.assertEqual(environment["LIFE_RADIO_CENTRAL_TOKEN"], UPLOAD_TOKEN)
        self.assertEqual(environment["LIFE_RADIO_CENTRAL_READ_TOKEN"], READ_TOKEN)

    def test_background_start_keeps_the_client_in_the_system_tray(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["kwargs"] = kwargs
            return FakeDesktopProcess()

        starter.start_desktop_client(
            self.profile(), background_start=True, popen=fake_popen,
        )

        environment = captured["kwargs"]["env"]
        self.assertEqual(environment["LIFE_RADIO_NO_BROWSER"], "true")
        self.assertEqual(environment["LIFE_RADIO_BACKGROUND_START"], "true")

    def test_remote_start_rejects_existing_incompatible_dashboard(self):
        with self.assertRaisesRegex(RuntimeError, "不兼容"):
            starter.ensure_dashboard_mode_is_compatible(
                8090, mode_reader=lambda port: "incompatible",
            )

    def test_remote_start_allows_existing_current_or_empty_dashboard_port(self):
        for mode in ("current", None):
            with self.subTest(mode=mode):
                self.assertEqual(
                    starter.ensure_dashboard_mode_is_compatible(
                        8090, mode_reader=lambda port, value=mode: value,
                    ),
                    mode,
                )

    def test_launcher_help_uses_life_link_branding(self):
        self.assertEqual(
            starter.build_parser().description,
            "Start Life Link as a central PC client",
        )

    def test_launcher_reports_incompatible_mode_error_instead_of_crashing(self):
        with (
            patch.object(starter, "migrate_legacy_installation_client_state"),
            patch.object(starter, "migrate_legacy_appdata_client_state"),
            patch.object(starter, "migrate_presplit_client_state"),
            patch.object(starter, "load_client_config", return_value=self.profile()),
            patch.object(starter, "dashboard_port", return_value=8090),
            patch.object(
                starter,
                "ensure_dashboard_mode_is_compatible",
                side_effect=RuntimeError("incompatible dashboard is running"),
            ),
            patch.object(starter, "show_error") as show_error,
        ):
            result = starter.main([])

        self.assertEqual(result, 2)
        show_error.assert_called_once_with("incompatible dashboard is running")

    def test_missing_or_invalid_config_runs_setup_then_starts_client(self):
        profile = self.profile()
        with (
            patch.object(starter, "migrate_legacy_installation_client_state"),
            patch.object(starter, "migrate_legacy_appdata_client_state"),
            patch.object(starter, "migrate_presplit_client_state"),
            patch.object(
                starter, "load_client_config", side_effect=ValueError("missing config"),
            ),
            patch.object(starter, "run_setup_only", return_value=profile) as setup_only,
            patch.object(starter, "dashboard_port", return_value=8090),
            patch.object(starter, "wait_for_loopback_port_release") as wait_for_release,
            patch.object(starter, "ensure_dashboard_mode_is_compatible") as compatible,
            patch.object(starter, "start_desktop_client", return_value=0) as start_client,
            patch.object(starter, "show_error") as show_error,
        ):
            result = starter.main([
                "--config", str(self.config_path),
                "--identity", str(self.identity_path),
            ])

        self.assertEqual(result, 0)
        setup_only.assert_called_once()
        wait_for_release.assert_called_once_with(8090)
        compatible.assert_called_once_with(8090)
        start_client.assert_called_once_with(profile)
        show_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
