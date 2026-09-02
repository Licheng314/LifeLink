import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from device_identity import (
    IdentityError,
    default_client_data_dir,
    device_descriptor,
    load_or_create_identity,
    migrate_legacy_appdata_client_state,
    migrate_legacy_installation_client_state,
    migrate_presplit_client_state,
)


class DeviceIdentityTests(unittest.TestCase):
    def test_default_path_is_stable_across_installations(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                default_client_data_dir({"USERPROFILE": directory}),
                Path(directory) / "LifeLink" / "client",
            )

    def test_split_client_directory_owns_identity_and_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"LIFE_LINK_RUNTIME_ROOT": directory}
            self.assertEqual(
                default_client_data_dir(environment),
                Path(directory) / "client",
            )

    def test_old_appdata_identity_moves_before_a_new_identity_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "LIFE_LINK_RUNTIME_ROOT": str(root / "project-runtime"),
                "LOCALAPPDATA": str(root / "local-app-data"),
            }
            legacy = root / "local-app-data" / "LifeRadio" / "client"
            legacy.mkdir(parents=True)
            (legacy / "identity.json").write_bytes(b"old-stable-identity")
            (legacy / "outbox.sqlite3").write_bytes(b"old-outbox")

            result = migrate_legacy_appdata_client_state(environment)
            destination = root / "project-runtime" / "client"

            self.assertEqual(len(result["moved"]), 2)
            self.assertEqual((destination / "identity.json").read_bytes(), b"old-stable-identity")
            self.assertEqual((destination / "outbox.sqlite3").read_bytes(), b"old-outbox")
            self.assertFalse(legacy.joinpath("identity.json").exists())

    def test_installation_local_identity_moves_to_unique_user_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "install" / "runtime" / "client"
            destination = root / "user-data" / "client"
            source.mkdir(parents=True)
            (source / "identity.json").write_bytes(b"stable-identity")
            with mock.patch(
                "device_identity.legacy_installation_client_data_dir",
                return_value=source,
            ):
                result = migrate_legacy_installation_client_state(
                    {"LIFE_LINK_DATA_ROOT": str(root / "user-data")},
                )
            self.assertEqual(len(result["moved"]), 1)
            self.assertEqual((destination / "identity.json").read_bytes(), b"stable-identity")

    def test_existing_project_identity_wins_over_stale_appdata_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "LIFE_LINK_RUNTIME_ROOT": str(root / "project-runtime"),
                "LOCALAPPDATA": str(root / "local-app-data"),
            }
            legacy = root / "local-app-data" / "LifeRadio" / "client"
            current = root / "project-runtime" / "client"
            legacy.mkdir(parents=True)
            current.mkdir(parents=True)
            (legacy / "identity.json").write_bytes(b"old-identity")
            (current / "identity.json").write_bytes(b"current-identity")

            result = migrate_legacy_appdata_client_state(environment)

            self.assertEqual(result["moved"], [])
            self.assertEqual(result["skipped_conflicts"], [str(legacy / "identity.json")])
            self.assertEqual((current / "identity.json").read_bytes(), b"current-identity")
            self.assertEqual((legacy / "identity.json").read_bytes(), b"old-identity")

    def test_legacy_identity_and_sqlite_files_move_together(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"LIFE_LINK_RUNTIME_ROOT": directory}
            legacy = Path(directory)
            legacy.mkdir(parents=True, exist_ok=True)
            payloads = {
                "identity.json": b'{"device_id":"desktop-legacy"}',
                "outbox.sqlite3": b"sqlite-main",
                "outbox.sqlite3-wal": b"sqlite-wal",
                "outbox.sqlite3-shm": b"sqlite-shm",
            }
            for name, content in payloads.items():
                (legacy / name).write_bytes(content)

            result = migrate_presplit_client_state(environment)
            destination = legacy / "client"

            self.assertEqual(len(result["moved"]), 4)
            for name, content in payloads.items():
                self.assertFalse((legacy / name).exists())
                self.assertEqual((destination / name).read_bytes(), content)

    def test_conflicting_split_state_refuses_before_moving_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"LIFE_LINK_RUNTIME_ROOT": directory}
            legacy = Path(directory)
            destination = legacy / "client"
            destination.mkdir(parents=True)
            (legacy / "identity.json").write_bytes(b"legacy-identity")
            (legacy / "outbox.sqlite3").write_bytes(b"legacy-outbox")
            (destination / "outbox.sqlite3").write_bytes(b"different-outbox")

            with self.assertRaises(IdentityError):
                migrate_presplit_client_state(environment)

            self.assertTrue((legacy / "identity.json").exists())
            self.assertFalse((destination / "identity.json").exists())

    def test_identity_is_stable_and_display_name_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"

            first = device_descriptor(
                display_name="Office PC", identity_path=path,
            )
            second = device_descriptor(
                display_name="Renamed PC", identity_path=path,
            )

            self.assertTrue(first["device_id"].startswith("desktop-"))
            self.assertEqual(second["device_id"], first["device_id"])
            self.assertEqual(first["display_name"], "Office PC")
            self.assertEqual(second["display_name"], "Renamed PC")
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["device_id"], first["device_id"])
            self.assertNotIn("Office PC", path.read_text(encoding="utf-8"))

    def test_hostname_shaped_identity_is_rejected_instead_of_cloned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            path.write_text(
                json.dumps({"device_id": "activitywatch:DESKTOP-A"}),
                encoding="utf-8",
            )

            with self.assertRaises(IdentityError):
                load_or_create_identity(path)


if __name__ == "__main__":
    unittest.main()
