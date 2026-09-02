"""Local initialization helpers for the central service."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import (
    _read_external_config,
    _read_token,
    _token_bindings,
    default_config_path,
)


@dataclass(frozen=True)
class InitializationResult:
    config_path: Path
    device_id: str
    created_config: bool
    created_read_token: bool


def initialize_device(
    *,
    device_id: str,
    config_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8091,
    database_path: Path | None = None,
) -> InitializationResult:
    """Create an external config or add one new device credential to it.

    The generated token is deliberately not returned or printed. It exists
    only in the user-selected external configuration file.
    """
    normalized_device_id = device_id.strip()
    if not normalized_device_id or len(normalized_device_id) > 200:
        raise ValueError("device_id must contain 1 to 200 characters")
    if not host:
        raise ValueError("host must not be empty")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")

    target = (config_path or default_config_path()).expanduser().resolve()
    created_config = not target.exists()
    existing = _read_external_config(str(target)) if target.exists() else {}
    bindings = _token_bindings(existing.get("token_bindings"), source="external config")
    if normalized_device_id in bindings.values():
        raise ValueError(
            "device_id is already configured; refusing to replace its token implicitly"
        )

    token = secrets.token_urlsafe(32)
    while token in bindings:
        token = secrets.token_urlsafe(32)
    bindings[token] = normalized_device_id

    read_token = _read_token(existing.get("read_token"), source="external config")
    created_read_token = read_token is None
    if read_token is None:
        read_token = secrets.token_urlsafe(32)
        while read_token in bindings:
            read_token = secrets.token_urlsafe(32)
    elif read_token in bindings:
        raise ValueError("read token must be distinct from every device token")

    selected_database = database_path
    if selected_database is None and existing.get("database_path"):
        selected_database = Path(str(existing["database_path"]))
        if not selected_database.is_absolute():
            selected_database = target.parent / selected_database
    if selected_database is None:
        selected_database = target.parent / "life_radio.sqlite3"

    payload = dict(existing)
    payload.update(
        {
            "host": str(existing.get("host") or host),
            "port": int(existing.get("port") or port),
            "database_path": str(selected_database.expanduser().resolve()),
            "token_bindings": bindings,
            "read_token": read_token,
        }
    )
    _secure_atomic_write_json(target, payload)
    return InitializationResult(
        config_path=target,
        device_id=normalized_device_id,
        created_config=created_config,
        created_read_token=created_read_token,
    )


def _secure_atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
