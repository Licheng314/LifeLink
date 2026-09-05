"""Configuration for the independent central context service."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("LIFE_LINK_DATA_ROOT") or env.get("LIFE_LINK_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser() / "central"
    profile = env.get("USERPROFILE")
    return (Path(profile) if profile else Path.home()) / "LifeLink" / "central"


def legacy_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LifeRadio" / "central"
    return Path.home() / ".life-radio" / "central"


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    return default_data_dir(environ) / "config.json"


def legacy_project_data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "runtime" / "central"


def legacy_project_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.json"


def legacy_config_path(environ: Mapping[str, str] | None = None) -> Path:
    return legacy_data_dir(environ) / "config.json"


DEFAULT_DATABASE_PATH = default_data_dir() / "life_radio.sqlite3"
DEFAULT_TIANDITU_KEY = "dce95eafa58322a32d41814ef8e31d25"


def _read_external_config(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    config_path = Path(path).expanduser()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"central config file does not exist: {config_path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"central config file is invalid: {config_path}") from error
    if not isinstance(payload, dict):
        raise ValueError("central config file must contain a JSON object")
    return payload


def _token_bindings(value: object, *, source: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{source} token_bindings must be a JSON object")
    bindings: dict[str, str] = {}
    for token, device_id in value.items():
        _validate_secret_token(token, source=source, name="device token")
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError(f"{source} contains an empty device_id")
        bindings[token] = device_id.strip()
    return bindings


def _validate_secret_token(value: object, *, source: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source} contains an empty {name}")
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError(f"{source} {name} must not contain whitespace")
    if len(value) < 32 or len(set(value)) < 8:
        raise ValueError(
            f"{source} contains a weak {name}; use at least 32 high-entropy characters"
        )
    return value


def _read_token(value: object, *, source: str) -> str | None:
    if value is None:
        return None
    return _validate_secret_token(value, source=source, name="read token")


@dataclass(frozen=True)
class CentralConfig:
    """Runtime settings.

    Secrets are deliberately injectable for tests but are never given a source
    default. Production callers must load them from the environment or an
    explicitly selected external JSON file.
    """

    database_path: Path = DEFAULT_DATABASE_PATH
    host: str = "127.0.0.1"
    port: int = 8091
    token_bindings: Mapping[str, str] = field(default_factory=dict, repr=False)
    read_token: str | None = field(default=None, repr=False)
    max_body_bytes: int = 1_000_000
    max_events_per_batch: int = 500
    allow_empty_tokens: bool = field(default=False, repr=False)
    media_audio_dir: Path | None = None
    media_incoming_dir: Path | None = None
    media_tool_script: Path | None = None
    tianditu_key: str = DEFAULT_TIANDITU_KEY
    config_path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host must not be empty")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if not 1 <= self.max_events_per_batch <= 500:
            raise ValueError("max_events_per_batch must be between 1 and 500")
        if not isinstance(self.tianditu_key, str) or not self.tianditu_key.strip() or self.tianditu_key != self.tianditu_key.strip():
            raise ValueError("tianditu_key must be a non-empty string without surrounding whitespace")
        bindings = _token_bindings(dict(self.token_bindings), source="runtime")
        read_token = _read_token(self.read_token, source="runtime")
        if read_token is not None and read_token in bindings:
            raise ValueError("read token must be distinct from every device token")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "CentralConfig":
        env = dict(os.environ if environ is None else environ)
        explicit_config = env.get("LIFE_RADIO_CENTRAL_CONFIG")
        automatic_config = default_config_path(env)
        selected_config = explicit_config or (str(automatic_config) if automatic_config.exists() else None)
        external = _read_external_config(selected_config)

        file_bindings = _token_bindings(external.get("token_bindings"), source="external config")
        env_bindings_raw = env.get("LIFE_RADIO_CENTRAL_TOKENS_JSON")
        if env_bindings_raw is not None:
            try:
                parsed_bindings = json.loads(env_bindings_raw)
            except json.JSONDecodeError as error:
                raise ValueError("LIFE_RADIO_CENTRAL_TOKENS_JSON is invalid JSON") from error
            bindings = _token_bindings(parsed_bindings, source="environment")
        else:
            bindings = file_bindings

        if "LIFE_RADIO_CENTRAL_READ_TOKEN" in env:
            read_token = _read_token(
                env["LIFE_RADIO_CENTRAL_READ_TOKEN"],
                source="environment",
            )
        else:
            read_token = _read_token(
                external.get("read_token"),
                source="external config",
            )

        database_value = env.get("LIFE_RADIO_CENTRAL_DATABASE") or external.get("database_path")
        if database_value:
            database_path = Path(os.path.expandvars(str(database_value))).expanduser()
            if not database_path.is_absolute() and selected_config:
                database_path = Path(selected_config).expanduser().resolve().parent / database_path
        elif selected_config:
            database_path = Path(selected_config).expanduser().resolve().parent / "life_radio.sqlite3"
        else:
            database_path = default_data_dir(env) / "life_radio.sqlite3"
        host = str(env.get("LIFE_RADIO_CENTRAL_HOST") or external.get("host") or "127.0.0.1")
        port_value = env.get("LIFE_RADIO_CENTRAL_PORT") or external.get("port") or 8091
        try:
            port = int(port_value)
        except (TypeError, ValueError) as error:
            raise ValueError("central service port must be an integer") from error

        def _optional_path(key_env: str, key_external: str) -> Path | None:
            value = env.get(key_env) or external.get(key_external)
            if not value:
                return None
            resolved = Path(os.path.expandvars(str(value))).expanduser()
            if not resolved.is_absolute() and selected_config:
                resolved = Path(selected_config).expanduser().resolve().parent / resolved
            return resolved

        media_audio_dir = _optional_path("LIFE_RADIO_CENTRAL_MEDIA_AUDIO_DIR", "media_audio_dir")
        media_incoming_dir = _optional_path("LIFE_RADIO_CENTRAL_MEDIA_INCOMING_DIR", "media_incoming_dir")
        media_tool_script = _optional_path("LIFE_RADIO_CENTRAL_MEDIA_TOOL_SCRIPT", "media_tool_script")
        tianditu_key = str(env.get("LIFE_RADIO_TIANDITU_KEY") or external.get("tianditu_key") or DEFAULT_TIANDITU_KEY).strip()

        return cls(
            database_path=database_path,
            host=host,
            port=port,
            token_bindings=bindings,
            read_token=read_token,
            config_path=(
                Path(selected_config).expanduser().resolve()
                if selected_config
                else None
            ),
            media_audio_dir=media_audio_dir,
            media_incoming_dir=media_incoming_dir,
            media_tool_script=media_tool_script,
            tianditu_key=tianditu_key,
        )
