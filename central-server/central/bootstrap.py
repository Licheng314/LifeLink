"""Platform-neutral bootstrap helpers for the Life Link central service."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .config import (
    default_config_path,
    default_data_dir,
    legacy_config_path,
    legacy_project_config_path,
)
from .operations import _secure_atomic_write_json, initialize_device


DEFAULT_SERVER_PORT = 8091


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"配置文件不是 JSON 对象：{path}")
    return payload


def ensure_server_configuration(
    config_path: Path | None = None,
    identity_path: Path | None = None,
    *,
    port: int = DEFAULT_SERVER_PORT,
) -> Path:
    """Ensure one stable central identity and an initialized external config."""
    target = (config_path or default_config_path()).expanduser().resolve()
    if not target.exists() and target == default_config_path().expanduser().resolve():
        for legacy in (
            legacy_project_config_path().expanduser().resolve(),
            legacy_config_path().expanduser().resolve(),
        ):
            if legacy != target and legacy.exists():
                _secure_atomic_write_json(target, _read_json(legacy))
                break

    existing = _read_json(target)
    bindings = existing.get("token_bindings")
    read_token = existing.get("read_token")

    selected_identity = identity_path or (default_data_dir() / "server_identity.json")
    identity = _read_json(selected_identity)
    server_id = identity.get("device_id")
    if not isinstance(server_id, str) or not server_id:
        server_id = f"central-server-{uuid.uuid4()}"
        _secure_atomic_write_json(selected_identity, {"device_id": server_id})

    # Existing installations may have complete credentials but predate the
    # separate identity file.  Identity recovery must happen before returning.
    if isinstance(bindings, dict) and bindings and isinstance(read_token, str) and read_token:
        return target

    if isinstance(bindings, dict) and server_id in bindings.values():
        server_id = f"central-server-{uuid.uuid4()}"
        _secure_atomic_write_json(selected_identity, {"device_id": server_id})
    elif identity.get("device_id") != server_id:
        _secure_atomic_write_json(selected_identity, {"device_id": server_id})

    initialize_device(
        device_id=server_id,
        config_path=target,
        host="127.0.0.1",
        port=port,
    )
    return target
