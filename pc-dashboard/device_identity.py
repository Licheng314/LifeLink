"""Per-Windows-user identity for the Life Link desktop client.

The identity lives outside every source checkout or portable application folder.
Copying or upgrading program files must never clone or redefine the PC identity.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_paths import client_data_dir, installation_dir


IDENTITY_VERSION = 1
CLIENT_DIRECTORY_NAME = "client"


class IdentityError(RuntimeError):
    """Raised when a persisted identity cannot be safely used."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_client_data_dir(environ: dict[str, str] | None = None) -> Path:
    return client_data_dir(environ)


def legacy_installation_client_data_dir() -> Path:
    return installation_dir() / "runtime" / CLIENT_DIRECTORY_NAME


def legacy_client_data_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LifeRadio" / CLIENT_DIRECTORY_NAME
    return Path.home() / "AppData" / "Local" / "LifeRadio" / CLIENT_DIRECTORY_NAME


def migrate_legacy_appdata_client_state(
    environ: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Move the pre-project AppData identity/outbox into project runtime.

    A current project identity always wins. This matters after a user has
    already re-paired: silently replacing that live identity with the stale
    AppData identity would split the device a second time. The legacy files
    are therefore left untouched for manual recovery/audit in that case.
    """
    source_dir = legacy_client_data_dir(environ)
    destination_dir = default_client_data_dir(environ)
    destination_identity = destination_dir / "identity.json"
    source_identity = source_dir / "identity.json"
    if destination_identity.exists():
        skipped = (
            [str(source_identity)]
            if source_identity.exists()
            and source_identity.read_bytes() != destination_identity.read_bytes()
            else []
        )
        return {"moved": [], "removed_duplicates": [], "skipped_conflicts": skipped}
    result = _migrate_client_state(
        source_dir, destination_dir, conflict_label="Legacy AppData",
    )
    result["skipped_conflicts"] = []
    return result


def migrate_legacy_installation_client_state(
    environ: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Move state from the checkout/portable folder into the per-user root."""
    source_dir = legacy_installation_client_data_dir()
    destination_dir = default_client_data_dir(environ)
    destination_identity = destination_dir / "identity.json"
    source_identity = source_dir / "identity.json"
    if source_dir.resolve() == destination_dir.resolve():
        return {"moved": [], "removed_duplicates": [], "skipped_conflicts": []}
    if destination_identity.exists():
        skipped = (
            [str(source_identity)]
            if source_identity.exists()
            and source_identity.read_bytes() != destination_identity.read_bytes()
            else []
        )
        return {"moved": [], "removed_duplicates": [], "skipped_conflicts": skipped}
    result = _migrate_client_state(
        source_dir, destination_dir, conflict_label="Installation-local",
    )
    result["skipped_conflicts"] = []
    return result


def default_identity_path() -> Path:
    return default_client_data_dir() / "identity.json"


def presplit_client_data_dir(environ: dict[str, str] | None = None) -> Path:
    return default_client_data_dir(environ).parent


def migrate_presplit_client_state(
    environ: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Move pre-split client identity/outbox files into the client directory.

    The caller must invoke this before opening the SQLite outbox. Existing
    destination files are never overwritten or merged implicitly.
    """
    return _migrate_client_state(
        presplit_client_data_dir(environ),
        default_client_data_dir(environ),
        conflict_label="Pre-split",
    )


def _migrate_client_state(
    source_dir: Path,
    destination_dir: Path,
    *,
    conflict_label: str,
) -> dict[str, list[str]]:
    moved: list[str] = []
    removed_duplicates: list[str] = []
    names = (
        "identity.json",
        "outbox.sqlite3",
        "outbox.sqlite3-wal",
        "outbox.sqlite3-shm",
    )
    source_files = [source_dir / name for name in names]
    if not any(path.exists() for path in source_files):
        return {"moved": moved, "removed_duplicates": removed_duplicates}
    destination_dir.mkdir(parents=True, exist_ok=True)
    for source in source_files:
        destination = destination_dir / source.name
        if (
            source.exists()
            and destination.exists()
            and source.read_bytes() != destination.read_bytes()
        ):
            raise IdentityError(
                f"{conflict_label} and current client state both exist with different content; "
                f"refusing to overwrite either file: {source} / {destination}"
            )
    copied: list[tuple[Path, Path]] = []
    for source in source_files:
        if not source.exists():
            continue
        destination = destination_dir / source.name
        if destination.exists():
            continue
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".migrating", dir=destination_dir,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        copied.append((source, destination))
        moved.append(str(destination))
    # Keep every source intact until all copies have completed. A cross-volume
    # interruption can then be retried without losing identity or SQLite WAL.
    for source in source_files:
        if not source.exists():
            continue
        destination = destination_dir / source.name
        if destination.exists() and source.read_bytes() == destination.read_bytes():
            source.unlink()
            if (source, destination) not in copied:
                removed_duplicates.append(str(source))
    return {"moved": moved, "removed_duplicates": removed_duplicates}


def is_valid_device_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("desktop-"):
        return False
    try:
        parsed = uuid.UUID(value.removeprefix("desktop-"))
    except (ValueError, AttributeError):
        return False
    return value == f"desktop-{parsed}"


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def load_or_create_identity(path: Path | str | None = None) -> dict[str, str | int]:
    identity_path = Path(path) if path is not None else default_identity_path()
    if identity_path.exists():
        try:
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IdentityError(f"Identity file is unreadable: {identity_path}") from error
        if not isinstance(payload, dict) or not is_valid_device_id(payload.get("device_id")):
            raise IdentityError(
                "Identity file does not contain an installation-scoped "
                f"desktop UUID: {identity_path}"
            )
        return {
            "version": int(payload.get("version", IDENTITY_VERSION)),
            "device_id": str(payload["device_id"]),
            "created_at": str(payload.get("created_at") or ""),
        }

    identity: dict[str, str | int] = {
        "version": IDENTITY_VERSION,
        "device_id": f"desktop-{uuid.uuid4()}",
        "created_at": utc_timestamp(),
    }
    _atomic_write_json(identity_path, identity)
    return identity


def device_descriptor(
    *,
    display_name: str | None = None,
    identity_path: Path | str | None = None,
) -> dict[str, str]:
    identity = load_or_create_identity(identity_path)
    name = str(display_name or socket.gethostname()).strip()
    return {
        "device_id": str(identity["device_id"]),
        "platform": "desktop",
        "display_name": name or "Life Link PC",
    }
