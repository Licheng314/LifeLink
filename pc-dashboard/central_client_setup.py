#!/usr/bin/env python3
"""Validate and securely persist a claimed central-client profile."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from device_identity import device_descriptor
import pc_windows_startup
from runtime_paths import client_data_dir, installation_dir, is_frozen, resource_dir


PROFILE_SCHEMA = "life-radio-client-profile-v1"
CLIENT_CONFIG_SCHEMA = "life-radio-client-config-v1"
DEFAULT_TIANDITU_KEY = "dce95eafa58322a32d41814ef8e31d25"
CLIENT_RUNTIME_DEFAULTS: dict[str, Any] = {
    "port": 8090,
    "app_usage_collection_enabled": True,
    "tianditu_key": DEFAULT_TIANDITU_KEY,
}


def default_client_config_path(environ: Mapping[str, str] | None = None) -> Path:
    return client_data_dir(environ) / "config.json"


def legacy_installation_config_path() -> Path:
    if is_frozen():
        return installation_dir() / "config.json"
    return resource_dir() / "config.json"


def legacy_client_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LifeRadio" / "client" / "config.json"
    return Path.home() / ".life-radio" / "client" / "config.json"


def _validate_utc_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} must be a UTC ISO-8601 timestamp ending in Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{field} must be a valid UTC ISO-8601 timestamp") from error
    return value


def _validate_token(value: Any, field: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{field} must be a non-empty token without surrounding whitespace")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field} must not contain whitespace")
    if len(value) < 32 or len(set(value)) < 8:
        raise ValueError(f"{field} must contain at least 32 high-entropy characters")
    return value


def validate_central_base_url(
    value: Any,
    *,
    allow_loopback_http: bool = False,
) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("central_base_url must be a non-empty absolute URL")
    parsed = urlparse(value)
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("central_base_url must be an absolute server URL without credentials or query")
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        pass
    else:
        raise ValueError(
            "central_base_url must use HTTPS; HTTP is allowed only for local loopback"
        )
    return value.rstrip("/")


def validate_client_profile(
    profile: Any,
    *,
    local_device_id: str,
    allow_loopback_http: bool = False,
) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("client profile must contain a JSON object")
    if profile.get("schema_version") != PROFILE_SCHEMA:
        raise ValueError(f"client profile schema_version must be {PROFILE_SCHEMA}")
    device = profile.get("device")
    if not isinstance(device, dict):
        raise ValueError("client profile device must be an object")
    device_id = device.get("device_id")
    if device_id != local_device_id:
        raise ValueError(
            "client profile device_id does not match this PC installation identity"
        )
    if device.get("platform") != "desktop":
        raise ValueError("client profile device.platform must be desktop")
    display_name = device.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("client profile device.display_name must not be empty")
    base_url = validate_central_base_url(
        profile.get("central_base_url"),
        allow_loopback_http=allow_loopback_http,
    )
    upload_token = _validate_token(profile.get("upload_token"), "upload_token")
    read_token = profile.get("read_token")
    if read_token is not None:
        read_token = _validate_token(read_token, "read_token")
        if secrets.compare_digest(read_token, upload_token):
            raise ValueError("read_token must be distinct from upload_token")
    issued_at = _validate_utc_timestamp(profile.get("issued_at"), "issued_at")
    normalized = {
        "schema_version": PROFILE_SCHEMA,
        "central_base_url": base_url,
        "device": {
            "device_id": local_device_id,
            "platform": "desktop",
            "display_name": display_name.strip(),
        },
        "upload_token": upload_token,
        "issued_at": issued_at,
    }
    if read_token is not None:
        normalized["read_token"] = read_token
    return normalized


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"file does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"file is not valid UTF-8 JSON: {path}") from error


def _validate_runtime_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("client config must contain a JSON object")
    if payload.get("schema_version") != CLIENT_CONFIG_SCHEMA:
        raise ValueError(f"client config schema_version must be {CLIENT_CONFIG_SCHEMA}")
    normalized = dict(CLIENT_RUNTIME_DEFAULTS)
    try:
        port = int(payload.get("port", normalized["port"]))
    except (TypeError, ValueError) as error:
        raise ValueError("client port must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("client port must be between 1 and 65535")
    normalized["port"] = port
    collection_enabled = payload.get(
        "app_usage_collection_enabled",
        normalized["app_usage_collection_enabled"],
    )
    if not isinstance(collection_enabled, bool):
        raise ValueError("app_usage_collection_enabled must be a boolean")
    normalized["app_usage_collection_enabled"] = collection_enabled
    map_key = payload.get("tianditu_key", normalized["tianditu_key"])
    if not isinstance(map_key, str) or map_key != map_key.strip():
        raise ValueError("tianditu_key must be a string without surrounding whitespace")
    normalized["tianditu_key"] = map_key
    return normalized


def _secure_atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def ensure_client_config(config_path: Path | None = None) -> Path:
    """Create the one runtime config, or add newly introduced public defaults."""
    destination = (config_path or default_client_config_path()).expanduser().resolve()
    if (
        not destination.exists()
        and destination == default_client_config_path().expanduser().resolve()
    ):
        candidates = (
            legacy_installation_config_path().expanduser().resolve(),
            legacy_client_config_path().expanduser().resolve(),
        )
        for legacy in candidates:
            if legacy != destination and legacy.exists():
                _secure_atomic_write_json(destination, _read_json(legacy))
                break
    if not destination.exists():
        payload = {
            "schema_version": CLIENT_CONFIG_SCHEMA,
            **CLIENT_RUNTIME_DEFAULTS,
        }
        _secure_atomic_write_json(destination, payload)
        return destination
    raw = _read_json(destination)
    settings = _validate_runtime_settings(raw)
    missing = [key for key in CLIENT_RUNTIME_DEFAULTS if key not in raw]
    obsolete = [key for key in ("activitywatch_url",) if key in raw]
    if missing or obsolete:
        migrated = {key: value for key, value in raw.items() if key not in obsolete}
        migrated.update(settings)
        _secure_atomic_write_json(destination, migrated)
    return destination


def load_client_runtime_config(config_path: Path | None = None) -> dict[str, Any]:
    path = ensure_client_config(config_path)
    return _validate_runtime_settings(_read_json(path))


def read_client_runtime_config(config_path: Path | None = None) -> dict[str, Any]:
    """Read runtime settings without creating or migrating files."""
    path = (config_path or default_client_config_path()).expanduser().resolve()
    if not path.exists():
        return dict(CLIENT_RUNTIME_DEFAULTS)
    return _validate_runtime_settings(_read_json(path))


def write_client_profile(
    profile: Any,
    *,
    config_path: Path | None = None,
    identity_path: Path | str | None = None,
    allow_loopback_http: bool = False,
) -> Path:
    """Validate and atomically persist a host-issued client profile."""
    local = device_descriptor(identity_path=identity_path)
    normalized = validate_client_profile(
        profile,
        local_device_id=local["device_id"],
        allow_loopback_http=allow_loopback_http,
    )
    destination = (config_path or default_client_config_path()).expanduser().resolve()
    ensure_client_config(destination)
    runtime_settings = load_client_runtime_config(destination)
    client_config = {
        "schema_version": CLIENT_CONFIG_SCHEMA,
        **runtime_settings,
        **normalized,
    }
    client_config["schema_version"] = CLIENT_CONFIG_SCHEMA
    _secure_atomic_write_json(destination, client_config)
    # Pairing is the PC installation's completed initialization point. This is
    # intentionally best-effort: a profile must remain valid even if Windows
    # startup registration is unavailable or blocked by policy.
    try:
        pc_windows_startup.ensure_default_enabled()
    except OSError:
        pass
    return destination


def load_client_config(
    config_path: Path | None = None,
    *,
    identity_path: Path | str | None = None,
    allow_loopback_http: bool = False,
) -> dict[str, Any]:
    local = device_descriptor(identity_path=identity_path)
    path = ensure_client_config(config_path)
    raw = _read_json(path)
    if not isinstance(raw, dict) or raw.get("schema_version") != CLIENT_CONFIG_SCHEMA:
        raise ValueError(f"client config schema_version must be {CLIENT_CONFIG_SCHEMA}")
    runtime_settings = _validate_runtime_settings(raw)
    profile = dict(raw)
    profile["schema_version"] = PROFILE_SCHEMA
    normalized = validate_client_profile(
        profile,
        local_device_id=local["device_id"],
        allow_loopback_http=allow_loopback_http,
    )
    normalized.update(runtime_settings)
    return normalized
