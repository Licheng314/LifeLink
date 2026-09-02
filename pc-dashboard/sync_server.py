#!/usr/bin/env python3
"""Life Link PC client, local Dashboard API, and central upload worker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse as parse_url
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
import urllib.request
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from central_client import CentralClient, CentralReadClient, CentralReadError, CentralDeviceClient
from central_client_setup import default_client_config_path, read_client_runtime_config
from device_identity import (
    default_client_data_dir,
    default_identity_path,
    device_descriptor,
    migrate_legacy_appdata_client_state,
    migrate_legacy_installation_client_state,
    migrate_presplit_client_state,
)
from outbox import Outbox
from blacklist_cache import load as load_cached_blacklist, save as save_cached_blacklist
from aw_web_compat import AWWebCompatReceiver
from windows_native_collector import WindowsNativeCollector
import pc_windows_startup
from runtime_paths import installation_dir, is_frozen, resource_dir


CENTRAL_CLIENT_CLASS = CentralClient
CENTRAL_READ_CLIENT_CLASS = CentralReadClient


HOST = "127.0.0.1"
MAX_BODY_BYTES = 1_000_000
MAX_EVENTS_PER_BATCH = 500
BASE_DIR = resource_dir()
INSTALLATION_DIR = installation_dir()
DATA_DIR = default_client_data_dir() / "data"
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
WEB_ASSET_DIR = BASE_DIR / "web"
WEB_ASSETS: dict[str, tuple[Path, str]] = {
    "/assets/images/life-link-logo.png": (WEB_ASSET_DIR / "assets" / "life-link-logo.png", "image/png"),
    "/assets/styles/base.css": (WEB_ASSET_DIR / "styles" / "base.css", "text/css; charset=utf-8"),
    "/assets/styles/components.css": (WEB_ASSET_DIR / "styles" / "components.css", "text/css; charset=utf-8"),
    "/assets/styles/wishes-events.css": (WEB_ASSET_DIR / "styles" / "wishes-events.css", "text/css; charset=utf-8"),
    "/assets/styles/tools.css": (WEB_ASSET_DIR / "styles" / "tools.css", "text/css; charset=utf-8"),
    "/assets/scripts/shared-ui.js": (WEB_ASSET_DIR / "scripts" / "shared-ui.js", "text/javascript; charset=utf-8"),
    "/assets/scripts/wishes-events.js": (WEB_ASSET_DIR / "scripts" / "wishes-events.js", "text/javascript; charset=utf-8"),
    "/assets/scripts/usage.js": (WEB_ASSET_DIR / "scripts" / "usage.js", "text/javascript; charset=utf-8"),
    "/assets/scripts/health-info.js": (WEB_ASSET_DIR / "scripts" / "health-info.js", "text/javascript; charset=utf-8"),
    "/assets/scripts/devices.js": (WEB_ASSET_DIR / "scripts" / "devices.js", "text/javascript; charset=utf-8"),
    "/assets/scripts/location.js": (WEB_ASSET_DIR / "scripts" / "location.js", "text/javascript; charset=utf-8"),
    "/assets/scripts/app.js": (WEB_ASSET_DIR / "scripts" / "app.js", "text/javascript; charset=utf-8"),
    "/assets/scripts/tools.js": (WEB_ASSET_DIR / "scripts" / "tools.js", "text/javascript; charset=utf-8"),
    "/assets/vendor/leaflet/leaflet.css": (WEB_ASSET_DIR / "vendor" / "leaflet" / "leaflet.css", "text/css; charset=utf-8"),
    "/assets/vendor/leaflet/leaflet.js": (WEB_ASSET_DIR / "vendor" / "leaflet" / "leaflet.js", "text/javascript; charset=utf-8"),
}
EVENT_FILE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
SHARED_SETTINGS_CACHE_FILENAME = "shared_settings_cache.json"
V17_READ_CACHE_FILENAME = "v17_read_cache.json"
HEALTH_INFO_CACHE_FILENAME = "health_info_cache.json"


def _load_config() -> dict[str, Any]:
    """Load the single machine-local client configuration."""
    configured = os.environ.get("LIFE_RADIO_CLIENT_CONFIG")
    path = Path(configured).expanduser() if configured else default_client_config_path()
    return read_client_runtime_config(path)


_CFG = _load_config()

# Precedence: environment override > machine-local config > code default.
PORT = int(os.environ.get("LIFE_RADIO_PORT") or _CFG.get("port") or 8090)
HOST = os.environ.get("LIFE_RADIO_HOST") or _CFG.get("host") or HOST
CENTRAL_BASE_URL = str(
    os.environ.get("LIFE_RADIO_CENTRAL_BASE_URL")
    or _CFG.get("central_base_url")
    or ""
).strip().rstrip("/")
CENTRAL_SYNC_INTERVAL_SECONDS = max(
    30,
    int(
        os.environ.get("LIFE_RADIO_CENTRAL_SYNC_INTERVAL_SECONDS")
        or _CFG.get("central_sync_interval_seconds")
        or 600
    ),
)
CENTRAL_MAX_BATCHES_PER_RUN = max(
    1, min(100, int(os.environ.get("LIFE_RADIO_CENTRAL_MAX_BATCHES_PER_RUN", "20")))
)
TIANDITU_MAP_KEY = str(
    os.environ.get("LIFE_RADIO_TIANDITU_KEY") or _CFG.get("tianditu_key") or ""
).strip()


def _configured_bool(environment_name: str, config_name: str, default: bool) -> bool:
    value = os.environ.get(environment_name)
    if value is None:
        value = _CFG.get(config_name, default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def default_central_outbox_path() -> Path:
    configured = os.environ.get("LIFE_RADIO_CENTRAL_OUTBOX")
    if configured:
        return Path(configured).expanduser()
    return default_identity_path().with_name("outbox.sqlite3")


CENTRAL_OUTBOX_PATH = default_central_outbox_path()
CENTRAL_IDENTITY_PATH = default_identity_path()
APP_USAGE_COLLECTION_ENABLED = _configured_bool(
    "LIFE_RADIO_APP_USAGE_COLLECTION_ENABLED",
    "app_usage_collection_enabled",
    True,
)
AI_CONTEXT_GUIDE = BASE_DIR / "ai_context" / "README.md"
AI_READER_SKILL_SOURCE = (
    BASE_DIR / "resources" / "life-link-ai-reader" / "SKILL.md"
    if is_frozen() else BASE_DIR.parent / ".codex" / "skills" / "life-link-ai-reader" / "SKILL.md"
)
AI_READER_MCP_EXECUTABLE = (
    BASE_DIR / "resources" / "life-link-mcp" / "life-link-mcp.exe"
    if is_frozen() else BASE_DIR.parent / "life-link-mcp" / "dist" / "life-link-mcp.exe"
)
CHROME_SWITCH_MIN_SECONDS = 2
LOCATION_CLUSTER_RADIUS_METERS = 150
LOCATION_STAY_SECONDS = 15 * 60
# aw-watcher-web-chrome commonly refreshes an unchanged page about every
# 90 seconds. Keep two heartbeat intervals when returning from another app.
CHROME_URL_RESUME_GRACE_SECONDS = 180


def export_ai_reader_skill(*, open_location: bool = True) -> Path:
    """Create a shareable Skill copy and optionally reveal it in Explorer."""
    if not AI_READER_SKILL_SOURCE.is_file():
        raise FileNotFoundError("Life Link AI reader Skill source is missing")
    destination = (
        DATA_DIR.parent / "exports" / "life-link-ai-reader" / "SKILL.md"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(AI_READER_SKILL_SOURCE, destination)
    if open_location:
        if os.name == "nt":
            subprocess.Popen(
                ["explorer.exe", f"/select,{destination}"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            raise OSError("opening the Skill export is supported only on Windows")
    return destination


def _reveal_in_explorer(path: Path) -> None:
    if os.name != "nt":
        raise OSError("opening the export is supported only on Windows")
    os.startfile(str(path.parent))  # type: ignore[attr-defined]


def create_ai_reader_connection_bundle(
    pairing_payload: dict[str, Any], *, open_location: bool = True,
) -> Path:
    """Package the MCP executable, current Skill, and one-time pairing material."""
    if not AI_READER_SKILL_SOURCE.is_file():
        raise FileNotFoundError("Life Link AI reader Skill source is missing")
    if not AI_READER_MCP_EXECUTABLE.is_file():
        raise FileNotFoundError("Life Link MCP executable has not been built")
    pairing_text = pairing_payload.get("pairing_text")
    expires_at = pairing_payload.get("expires_at")
    central_instance_id = pairing_payload.get("central_instance_id")
    if not all(isinstance(value, str) and value.strip() for value in (
        pairing_text, expires_at, central_instance_id,
    )):
        raise ValueError("central pairing response is incomplete")
    try:
        pairing = json.loads(pairing_text)
    except json.JSONDecodeError as error:
        raise ValueError("central pairing text is not valid JSON") from error
    if not isinstance(pairing, dict):
        raise ValueError("central pairing text must be a JSON object")

    export_dir = DATA_DIR.parent / "exports" / "ai-connections"
    export_dir.mkdir(parents=True, exist_ok=True)
    created_at = utc_now()
    stamp = created_at.astimezone(DISPLAY_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    destination = export_dir / f"LifeLink-AI-MCP-Connection-{stamp}-{uuid.uuid4().hex[:6]}.zip"
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    skill_text = AI_READER_SKILL_SOURCE.read_text(encoding="utf-8")
    mcp_profile_id = str(uuid.uuid4())
    executable_hash = hashlib.sha256(AI_READER_MCP_EXECUTABLE.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "life-link-ai-mcp-connection-package/v1",
        "product": "Life Link",
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at,
        "central_instance_id": central_instance_id,
        "mcp_profile_id": mcp_profile_id,
        "entrypoint": "README.md",
        "pairing_file": "pairing.json",
        "skill_file": "life-link-ai-reader/SKILL.md",
        "executable": "life-link-mcp.exe",
        "executable_sha256": executable_hash,
        "transport": "stdio",
    }
    mcp_config = {
        "mcpServers": {
            "life-link": {
                "command": "<解压后的绝对路径>\\life-link-mcp.exe",
                "args": ["serve"],
            }
        }
    }
    reader_identity = {
        "schema_version": "life-link-mcp-reader/v1",
        "reader": {
            "type": "mcp-client",
            "instance_id": f"mcp:{mcp_profile_id}",
            "display_name": "AI Companion",
        },
    }
    readme = f"""# Life Link AI MCP 连接包

请把这个压缩包视为私密的一次性授权材料。配对有效期至 `{expires_at}`。

目标 AI 请依次完成：

1. 将整个 ZIP 解压到一个私密、稳定的本地目录；不要只从压缩包临时预览或单独复制 EXE。
2. 完整阅读并遵守 `life-link-ai-reader/SKILL.md`。如果当前 AI 支持安装 Skill，请安装该目录；否则将它作为本连接的固定理解说明。
3. **必须在首次读取前完成 `reader.json`**：把 `display_name` 改成自己的稳定显示名；若 AI 与 Life Link 位于同一台 Windows 电脑，先检查当前真实运行进程的命令行，再按 Skill 规则加入 `process_binding`。禁止根据安装目录、包结构、文档示例或猜测的入口文件填写；共享宿主的路径段必须连续出现在同一个真实启动参数中。OpenClaw 常见写法是 `node.exe` 加 `node_modules/openclaw`，不要擅自猜成 `dist/index.js`；只有实际命令行确实包含其他入口时才能采用。无法准确确认则保持省略，不要使用 PID，也不要只绑定 Node、Python 或 Java 等共享宿主。首次读取后再修改此文件不会更新已经配对的记录。
4. 根据当前 AI 自己的 MCP 配置方式登记本目录中的 `life-link-mcp.exe`，使用 stdio，参数为 `serve`。`mcp-config.example.json` 只展示通用结构，目标 AI 应自行转换成自己的配置格式。
5. 启动 MCP 后先调用 `lifelink_connection_status`。需要背景或新增事件时调用 `lifelink_read_context`；首次读取会由 EXE 私密读取 `pairing.json`、领取长期只读 Token，并立即返回首次上下文。随后可轻量轮询 `lifelink_check_updates`，仅当 `update_mcp=true` 时读取上下文。再次调用 `lifelink_connection_status`，确认返回的 `reader.process_binding` 与配对前核对的真实进程一致。
6. 不要在回复、日志、源码或遥测中输出 `pairing.json`、Token、游标或完整上下文。分别报告“MCP/Token 是否连接”和“进程绑定是否已按真实命令行配置”，不能只笼统回复“全部 OK”。`lifelink_connection_status` 不代表服务端已经检测到进程，最终以 Life Link WebUI 是否出现绿色进程提示为准；失败时提供不含秘密的原因和下一步。

本包包含真实的 Windows MCP stdio 可执行程序。它不识别目标 AI 应用，也不修改任何特定应用的配置；登记工作由收到本包的目标 AI 自行完成。配对凭据只能领取一次。
"""
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("README.md", readme.encode("utf-8"))
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            archive.writestr(
                "pairing.json",
                json.dumps(pairing, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            archive.writestr(
                "mcp-config.example.json",
                json.dumps(mcp_config, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            archive.writestr(
                "reader.json",
                json.dumps(reader_identity, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            archive.writestr("life-link-ai-reader/SKILL.md", skill_text.encode("utf-8"))
            archive.write(AI_READER_MCP_EXECUTABLE, "life-link-mcp.exe")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    # Keep one unambiguous hand-off file. Cleanup happens only after the new
    # archive is complete, so a packaging failure never removes the previous one.
    for pattern in ("LifeLink-AI-Connection-*.zip", "LifeLink-AI-MCP-Connection-*.zip"):
        for old_bundle in export_dir.glob(pattern):
            if old_bundle == destination:
                continue
            try:
                old_bundle.unlink()
            except OSError:
                continue
    if open_location:
        _reveal_in_explorer(destination)
    return destination


try:
    DISPLAY_TIMEZONE = ZoneInfo(os.environ.get("LIFE_RADIO_DISPLAY_TIMEZONE", "Asia/Shanghai"))
except Exception:
    # Windows Python may not ship the IANA zone database. Life Link's default
    # display calendar is Beijing time, never UTC in that fallback case.
    DISPLAY_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

EVENT_TYPES = {
    "app.foreground",
    "web.foreground",
    "device.input_state",
    "custom.event",
    "calendar.event",
    "task.update",
    "location.observation",
    "location.sample",
    "location.stay",
    "location.visit",
    "manual.note",
}
SOURCE_KINDS = {"android", "desktop", "web", "manual"}
COLLECTORS = {
    "accessibility_service",
    "usage_stats",
    "activitywatch",
    "windows_native",
    "fused_location",
    "browser_extension",
    "calendar_import",
    "life_radio_app",
    "manual_entry",
}
RELIABILITY_VALUES = {"observed", "inferred", "user_confirmed"}

data_lock = threading.Lock()
central_sync_lock = threading.Lock()
live_daily_cache_lock = threading.Lock()
live_daily_cache: tuple[float, str, int, tuple[int, int]] | None = None
seen_event_ids_cache: tuple[str, set[str]] | None = None
central_runtime_lock = threading.RLock()
central_outbox_instance: Outbox | None = None
central_outbox_instance_path: Path | None = None
central_sync_status: dict[str, Any] = {
    "state": "idle",
    "last_started_at": None,
    "last_finished_at": None,
    "last_result": None,
}
native_collector_lock = threading.RLock()
native_collector_instance: WindowsNativeCollector | None = None
native_collector_latest_events: list[dict[str, Any]] = []
native_collector_status: dict[str, Any] = {"status": "not_started"}
browser_receiver_instance: AWWebCompatReceiver | None = None
browser_receiver_status: dict[str, Any] = {"status": "not_started", "port": 5600}
NATIVE_CHECKPOINT_KEY = "windows_native_checkpoint:v1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_day_start_hour() -> int:
    """Return the most recently confirmed central business-day boundary."""
    settings = load_shared_settings_cache()
    return settings["day_start_hour"] if settings is not None else 0


def shared_settings_cache_path() -> Path:
    return DATA_DIR / SHARED_SETTINGS_CACHE_FILENAME


def is_valid_shared_settings(payload: Any) -> bool:
    required = {"timezone", "day_start_hour", "primary_health_device_id", "sleep_local_time", "ai_display_name", "morning_report", "evening_report", "periodic_summary", "settings_version", "updated_at"}
    if not isinstance(payload, dict) or set(payload) != required:
        return False
    hour = payload["day_start_hour"]
    version = payload["settings_version"]
    updated_at = payload["updated_at"]
    primary = payload["primary_health_device_id"]
    if not isinstance(updated_at, str) or not updated_at.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        payload["timezone"] == "Asia/Shanghai"
        and not isinstance(hour, bool) and isinstance(hour, int) and 0 <= hour <= 23
        and not isinstance(version, bool) and isinstance(version, int) and version >= 1
        and (primary is None or isinstance(primary, str) and bool(primary))
        and isinstance(payload["sleep_local_time"], str) and bool(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", payload["sleep_local_time"]))
        and isinstance(payload["ai_display_name"], str) and 1 <= len(payload["ai_display_name"].strip()) <= 80
        and _valid_report_schedule("morning_report", payload["morning_report"])
        and _valid_report_schedule("evening_report", payload["evening_report"])
        and _valid_report_schedule("periodic_summary", payload["periodic_summary"])
    )


def _valid_report_schedule(kind: str, value: Any) -> bool:
    clock = lambda candidate: isinstance(candidate, str) and bool(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", candidate))
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        return False
    if kind == "morning_report":
        mode = value.get("mode")
        return set(value) == {"enabled", "mode", "delay_minutes", "local_time"} and mode in {"after_first_usage", "fixed_time"} and isinstance(value.get("delay_minutes"), int) and not isinstance(value.get("delay_minutes"), bool) and 1 <= value["delay_minutes"] <= 720 and ((mode == "after_first_usage" and value.get("local_time") is None) or (mode == "fixed_time" and clock(value.get("local_time"))))
    if kind == "evening_report":
        return set(value) == {"enabled", "local_time"} and clock(value.get("local_time"))
    return set(value) == {"enabled", "start_local_time", "end_local_time", "interval_minutes"} and clock(value.get("start_local_time")) and clock(value.get("end_local_time")) and value.get("interval_minutes") in {30, 60, 120, 180, 240}


def load_shared_settings_cache() -> dict[str, Any] | None:
    cached = load_json(shared_settings_cache_path(), None)
    return cached if is_valid_shared_settings(cached) else None


def save_shared_settings_cache(settings: dict[str, Any]) -> None:
    if not is_valid_shared_settings(settings):
        raise ValueError("shared settings cache must contain a valid central response")
    with data_lock:
        atomic_write_json(shared_settings_cache_path(), settings)


def refresh_shared_settings() -> dict[str, Any] | None:
    """Refresh the read-only cache; retain the prior confirmed value on failure."""
    token = get_central_token()
    if not CENTRAL_BASE_URL or not token:
        return None
    try:
        settings = CENTRAL_CLIENT_CLASS(CENTRAL_BASE_URL, token).get_shared_settings()
        save_shared_settings_cache(settings)
        return settings
    except (ValueError, CentralReadError, OSError):
        return None


def business_date(value: datetime) -> str:
    """Local business date, with a configurable local cross-day hour."""
    return (value.astimezone(DISPLAY_TIMEZONE) - timedelta(hours=get_day_start_hour())).date().isoformat()


def business_day_utc_bounds(date_str: str) -> tuple[str, str]:
    """Return the selected local business day's exact UTC half-open window."""
    try:
        local_date = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError) as error:
        raise ValueError("date must use YYYY-MM-DD") from error
    local_start = local_date.replace(
        hour=get_day_start_hour(), minute=0, second=0, microsecond=0,
        tzinfo=DISPLAY_TIMEZONE,
    )
    return utc_timestamp(local_start), utc_timestamp(local_start + timedelta(days=1))


def utc_timestamp(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown-pc"


def device_data_dir() -> Path:
    return DATA_DIR / "devices"


def storage_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned[:80] or "unknown"


def device_storage_key(device_id: str, platform: str) -> str:
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:16]
    return f"{storage_component(platform)}-{digest}"


def v1_device_descriptor(device: dict[str, Any]) -> dict[str, str]:
    device_id = str(device["device_id"])
    platform = str(device["platform"])
    return {
        "device_key": device_storage_key(device_id, platform),
        "device_id": device_id,
        "platform": platform,
        "display_name": str(device.get("display_name") or device_id),
    }


def local_desktop_device_descriptor() -> dict[str, str]:
    """Return this installation's central-client device descriptor."""
    return v1_device_descriptor(device_descriptor(
        display_name=get_hostname(), identity_path=CENTRAL_IDENTITY_PATH,
    ))


def get_device_dir(device_key: str) -> Path:
    return device_data_dir() / storage_component(device_key)


def get_device_metadata_file(device_key: str) -> Path:
    return get_device_dir(device_key) / "device.json"


def get_device_data_file(device_key: str, event_type: str, date_str: str) -> Path:
    return get_device_dir(device_key) / "events" / storage_component(event_type) / f"{date_str}.json"


def parse_utc_datetime(value: Any) -> datetime | None:
    """Return a UTC datetime only for ISO-8601 timestamps ending in Z."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically so an interruption cannot corrupt a data file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_device_date_data(device: dict[str, str], event_type: str, date_str: str) -> dict[str, Any]:
    data = load_json(
        get_device_data_file(device["device_key"], event_type, date_str),
        {
            "storage_version": "v4",
            "date": date_str,
            "device": device,
            "event_type": event_type,
            "batches": [],
        },
    )
    if not isinstance(data, dict):
        data = {}
    data["storage_version"] = "v4"
    data["date"] = date_str
    data["device"] = device
    data["event_type"] = event_type
    if not isinstance(data.get("batches"), list):
        data["batches"] = []
    return data


def iter_device_event_files() -> list[Path]:
    root = device_data_dir()
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.glob("*/events/*/*.json")
        if EVENT_FILE_PATTERN.match(path.name)
    )


def load_device_metadata() -> list[dict[str, Any]]:
    root = device_data_dir()
    if not root.exists():
        return []
    devices = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        metadata = load_json(path / "device.json", None)
        if isinstance(metadata, dict) and metadata.get("device_key"):
            devices.append(metadata)
    return devices


def local_activitywatch_device_ids() -> set[str]:
    # Only this process's persisted instance ID is authoritative locally.
    return {local_desktop_device_descriptor()["device_id"]}


def is_local_device_metadata(metadata: dict[str, Any]) -> bool:
    device_id = str(metadata.get("device_id", ""))
    if device_id in local_activitywatch_device_ids():
        return True
    if not device_id.startswith("legacy:desktop:"):
        return False
    local_names = {get_hostname().casefold()}
    return str(metadata.get("display_name", "")).strip().casefold() in local_names


def touch_device(device: dict[str, str], received_at: str) -> None:
    path = get_device_metadata_file(device["device_key"])
    metadata = load_json(path, {})
    if not isinstance(metadata, dict):
        metadata = {}
    existing_first_seen = metadata.get("first_seen_at")
    metadata.update(device)
    metadata["storage_version"] = "v4"
    metadata["first_seen_at"] = existing_first_seen or received_at
    previous_last_seen = parse_utc_datetime(metadata.get("last_received_at"))
    current_last_seen = parse_utc_datetime(received_at)
    if previous_last_seen is None or (current_last_seen and current_last_seen >= previous_last_seen):
        metadata["last_received_at"] = received_at
        metadata["last_connected_at"] = received_at
    atomic_write_json(path, metadata)


def load_seen_event_ids() -> set[str]:
    """Load the durable dedupe index once per active data directory."""
    global seen_event_ids_cache
    cache_key = str(DATA_DIR.resolve())
    if seen_event_ids_cache and seen_event_ids_cache[0] == cache_key:
        return seen_event_ids_cache[1]
    seen: set[str] = set()
    for path in iter_device_event_files():
        data = load_json(path, {"batches": []})
        if not isinstance(data, dict):
            continue
        for batch in data["batches"]:
            if not isinstance(batch, dict):
                continue
            for event in batch.get("events", []):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("event_id", event.get("id"))
                if isinstance(event_id, str) and event_id:
                    seen.add(event_id)
    seen_event_ids_cache = (cache_key, seen)
    return seen


def event_date(event: dict[str, Any]) -> str:
    occurred = parse_utc_datetime(event.get("occurred_at"))
    if occurred is not None:
        return business_date(occurred)
    received = parse_utc_datetime(event.get("_received_at"))
    return (received or utc_now()).date().isoformat()


def write_device_events(
    events: list[dict[str, Any]], *, device: dict[str, str], batch_metadata: dict[str, Any]
) -> None:
    """Write events into device/event-category/date partitions."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[(event_date(event), str(event.get("event_type", "legacy.unknown")))].append(event)
    for (date_str, event_type), grouped_events in grouped.items():
        data = load_device_date_data(device, event_type, date_str)
        batch = dict(batch_metadata)
        batch["device"] = device
        batch["event_count"] = len(grouped_events)
        batch["events"] = grouped_events
        data["batches"].append(batch)
        atomic_write_json(get_device_data_file(device["device_key"], event_type, date_str), data)


def save_events(
    events: list[dict[str, Any]], *, device: dict[str, str], batch_metadata: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Persist unseen events by device, category and occurrence date."""
    accepted_ids = [event["event_id"] for event in events]
    with data_lock:
        seen = load_seen_event_ids()
        new_events = [event for event in events if event["event_id"] not in seen]
        duplicates = [event["event_id"] for event in events if event["event_id"] in seen]
        seen.update(event["event_id"] for event in new_events)
        metadata_path = get_device_metadata_file(device["device_key"])
        if new_events or metadata_path.exists():
            touch_device(device, str(batch_metadata["received_at"]))
        if new_events:
            write_device_events(new_events, device=device, batch_metadata=batch_metadata)
    return accepted_ids, duplicates


def is_location_segment_update(stored: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Whether a same-ID Android location replay replaces an active segment."""
    location_segment_types = {"location.sample", "location.stay"}
    stored_payload = stored.get("payload") if isinstance(stored.get("payload"), dict) else {}
    candidate_payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    if isinstance(stored_payload.get("legacy_data"), dict):
        stored_payload = stored_payload["legacy_data"]
    if isinstance(candidate_payload.get("legacy_data"), dict):
        candidate_payload = candidate_payload["legacy_data"]
    stored_is_legacy_location = stored.get("event_type") == "location.visit" and stored_payload.get("kind") in {"sample", "stay"}
    if stored.get("event_type") not in location_segment_types and not stored_is_legacy_location:
        return False
    if candidate.get("event_type") not in location_segment_types:
        return False
    if stored_payload.get("is_active") is not True:
        return False
    # The contract defaults an omitted flag to a finalized segment, so an
    # older sender that does not emit ``is_active`` must not leave it open.
    candidate_is_active = candidate_payload.get("is_active", False)
    stored_duration = int(stored.get("duration_seconds", 0) or 0)
    candidate_duration = int(candidate.get("duration_seconds", 0) or 0)
    return candidate_is_active is False or candidate_duration > stored_duration


def refresh_mutable_events(events: list[dict[str, Any]]) -> set[str]:
    """Refresh updateable AW events and active Android location segments by ID."""
    incoming = {event["event_id"]: event for event in events}
    refreshed: set[str] = set()
    location_moves: list[tuple[dict[str, str], dict[str, Any], dict[str, Any]]] = []
    for path in iter_device_event_files():
        data = load_json(path, {"batches": []})
        changed = False
        for batch in data.get("batches", []) if isinstance(data, dict) else []:
            if not isinstance(batch, dict) or not isinstance(batch.get("events"), list):
                continue
            retained_events: list[dict[str, Any]] = []
            for stored in batch["events"]:
                candidate = incoming.get(stored.get("event_id")) if isinstance(stored, dict) else None
                if not candidate:
                    if isinstance(stored, dict):
                        retained_events.append(stored)
                    continue
                stored_payload = stored.get("payload", {}) if isinstance(stored.get("payload"), dict) else {}
                stored_aw = stored_payload.get("activitywatch") if isinstance(stored_payload.get("activitywatch"), dict) else stored_payload.get("legacy_data", {}).get("activitywatch", {})
                candidate_payload = candidate.get("payload", {}) if isinstance(candidate.get("payload"), dict) else {}
                candidate_aw = candidate_payload.get("activitywatch") if isinstance(candidate_payload.get("activitywatch"), dict) else {}
                candidate_is_aw = "event_id" in candidate_aw
                stored_is_aw = isinstance(stored_aw, dict) and "event_id" in stored_aw
                location_update = is_location_segment_update(stored, candidate)
                if not stored_is_aw and not candidate_is_aw and not location_update:
                    retained_events.append(stored)
                    continue
                # A same-ID replay with corrected AW metadata repairs records
                # written by the former relay shape, even if duration is equal.
                should_repair_legacy_shape = candidate_is_aw and not stored_is_aw
                if (
                    should_repair_legacy_shape
                    or location_update
                    or int(candidate.get("duration_seconds", 0)) > int(stored.get("duration_seconds", 0) or 0)
                ):
                    if location_update and candidate.get("event_type") != stored.get("event_type"):
                        # Android promotes an active segment from sample to stay
                        # after 15 minutes. The same event ID must move to its
                        # new category partition instead of being left as a
                        # stale sample or duplicated in both files.
                        device = data.get("device") if isinstance(data.get("device"), dict) else {}
                        location_moves.append((device, dict(batch), candidate))
                        refreshed.add(candidate["event_id"])
                        changed = True
                        continue
                    if "duration_seconds" in candidate:
                        stored["duration_seconds"] = candidate["duration_seconds"]
                    stored["payload"] = candidate["payload"]
                    stored["_received_at"] = candidate["_received_at"]
                    refreshed.add(candidate["event_id"])
                    changed = True
                retained_events.append(stored)
            if len(retained_events) != len(batch["events"]):
                batch["events"] = retained_events
                batch["event_count"] = len(retained_events)
        if changed:
            remaining_events = any(
                isinstance(batch, dict) and bool(batch.get("events"))
                for batch in data.get("batches", [])
            )
            if remaining_events:
                atomic_write_json(path, data)
            else:
                path.unlink(missing_ok=True)
    for device, original_batch, candidate in location_moves:
        if not isinstance(device.get("device_key"), str):
            continue
        date_str = event_date(candidate)
        target = load_device_date_data(device, str(candidate["event_type"]), date_str)
        target_batch = dict(original_batch)
        target_batch["event_count"] = 1
        target_batch["events"] = [candidate]
        target["batches"].append(target_batch)
        atomic_write_json(get_device_data_file(device["device_key"], str(candidate["event_type"]), date_str), target)
    return refreshed


def get_central_token() -> str:
    return os.environ.get("LIFE_RADIO_CENTRAL_TOKEN", "").strip()


def get_central_read_token() -> str:
    """Return the server-only credential used by same-origin read proxies."""
    return os.environ.get("LIFE_RADIO_CENTRAL_READ_TOKEN", "").strip()


class CentralReadConfigurationError(RuntimeError):
    """Central read proxy configuration is absent or invalid."""


class CentralReadNotModified(RuntimeError):
    """Central confirmed that a cached private resource is still current."""


USAGE_COUNTER_FIELDS = ("events", "window_events", "web_events", "afk_seconds")
USAGE_MAP_FIELDS = (
    "apps", "hourly", "hourly_apps", "hourly_online", "sites", "hourly_sites",
)


def read_central_view(view: str, date_str: str) -> dict[str, Any]:
    """Fetch one central compatibility view without exposing its read token."""
    token = get_central_read_token()
    if not CENTRAL_BASE_URL or not token:
        raise CentralReadConfigurationError(
            "central read proxy requires LIFE_RADIO_CENTRAL_BASE_URL and "
            "LIFE_RADIO_CENTRAL_READ_TOKEN"
        )
    from_utc, to_utc = business_day_utc_bounds(date_str)
    local_device_id = local_desktop_device_descriptor()["device_id"]
    try:
        client = CENTRAL_READ_CLIENT_CLASS(CENTRAL_BASE_URL, token)
    except ValueError as error:
        raise CentralReadConfigurationError(str(error)) from error
    return client.read_view(
        view,
        from_utc=from_utc,
        to_utc=to_utc,
        local_device_id=local_device_id,
    )


CENTRAL_MEDIA_TIMEOUT_SECONDS = 30


class CentralMediaProxyError(RuntimeError):
    def __init__(self, category: str, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status


def central_media_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    use_read: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Proxy a media API call to the central service without exposing tokens."""
    if not CENTRAL_BASE_URL:
        raise CentralReadConfigurationError("central media proxy requires LIFE_RADIO_CENTRAL_BASE_URL")
    token = get_central_read_token() if use_read else get_central_token()
    if not token:
        raise CentralReadConfigurationError("central media proxy requires a central token")
    url = CENTRAL_BASE_URL.rstrip("/") + path
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=CENTRAL_MEDIA_TIMEOUT_SECONDS) as response:
            raw = response.read()
            if response.status == 204 or not raw:
                return response.status, {}
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            return response.status, payload if isinstance(payload, dict) else {"data": payload}
    except HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            payload = {}
        message = payload.get("message") if isinstance(payload, dict) else None
        raise CentralMediaProxyError(
            payload.get("error", "central_media_error") if isinstance(payload, dict) else "central_media_error",
            message or f"central returned HTTP {error.code}",
            http_status=error.code,
        ) from error
    except OSError as error:
        raise CentralMediaProxyError("central_unreachable", f"无法连接中央服务：{error}") from error


def read_central_locations(date_str: str) -> dict[str, Any]:
    """Fetch the central location projection without exposing its read token."""
    token = get_central_read_token()
    if not CENTRAL_BASE_URL or not token:
        raise CentralReadConfigurationError(
            "central read proxy requires LIFE_RADIO_CENTRAL_BASE_URL and "
            "LIFE_RADIO_CENTRAL_READ_TOKEN"
        )
    from_utc, to_utc = business_day_utc_bounds(date_str)
    local_device_id = local_desktop_device_descriptor()["device_id"]
    try:
        client = CENTRAL_READ_CLIENT_CLASS(CENTRAL_BASE_URL, token)
    except ValueError as error:
        raise CentralReadConfigurationError(str(error)) from error
    return client.read_view(
        "locations",
        from_utc=from_utc,
        to_utc=to_utc,
        local_device_id=local_device_id,
    )


def read_central_calendar_days(from_date: str, to_date: str) -> dict[str, Any]:
    """Proxy the central calendar summary without exposing the read token."""
    token = get_central_read_token()
    if not CENTRAL_BASE_URL or not token:
        raise CentralReadConfigurationError(
            "central read proxy requires LIFE_RADIO_CENTRAL_BASE_URL and "
            "LIFE_RADIO_CENTRAL_READ_TOKEN"
        )
    try:
        client = CENTRAL_READ_CLIENT_CLASS(CENTRAL_BASE_URL, token)
    except ValueError as error:
        raise CentralReadConfigurationError(str(error)) from error
    return client.read_calendar_days(from_date=from_date, to_date=to_date)


def normalize_central_window_stats(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    counters: dict[str, int] = {}
    for field in ("event_count", "batch_count"):
        try:
            counters[field] = max(0, int(source.get(field, 0) or 0))
        except (TypeError, ValueError):
            counters[field] = 0
    categories = source.get("categories")
    normalized_categories: dict[str, int] = {}
    if isinstance(categories, dict):
        for name, count in categories.items():
            try:
                normalized_categories[str(name)] = max(0, int(count or 0))
            except (TypeError, ValueError):
                continue
    return {
        **counters,
        "categories": normalized_categories,
    }


def normalize_central_usage_aggregate(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    aggregate: dict[str, Any] = {}
    for field in USAGE_COUNTER_FIELDS:
        try:
            aggregate[field] = max(0, int(source.get(field, 0) or 0))
        except (TypeError, ValueError):
            aggregate[field] = 0
    for field in USAGE_MAP_FIELDS:
        aggregate[field] = (
            dict(source[field]) if isinstance(source.get(field), dict) else {}
        )
    return aggregate


def get_central_device_status_payload(date_str: str) -> dict[str, Any]:
    """Adapt the central device view to the existing Dashboard response."""
    response = read_central_view("devices", date_str)
    records = response.get("devices")
    if not isinstance(records, list):
        raise CentralReadError(
            "invalid_response", "central devices response must contain an array",
        )
    local = local_desktop_device_descriptor()
    local_central_record: dict[str, Any] | None = None
    devices_by_id: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            continue
        device_id = str(raw.get("device_id") or "").strip()
        platform = str(raw.get("platform") or "").strip()
        if not device_id or not platform:
            continue
        if device_id == local["device_id"]:
            local_central_record = raw
            continue
        last_seen_at = raw.get("last_seen_at") or raw.get("last_received_at")
        window_stats = normalize_central_window_stats(
            raw.get("today")
            if isinstance(raw.get("today"), dict)
            else raw.get("window")
            if isinstance(raw.get("window"), dict)
            else raw
        )
        item = dict(raw)
        item.update({
            "device_key": device_storage_key(device_id, platform),
            "device_id": device_id,
            "display_name": str(raw.get("display_name") or device_id),
            "platform": platform,
            "is_local": False,
            "status": raw.get("status") if raw.get("status") in {"connected", "disconnected"} else "disconnected",
            "last_seen_at": last_seen_at,
            "last_received_at": last_seen_at,
            "last_connected_at": last_seen_at,
            "connection": "central",
            "today": window_stats,
            "window": window_stats,
        })
        devices_by_id[device_id] = item
    online_window = response.get("online_window_seconds", 600)
    try:
        online_window = max(1, int(online_window))
    except (TypeError, ValueError):
        online_window = 600
    devices = sorted(
        devices_by_id.values(),
        key=lambda item: item.get("last_seen_at") or "",
        reverse=True,
    )
    local_display_name = str(
        (local_central_record or {}).get("display_name")
        or local["display_name"]
    )
    return {
        "date": date_str,
        "timezone": str(DISPLAY_TIMEZONE),
        "online_window_seconds": online_window,
        "local": {
            "display_name": local_display_name,
            "hostname": get_hostname(),
            "device_key": local["device_key"],
            "device_id": local["device_id"],
            "platform": local["platform"],
            "status": "connected",
            "service": "running",
            "transport": "central_https",
            "api_version": "v1",
        },
        "devices": devices,
        "central_sync": dict(central_sync_status),
        "sync_mode": "central",
    }


def get_central_usage_payload(date_str: str) -> dict[str, Any]:
    """Adapt central usage aggregates while deriving local state from identity."""
    response = read_central_view("usage", date_str)
    records = response.get("devices")
    if not isinstance(records, list) or not isinstance(response.get("all"), dict):
        raise CentralReadError(
            "invalid_response",
            "central usage response must contain devices and all aggregates",
        )
    local_device_id = local_desktop_device_descriptor()["device_id"]
    devices_by_id: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            continue
        device_id = str(raw.get("device_id") or "").strip()
        platform = str(raw.get("platform") or "").strip()
        if not device_id or not platform:
            continue
        item = dict(raw)
        item.update(normalize_central_usage_aggregate(raw))
        item.update({
            "device_key": device_storage_key(device_id, platform),
            "device_id": device_id,
            "display_name": str(raw.get("display_name") or device_id),
            "platform": platform,
            "is_local": device_id == local_device_id,
        })
        devices_by_id[device_id] = item
    devices = sorted(
        devices_by_id.values(),
        key=lambda item: (not item["is_local"], item["display_name"].casefold()),
    )
    return {
        "date": date_str,
        "devices": devices,
        "all": normalize_central_usage_aggregate(response["all"]),
        "sync_interval_seconds": CENTRAL_SYNC_INTERVAL_SECONDS,
        "day_start_hour": get_day_start_hour(),
    }


def get_central_outbox() -> Outbox:
    global central_outbox_instance, central_outbox_instance_path
    requested_path = Path(CENTRAL_OUTBOX_PATH)
    with central_runtime_lock:
        if (
            central_outbox_instance is None
            or central_outbox_instance_path != requested_path
        ):
            if central_outbox_instance is not None:
                central_outbox_instance.close()
            central_outbox_instance = Outbox(requested_path)
            central_outbox_instance_path = requested_path
        return central_outbox_instance


def close_central_outbox() -> None:
    global central_outbox_instance, central_outbox_instance_path
    with central_runtime_lock:
        if central_outbox_instance is not None:
            central_outbox_instance.close()
        central_outbox_instance = None
        central_outbox_instance_path = None


def _store_native_events(events: list[dict[str, Any]]) -> dict[str, int]:
    if not events:
        return {"eligible": 0, "changed": 0, "unchanged": 0, "excluded": 0}
    return enqueue_local_central_events(events, provenance="windows_native")


def _save_native_checkpoint(collector: WindowsNativeCollector) -> None:
    get_central_outbox().set_metadata(
        NATIVE_CHECKPOINT_KEY,
        json.dumps(collector.checkpoint(), ensure_ascii=False, separators=(",", ":")),
    )


def native_collection_loop(stop_event: threading.Event) -> None:
    """Sample Windows locally and revise at most two open outbox rows."""
    global native_collector_instance, native_collector_latest_events, native_collector_status
    if not APP_USAGE_COLLECTION_ENABLED:
        with native_collector_lock:
            native_collector_status = {"status": "disabled", "collector": "windows_native"}
        return
    collector = WindowsNativeCollector()
    try:
        checkpoint = get_central_outbox().get_metadata(NATIVE_CHECKPOINT_KEY)
        if checkpoint:
            collector.restore(json.loads(checkpoint))
    except Exception:
        # A corrupt/stale checkpoint must never prevent fresh collection.
        collector = WindowsNativeCollector()
    with native_collector_lock:
        native_collector_instance = collector
        native_collector_status = {"status": "running", "collector": "windows_native"}
    last_snapshot = 0.0
    while not stop_event.is_set():
        try:
            closed = collector.sample()
            if closed:
                _store_native_events(closed)
            current = time.monotonic()
            if current - last_snapshot >= 5:
                snapshots = collector.snapshot()
                _store_native_events(snapshots)
                _save_native_checkpoint(collector)
                with native_collector_lock:
                    native_collector_latest_events = snapshots
                    native_collector_status = {
                        "status": "running", "collector": "windows_native",
                        "sampled_at": utc_timestamp(),
                    }
                last_snapshot = current
        except Exception as error:
            with native_collector_lock:
                native_collector_status = {
                    "status": "degraded", "collector": "windows_native", "error": str(error),
                }
        stop_event.wait(1)
    try:
        closed = collector.flush()
        _store_native_events(closed)
        _save_native_checkpoint(collector)
    finally:
        with native_collector_lock:
            native_collector_latest_events = []
            native_collector_status = {"status": "stopped", "collector": "windows_native"}
            native_collector_instance = None


def start_browser_receiver() -> AWWebCompatReceiver | None:
    global browser_receiver_instance, browser_receiver_status
    if not APP_USAGE_COLLECTION_ENABLED:
        browser_receiver_status = {"status": "disabled", "port": 5600}
        browser_receiver_instance = None
        return None
    receiver = AWWebCompatReceiver(
        lambda event: enqueue_local_central_events([event], provenance="browser_extension")
    )
    browser_receiver_status = receiver.start()
    browser_receiver_instance = receiver
    return receiver


def collection_runtime_status() -> dict[str, Any]:
    with native_collector_lock:
        native = dict(native_collector_status)
    return {"native": native, "browser": dict(browser_receiver_status)}


def is_central_local_event(event: dict[str, Any]) -> bool:
    """Allow only events generated by this PC, never received device data."""
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    collector = source.get("collector")
    if source.get("kind") != "desktop":
        return False
    if (
        event.get("event_type") == "app.foreground"
        and collector in {"activitywatch", "browser_extension"}
        and isinstance(payload.get("activitywatch"), dict)
    ):
        return True
    if collector == "windows_native" and event.get("event_type") in {
        "app.foreground", "device.input_state",
    }:
        return True
    if collector == "browser_extension" and event.get("event_type") == "web.foreground":
        return True
    return (
        event.get("event_type") == "custom.event"
        and collector == "life_radio_app"
    )


def central_wire_event(event: dict[str, Any], outbox: Outbox) -> dict[str, Any]:
    """Add a durable monotonic revision to mutable local collection facts."""
    document = {
        key: value for key, value in event.items()
        if isinstance(key, str) and not key.startswith("_")
    }
    if not is_central_local_event(document):
        raise ValueError("event is not a local PC upload source")
    payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
    source = document.get("source") if isinstance(document.get("source"), dict) else {}
    is_mutable_collection = (
        document.get("event_type") == "app.foreground"
        and isinstance(payload.get("activitywatch"), dict)
    ) or (
        source.get("collector") == "windows_native"
        and document.get("event_type") in {"app.foreground", "device.input_state"}
    ) or (
        source.get("collector") == "browser_extension"
        and document.get("event_type") == "web.foreground"
    )
    if not is_mutable_collection:
        document.setdefault("revision", 0)
        return document

    document.pop("revision", None)

    previous = outbox.event_version(str(document["event_id"]))
    previous_revision = 0
    if previous is not None:
        previous_revision = int(previous.get("revision", 0) or 0)
        comparison_hash = hashlib.sha256(json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if str(previous.get("content_hash") or "") == comparison_hash:
            document["revision"] = max(1, previous_revision)
            return document
    document["revision"] = max(1, previous_revision + 1)
    return document


CENTRAL_LOCAL_PROVENANCES = {
    "activitywatch", "windows_native", "browser_extension",
    "local_custom", "bootstrap_local_store",
}


def enqueue_local_central_events(
    events: list[dict[str, Any]], *, provenance: str,
) -> dict[str, int]:
    """Persist events only when a trusted local producer proves provenance."""
    result = {"eligible": 0, "changed": 0, "unchanged": 0, "excluded": 0}
    if provenance not in CENTRAL_LOCAL_PROVENANCES:
        result["excluded"] = len(events)
        return result
    outbox = get_central_outbox()
    with central_runtime_lock:
        for event in events:
            if not isinstance(event, dict) or not is_central_local_event(event):
                result["excluded"] += 1
                continue
            result["eligible"] += 1
            wire_event = central_wire_event(event, outbox)
            queued = outbox.upsert_event(wire_event)
            result["changed" if queued["changed"] else "unchanged"] += 1
    return result


def bootstrap_local_central_outbox() -> dict[str, Any]:
    """One-time backfill of this PC's current and previous business day."""
    outbox = get_central_outbox()
    active_id = local_desktop_device_descriptor()["device_id"]
    marker_key = f"two_business_day_bootstrap:{active_id}:v1"
    if outbox.get_metadata(marker_key) == "complete":
        return {"status": "complete", "scanned": 0, "changed": 0}

    current_date = datetime.fromisoformat(display_date_today()).date()
    allowed_dates = {
        current_date.isoformat(),
        (current_date - timedelta(days=1)).isoformat(),
    }
    local_device_ids = {active_id}
    scanned = 0
    changed = 0
    for path in iter_device_event_files():
        data = load_json(path, {})
        if not isinstance(data, dict) or not isinstance(data.get("device"), dict):
            continue
        if str(data["device"].get("device_id", "")) not in local_device_ids:
            continue
        for batch in data.get("batches", []):
            if not isinstance(batch, dict):
                continue
            for event in batch.get("events", []):
                if (
                    not isinstance(event, dict)
                    or not is_central_local_event(event)
                    or event_date(event) not in allowed_dates
                ):
                    continue
                scanned += 1
                queued = enqueue_local_central_events(
                    [event], provenance="bootstrap_local_store",
                )
                changed += queued["changed"]
    outbox.set_metadata(marker_key, "complete")
    return {"status": "completed", "scanned": scanned, "changed": changed}


def prune_acked_local_device_history(*, now: datetime | None = None) -> dict[str, int]:
    """Retire this PC's ACKed AW mirrors; custom/local facts stay untouched."""
    global seen_event_ids_cache
    active_id = local_desktop_device_descriptor()["device_id"]
    outbox = get_central_outbox()
    removed_files = 0
    bytes_removed = 0
    with data_lock:
        for path in iter_device_event_files():
            data = load_json(path, {})
            device = data.get("device") if isinstance(data, dict) else None
            if not isinstance(device, dict) or str(device.get("device_id") or "") != active_id:
                continue
            if str(data.get("event_type") or "") != "app.foreground":
                continue
            event_ids = {
                str(event.get("event_id") or event.get("id") or "")
                for batch in data.get("batches", [])
                if isinstance(batch, dict)
                for event in batch.get("events", [])
                if isinstance(event, dict)
            }
            event_ids.discard("")
            if not outbox.all_events_acked(event_ids):
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            removed_files += 1
            bytes_removed += size
        if removed_files:
            seen_event_ids_cache = None
    return {"removed_files": removed_files, "bytes_removed": bytes_removed}


def compact_local_outbox(*, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """Daily physical compaction without tying revision memory to ActivityWatch."""
    current = now or utc_now()
    outbox = get_central_outbox()
    marker = "last_outbox_compaction:v1"
    today = current.astimezone(DISPLAY_TIMEZONE).date().isoformat()
    if not force and outbox.get_metadata(marker) == today:
        return {"status": "skipped", "events_compacted": 0, "batches_removed": 0}
    result = outbox.compact_confirmed(
        event_types={"app.foreground", "device.input_state", "web.foreground"},
        completed_before=current - timedelta(days=2),
        vacuum=True,
    )
    outbox.set_metadata(marker, today, now=current)
    return {"status": "completed", **result}


def activitywatch_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return utc_timestamp(parsed)


def get_central_sync_payload() -> dict[str, Any]:
    payload = {
        **central_sync_status,
        "mode": "central",
        "configured": bool(CENTRAL_BASE_URL and get_central_token()),
        "central_base_url": CENTRAL_BASE_URL,
        "device": local_desktop_device_descriptor(),
    }
    try:
        payload["outbox"] = get_central_outbox().status()
    except Exception as error:
        payload["outbox_error"] = str(error)
    return payload


def sync_central_once(force_retry: bool = False) -> dict[str, Any]:
    with central_sync_lock:
        started_at = utc_timestamp()
        central_sync_status.update({
            "state": "running", "last_started_at": started_at,
        })
        try:
            # This runs on startup and on the existing central sync cadence.
            # It is deliberately independent of upload success and never
            # clears a prior read-only cache on a failed refresh.
            refresh_shared_settings()
            collection = collection_runtime_status()
            bootstrap = bootstrap_local_central_outbox()
            outbox = get_central_outbox()
            token = get_central_token()
            if not CENTRAL_BASE_URL or not token:
                result = {
                    "mode": "central",
                    "collection": collection,
                    "bootstrap": bootstrap,
                    "uploads": [],
                    "outbox": outbox.status(),
                    "error": (
                        "LIFE_RADIO_CENTRAL_BASE_URL and "
                        "LIFE_RADIO_CENTRAL_TOKEN are required"
                    ),
                }
            else:
                client = CENTRAL_CLIENT_CLASS(CENTRAL_BASE_URL, token)
                uploads: list[dict[str, Any]] = []
                for _ in range(CENTRAL_MAX_BATCHES_PER_RUN):
                    upload = client.sync_once(
                        outbox,
                        local_desktop_device_descriptor(),
                        force_retry=force_retry,
                    )
                    uploads.append(upload)
                    if upload.get("status") != "ok":
                        break
                    force_retry = False
                result = {
                    "mode": "central",
                    "collection": collection,
                    "bootstrap": bootstrap,
                    "uploads": uploads,
                    "outbox": outbox.status(),
                    "error": next(
                        (
                            str(upload.get("error"))
                            for upload in uploads
                            if upload.get("error")
                        ),
                        None,
                    ),
                }
            try:
                result["retention"] = prune_acked_local_device_history()
            except Exception as retention_error:
                result["retention"] = {
                    "removed_files": 0,
                    "bytes_removed": 0,
                    "error": str(retention_error),
                }
            try:
                result["outbox_compaction"] = compact_local_outbox()
            except Exception as compaction_error:
                result["outbox_compaction"] = {
                    "status": "failed",
                    "events_compacted": 0,
                    "batches_removed": 0,
                    "error": str(compaction_error),
                }
        except Exception as error:
            result = {
                "mode": "central", "uploads": [], "error": str(error),
            }
        finished_at = utc_timestamp()
        central_sync_status.update({
            "state": "idle", "last_finished_at": finished_at,
            "last_result": result,
        })
        return result


def central_sync_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            sync_central_once()
        except Exception as error:
            central_sync_status.update({"state": "idle", "last_finished_at": utc_timestamp(), "last_result": {"error": str(error)}})
        stop_event.wait(CENTRAL_SYNC_INTERVAL_SECONDS)


def display_date_today() -> str:
    return business_date(utc_now())


def event_matches_display_date(event: dict[str, Any], date_str: str) -> bool:
    occurred = parse_utc_datetime(event.get("occurred_at"))
    return occurred is not None and business_date(occurred) == date_str


def create_local_custom_event(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and persist one event generated by the local desktop app."""
    event_key = payload.get("event_key")
    title = payload.get("title")
    detail = payload.get("detail", "")
    metadata = payload.get("metadata", {})
    if not isinstance(event_key, str) or not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", event_key):
        return None, "event_key must be a lowercase dotted identifier"
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        return None, "title must contain 1-120 characters"
    if not isinstance(detail, str) or len(detail) > 500:
        return None, "detail must contain at most 500 characters"
    if not isinstance(metadata, dict):
        return None, "metadata must be an object"

    occurred_at = utc_timestamp()
    event = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": occurred_at,
        "event_type": "custom.event",
        "source": {
            "kind": "desktop",
            "collector": "life_radio_app",
            "reliability": "observed",
        },
        "payload": {
            "event_key": event_key,
            "title": title.strip(),
            "detail": detail.strip(),
            "metadata": metadata,
        },
        "_received_at": occurred_at,
    }
    device = local_desktop_device_descriptor()
    accepted, duplicates = save_events(
        [event],
        device=device,
        batch_metadata={
            "schema_version": "v1-local",
            "batch_id": f"custom-{uuid.uuid4()}",
            "received_at": occurred_at,
            "source": device["display_name"],
            "source_type": "desktop",
            "data_type": "custom.event",
        },
    )
    if not accepted or duplicates:
        return None, "custom event could not be stored"
    enqueue_local_central_events([event], provenance="local_custom")
    return event, None


def get_custom_event_summary(date_str: str) -> dict[str, Any]:
    """Return custom events in reverse chronological order."""
    events: list[dict[str, Any]] = []
    devices: dict[str, dict[str, Any]] = {}
    metadata_by_key = {
        str(item.get("device_key")): item
        for item in load_device_metadata()
        if isinstance(item.get("device_key"), str)
    }
    for path in iter_device_event_files():
        data = load_json(path, {"batches": []})
        device = data.get("device") if isinstance(data, dict) else None
        if not isinstance(device, dict) or not device.get("device_key"):
            continue
        key = str(device["device_key"])
        for batch in data.get("batches", []):
            if not isinstance(batch, dict):
                continue
            for event in batch.get("events", []):
                if (
                    not isinstance(event, dict)
                    or event.get("event_type") != "custom.event"
                    or not event_matches_display_date(event, date_str)
                ):
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                events.append({
                    "event_id": event.get("event_id"),
                    "device_key": key,
                    "device_name": device.get("display_name") or device.get("device_id") or "unknown",
                    "occurred_at": event.get("occurred_at"),
                    "received_at": event.get("_received_at"),
                    "event_key": payload.get("event_key") or "custom.unknown",
                    "title": payload.get("title") or "自定义事件",
                    "detail": payload.get("detail") or "",
                    "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                })
                stored_metadata = metadata_by_key.get(key, {})
                current = devices.setdefault(key, {
                    "device_key": key,
                    "display_name": device.get("display_name") or device.get("device_id") or "unknown",
                    "platform": device.get("platform", "unknown"),
                    "is_local": is_local_device_metadata(device),
                    "event_count": 0,
                    "last_event_at": None,
                    "last_connected_at": stored_metadata.get("last_connected_at") or stored_metadata.get("last_received_at"),
                })
                current["event_count"] += 1
                if (event.get("occurred_at") or "") > (current["last_event_at"] or ""):
                    current["last_event_at"] = event.get("occurred_at")
    events.sort(key=lambda item: item.get("occurred_at") or "", reverse=True)
    return {
        "date": date_str,
        "timezone": str(DISPLAY_TIMEZONE),
        "devices": sorted(
            devices.values(),
            key=lambda item: (not item["is_local"], item["display_name"].casefold()),
        ),
        "events": events,
    }


def add_duration_to_hourly(target: dict[str, int], occurred: datetime | None, amount: int) -> None:
    """Split a UTC duration across local clock-hour buckets."""
    if not occurred or amount <= 0:
        return
    cursor = occurred.astimezone(DISPLAY_TIMEZONE)
    remaining = amount
    while remaining > 0:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment = min(remaining, max(1, int((next_hour - cursor).total_seconds())))
        key_hour = str(cursor.hour)
        target[key_hour] = target.get(key_hour, 0) + segment
        remaining -= segment
        cursor = next_hour


def add_site_duration_to_hourly(
    target: dict[str, dict[str, int]], domain: str, occurred: datetime | None, amount: int,
) -> None:
    """Split one inferred site duration across local clock hours."""
    if not occurred or amount <= 0:
        return
    cursor = occurred.astimezone(DISPLAY_TIMEZONE)
    remaining = amount
    while remaining > 0:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment = min(remaining, max(1, int((next_hour - cursor).total_seconds())))
        sites_in_hour = target.setdefault(str(cursor.hour), {})
        sites_in_hour[domain] = sites_in_hour.get(domain, 0) + segment
        remaining -= segment
        cursor = next_hour


def merge_time_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals, key=lambda item: item[0]):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(item[0], item[1]) for item in merged]


def subtract_time_intervals(
    start: datetime,
    end: datetime,
    exclusions: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Remove explicit AFK overlaps from a foreground interval."""
    pieces: list[tuple[datetime, datetime]] = []
    cursor = start
    for excluded_start, excluded_end in exclusions:
        if excluded_end <= cursor:
            continue
        if excluded_start >= end:
            break
        if excluded_start > cursor:
            pieces.append((cursor, min(excluded_start, end)))
        cursor = max(cursor, excluded_end)
        if cursor >= end:
            break
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def add_named_duration_to_hourly(
    target: dict[str, dict[str, int]], name: str, occurred: datetime, amount: int,
) -> None:
    if amount <= 0:
        return
    cursor = occurred.astimezone(DISPLAY_TIMEZONE)
    remaining = amount
    while remaining > 0:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment = min(remaining, max(1, int((next_hour - cursor).total_seconds())))
        values = target.setdefault(str(cursor.hour), {})
        values[name] = values.get(name, 0) + segment
        remaining -= segment
        cursor = next_hour


def is_chrome_window(app: dict[str, Any], activitywatch: dict[str, Any]) -> bool:
    """Identify foreground browser intervals used for domain attribution."""
    raw = activitywatch.get("data") if isinstance(activitywatch.get("data"), dict) else {}
    candidates = (
        app.get("package_name"), app.get("process_name"), app.get("display_name"), raw.get("app")
    )
    return any(is_browser_app_name(value) for value in candidates if value)


def is_browser_app_name(value: Any) -> bool:
    lowered = str(value or "").casefold()
    return any(name in lowered for name in ("chrome", "msedge", "firefox", "brave", "vivaldi", "opera"))


def is_chrome_web_marker(activitywatch: dict[str, Any]) -> bool:
    return "web-chrome" in str(activitywatch.get("bucket_id") or "").lower()


def derive_chrome_domain_segments(
    chrome_windows: list[tuple[datetime, int]],
    web_markers: list[tuple[datetime, str]],
    non_chrome_windows: list[tuple[datetime, int]] | None = None,
) -> list[tuple[str, datetime, int]]:
    """Label real Chrome foreground intervals with the latest URL observation.

    ActivityWatch can split one foreground Chrome session into many records
    separated by watcher gaps. When all window events are available, only an
    observed non-Chrome foreground application ends the labeling session.
    Callers without those events retain the conservative five-second fallback.
    Only real Chrome intervals are emitted, so watcher gaps are never counted.
    """
    sessions = chrome_window_sessions(chrome_windows, non_chrome_windows)
    segments: list[tuple[str, datetime, int]] = []
    markers = sorted(web_markers, key=lambda item: item[0])
    for session_intervals in sessions:
        session_start = session_intervals[0][0]
        session_end = session_intervals[-1][1]
        session_markers = chrome_session_web_markers(
            session_start, session_end, markers,
        )
        marker_index = 0
        current_domain: str | None = None
        for interval_start, interval_end in session_intervals:
            while (
                marker_index < len(session_markers)
                and session_markers[marker_index][0] <= interval_start
            ):
                current_domain = session_markers[marker_index][1]
                marker_index += 1

            cursor = interval_start
            while (
                marker_index < len(session_markers)
                and session_markers[marker_index][0] < interval_end
            ):
                marker_time, next_domain = session_markers[marker_index]
                if current_domain and marker_time > cursor:
                    seconds = int((marker_time - cursor).total_seconds())
                    if seconds > 0:
                        segments.append((current_domain, cursor, seconds))
                current_domain = next_domain
                cursor = max(cursor, marker_time)
                marker_index += 1

            if current_domain and interval_end > cursor:
                seconds = int((interval_end - cursor).total_seconds())
                if seconds > 0:
                    segments.append((current_domain, cursor, seconds))
    return segments


def chrome_session_web_markers(
    session_start: datetime,
    session_end: datetime,
    web_markers: list[tuple[datetime, str]],
) -> list[tuple[datetime, str]]:
    """Return markers for one Chrome session, including a recent resume label."""
    markers = sorted(
        [
            (timestamp, domain) for timestamp, domain in web_markers
            if session_start - timedelta(seconds=2) <= timestamp < session_end and domain
        ],
        key=lambda item: item[0],
    )
    previous = max(
        (
            (timestamp, domain) for timestamp, domain in web_markers
            if (
                session_start - timedelta(seconds=CHROME_URL_RESUME_GRACE_SECONDS)
                <= timestamp < session_start - timedelta(seconds=2)
                and domain
            )
        ),
        key=lambda item: item[0],
        default=None,
    )
    if previous is not None:
        markers.insert(0, (session_start, previous[1]))
    return markers


def chrome_window_sessions(
    chrome_windows: list[tuple[datetime, int]],
    non_chrome_windows: list[tuple[datetime, int]] | None = None,
) -> list[list[list[datetime]]]:
    """Group Chrome intervals without treating watcher fragmentation as a switch."""
    intervals = sorted(
        [(start, start + timedelta(seconds=duration)) for start, duration in chrome_windows if duration > 0],
        key=lambda item: item[0],
    )
    sessions: list[list[list[datetime]]] = []
    if non_chrome_windows is not None:
        timeline = [
            (start, 0, start + timedelta(seconds=max(0, duration)))
            for start, duration in non_chrome_windows
            if duration >= CHROME_SWITCH_MIN_SECONDS
        ] + [
            (start, 1, end)
            for start, end in intervals
        ]
        current_session: list[list[datetime]] | None = None
        for start, kind, end in sorted(timeline, key=lambda item: (item[0], item[1])):
            if kind == 0:
                current_session = None
                continue
            if current_session is None:
                current_session = []
                sessions.append(current_session)
            if current_session and start <= current_session[-1][1]:
                current_session[-1][1] = max(current_session[-1][1], end)
            else:
                current_session.append([start, end])
        return [session for session in sessions if session]

    for start, end in intervals:
        if sessions and start <= sessions[-1][-1][1] + timedelta(seconds=5):
            if start <= sessions[-1][-1][1]:
                sessions[-1][-1][1] = max(sessions[-1][-1][1], end)
            else:
                sessions[-1].append([start, end])
        else:
            sessions.append([[start, end]])
    return sessions


def latest_chrome_domain(
    chrome_windows: list[tuple[datetime, int]],
    non_chrome_windows: list[tuple[datetime, int]],
    web_markers: list[tuple[datetime, str]],
    sampled_at: datetime,
) -> str | None:
    """Return the last URL label in the currently active Chrome session."""
    sessions = chrome_window_sessions(chrome_windows, non_chrome_windows)
    if not sessions:
        return None
    session = sessions[-1]
    session_start = session[0][0]
    session_end = session[-1][1]
    candidates = chrome_session_web_markers(
        session_start,
        min(sampled_at + timedelta(microseconds=1), session_end + timedelta(seconds=20)),
        web_markers,
    )
    candidates = [
        (timestamp, domain) for timestamp, domain in candidates
        if timestamp <= sampled_at
    ]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def get_usage_summary(date_str: str) -> dict[str, Any]:
    """Aggregate durable AW/mobile usage for the multi-device usage page."""
    devices: dict[str, dict[str, Any]] = {}
    chrome_windows: defaultdict[str, list[tuple[datetime, int]]] = defaultdict(list)
    non_chrome_windows: defaultdict[str, list[tuple[datetime, int]]] = defaultdict(list)
    chrome_markers: defaultdict[str, list[tuple[datetime, str]]] = defaultdict(list)
    afk_intervals: defaultdict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    window_intervals: defaultdict[
        str, list[tuple[datetime, datetime, str, bool]]
    ] = defaultdict(list)
    day_start, day_end = business_day_bounds(date_str)
    marker_floor = day_start - timedelta(seconds=CHROME_URL_RESUME_GRACE_SECONDS)
    for path in iter_device_event_files():
        data = load_json(path, {"batches": []})
        device = data.get("device") if isinstance(data, dict) else None
        if not isinstance(device, dict) or not device.get("device_key"):
            continue
        key = str(device["device_key"])
        summary = devices.setdefault(key, {
            "device_key": key, "display_name": device.get("display_name", key),
            "platform": device.get("platform", "unknown"), "is_local": is_local_device_metadata(device),
            "events": 0, "window_events": 0, "web_events": 0, "afk_seconds": 0,
            "apps": {}, "hourly": {}, "hourly_apps": {}, "hourly_online": {}, "sites": {}, "hourly_sites": {},
        })
        for batch in data.get("batches", []):
            if not isinstance(batch, dict):
                continue
            for event in batch.get("events", []):
                if not isinstance(event, dict):
                    continue
                # Usage statistics have one explicit fact type.  Location and
                # other semantic events may also carry a duration, but that is
                # a stay/segment duration rather than foreground app use.
                if event.get("event_type") != "app.foreground":
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                legacy_payload = payload.get("legacy_data") if isinstance(payload.get("legacy_data"), dict) else {}
                aw = payload.get("activitywatch") if isinstance(payload.get("activitywatch"), dict) else legacy_payload.get("activitywatch", {})
                if not isinstance(aw, dict):
                    aw = {}
                kind = str(aw.get("kind") or "window")
                duration = max(0, int(event.get("duration_seconds", 0) or 0))
                occurred = parse_utc_datetime(event.get("occurred_at"))
                if kind == "web":
                    if occurred is None or not marker_floor <= occurred < day_end:
                        continue
                    summary["events"] += 1
                    summary["web_events"] += 1
                    raw = aw.get("data") if isinstance(aw.get("data"), dict) else legacy_payload.get("activitywatch", {}).get("data", {})
                    url = str(raw.get("url") or "") if isinstance(raw, dict) else ""
                    domain = parse_url(url).netloc.lower().removeprefix("www.")
                    if str(device.get("platform", "")).casefold() == "desktop" and domain and is_chrome_web_marker(aw):
                        chrome_markers[key].append((occurred, domain))
                    continue
                if occurred is None:
                    continue
                clipped_start = max(occurred, day_start)
                clipped_end = min(occurred + timedelta(seconds=duration), day_end)
                if clipped_end <= clipped_start:
                    continue
                summary["events"] += 1
                clipped_seconds = int((clipped_end - clipped_start).total_seconds())
                if kind == "afk":
                    summary["afk_seconds"] += clipped_seconds
                    raw = aw.get("data") if isinstance(aw.get("data"), dict) else {}
                    if (
                        str(device.get("platform", "")).casefold() == "desktop"
                        and raw.get("status") == "afk"
                    ):
                        afk_intervals[key].append((clipped_start, clipped_end))
                    continue
                summary["window_events"] += 1
                app = payload.get("app") if isinstance(payload.get("app"), dict) else legacy_payload.get("app", {})
                if not isinstance(app, dict):
                    app = {}
                name = str(app.get("display_name") or app.get("package_name") or "未识别应用")
                window_intervals[key].append(
                    (clipped_start, clipped_end, name, is_chrome_window(app, aw))
                )
    for key, summary in devices.items():
        platform = str(summary.get("platform", "")).casefold()
        exclusions = merge_time_intervals(afk_intervals[key]) if platform == "desktop" else []
        retained: list[tuple[datetime, datetime]] = []
        for interval_start, interval_end, name, is_chrome in window_intervals[key]:
            for piece_start, piece_end in subtract_time_intervals(
                interval_start, interval_end, exclusions,
            ):
                seconds = int((piece_end - piece_start).total_seconds())
                if seconds <= 0:
                    continue
                summary["apps"][name] = summary["apps"].get(name, 0) + seconds
                add_named_duration_to_hourly(summary["hourly_apps"], name, piece_start, seconds)
                retained.append((piece_start, piece_end))
                if platform == "desktop":
                    target = chrome_windows if is_chrome else non_chrome_windows
                    target[key].append((piece_start, seconds))
        for interval_start, interval_end in merge_time_intervals(retained):
            seconds = int((interval_end - interval_start).total_seconds())
            add_duration_to_hourly(summary["hourly"], interval_start, seconds)
            # Compatibility alias for older dashboard clients.
            add_duration_to_hourly(summary["hourly_online"], interval_start, seconds)
        for domain, occurred, duration in derive_chrome_domain_segments(
            chrome_windows[key], chrome_markers[key], non_chrome_windows[key],
        ):
            summary["sites"][domain] = summary["sites"].get(domain, 0) + duration
            add_site_duration_to_hourly(summary["hourly_sites"], domain, occurred, duration)

    ordered = sorted(devices.values(), key=lambda item: (not item["is_local"], item["display_name"].casefold()))
    total = {"events": 0, "window_events": 0, "web_events": 0, "afk_seconds": 0, "apps": {}, "hourly": {}, "hourly_apps": {}, "hourly_online": {}, "sites": {}, "hourly_sites": {}}
    for item in ordered:
        for field in ("events", "window_events", "web_events", "afk_seconds"):
            total[field] += item[field]
        for name, seconds in item["apps"].items():
            total["apps"][name] = total["apps"].get(name, 0) + seconds
        for hour, seconds in item["hourly"].items():
            total["hourly"][hour] = total["hourly"].get(hour, 0) + seconds
        for hour, seconds in item["hourly_online"].items():
            total["hourly_online"][hour] = total["hourly_online"].get(hour, 0) + seconds
        for hour, apps in item["hourly_apps"].items():
            total_apps = total["hourly_apps"].setdefault(hour, {})
            for name, seconds in apps.items():
                total_apps[name] = total_apps.get(name, 0) + seconds
        for site, seconds in item["sites"].items():
            total["sites"][site] = total["sites"].get(site, 0) + seconds
        for hour, sites in item["hourly_sites"].items():
            total_sites = total["hourly_sites"].setdefault(hour, {})
            for site, seconds in sites.items():
                total_sites[site] = total_sites.get(site, 0) + seconds
    return {
        "date": date_str,
        "devices": ordered,
        "all": total,
        "sync_interval_seconds": CENTRAL_SYNC_INTERVAL_SECONDS,
        "day_start_hour": get_day_start_hour(),
    }


def completed_local_usage_totals(summary: dict[str, Any], current_hour: int) -> tuple[int, int]:
    """Return local-PC usage and blacklist totals before the current hour."""
    device = next(
        (
            item for item in summary.get("devices", [])
            if isinstance(item, dict) and item.get("is_local")
        ),
        None,
    )
    if not isinstance(device, dict):
        return 0, 0

    current_key = str(current_hour)
    hourly = device.get("hourly") if isinstance(device.get("hourly"), dict) else {}
    hourly_apps = (
        device.get("hourly_apps") if isinstance(device.get("hourly_apps"), dict) else {}
    )
    hourly_sites = (
        device.get("hourly_sites") if isinstance(device.get("hourly_sites"), dict) else {}
    )
    usage_total = 0
    blacklist_total = 0
    for hour, seconds in hourly.items():
        if str(hour) == current_key:
            continue
        try:
            usage_seconds = max(0, int(seconds or 0))
        except (TypeError, ValueError):
            continue
        usage_total += usage_seconds
        app_black = sum(
            int(value or 0) for name, value in (hourly_apps.get(str(hour), {}) or {}).items()
            if is_blacklisted_app(str(name))
        )
        site_black = sum(
            int(value or 0) for name, value in (hourly_sites.get(str(hour), {}) or {}).items()
            if is_blacklisted_site(str(name))
        )
        blacklist_total += min(usage_seconds, max(0, app_black + site_black))
    return usage_total, blacklist_total


def get_cached_completed_local_usage_totals(
    date_str: str, current_hour: int,
) -> tuple[int, int]:
    """Cache stable completed-hour totals so the 2s live poll stays lightweight."""
    global live_daily_cache
    now_monotonic = time.monotonic()
    with live_daily_cache_lock:
        if (
            live_daily_cache is not None
            and live_daily_cache[1] == date_str
            and live_daily_cache[2] == current_hour
            and now_monotonic - live_daily_cache[0] < 60
        ):
            return live_daily_cache[3]
        totals = completed_local_usage_totals(get_central_usage_payload(date_str), current_hour)
        live_daily_cache = (now_monotonic, date_str, current_hour, totals)
        return totals


def business_day_bounds(date_str: str) -> tuple[datetime, datetime]:
    date_value = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.combine(date_value, datetime.min.time(), tzinfo=DISPLAY_TIMEZONE)
    start += timedelta(hours=get_day_start_hour())
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


_BLACKLIST_MEMORY_APPS: list[str] = []
_BLACKLIST_MEMORY_DOMAINS: list[str] = []
_BLACKLIST_MEMORY_RULES: list[dict[str, Any]] | None = None
_BLACKLIST_MEMORY_TIMESTAMP = 0.0
_BLACKLIST_MEMORY_LOADED = False  # True once central or persistent cache has been consulted
BLACKLIST_CACHE_TTL_SECONDS = 300


def _load_blacklist_for_matching() -> tuple[list[str], list[str]]:
    """Return (app_patterns, domain_patterns) from in-process cache or central.

    Cache holds for BLACKLIST_CACHE_TTL_SECONDS.  On expiry the central
    service is queried once; the result is persisted to disk and used in
    process for the next TTL window.  This avoids hundreds of HTTP requests
    during a single dashboard render.
    """
    now = time.monotonic()
    if _BLACKLIST_MEMORY_TIMESTAMP and (now - _BLACKLIST_MEMORY_TIMESTAMP) < BLACKLIST_CACHE_TTL_SECONDS:
        return list(_BLACKLIST_MEMORY_APPS), list(_BLACKLIST_MEMORY_DOMAINS)
    _refresh_blacklist_memory()
    return list(_BLACKLIST_MEMORY_APPS), list(_BLACKLIST_MEMORY_DOMAINS)


def _refresh_blacklist_memory() -> None:
    """Pull rules from central (or persistent cache) and populate the in-process caches."""
    global _BLACKLIST_MEMORY_APPS, _BLACKLIST_MEMORY_DOMAINS, _BLACKLIST_MEMORY_RULES, _BLACKLIST_MEMORY_TIMESTAMP, _BLACKLIST_MEMORY_LOADED
    rules: list[dict[str, Any]] | None = None
    try:
        rules = _read_central_blacklist_rules()
        save_cached_blacklist(rules)
        _BLACKLIST_MEMORY_LOADED = True
    except (CentralReadConfigurationError, CentralReadError):
        pass
    if rules is None:
        cached = load_cached_blacklist()
        if cached is not None:
            rules = cached.get("rules")
            if isinstance(rules, list):
                _BLACKLIST_MEMORY_LOADED = True
    if not isinstance(rules, list):
        rules = []
    _BLACKLIST_MEMORY_RULES = rules
    _BLACKLIST_MEMORY_APPS = [
        str(r.get("normalized_pattern", r.get("pattern", ""))) for r in rules
        if isinstance(r, dict) and r.get("rule_type") == "app" and r.get("enabled") is not False
        and (r.get("platform_scope") or "pc") == "pc"
    ]
    _BLACKLIST_MEMORY_DOMAINS = [
        str(r.get("normalized_pattern", r.get("pattern", ""))) for r in rules
        if isinstance(r, dict) and r.get("rule_type") == "domain" and r.get("enabled") is not False
        and (r.get("platform_scope") or "web") == "web"
    ]
    _BLACKLIST_MEMORY_TIMESTAMP = time.monotonic()

def invalidate_blacklist_memory() -> None:
    """Force the next matching call to re-read from central."""
    global _BLACKLIST_MEMORY_TIMESTAMP
    _BLACKLIST_MEMORY_TIMESTAMP = 0.0


def _match_path(path: str, pattern: str) -> re.Match | None:
    """Safe regex match helper."""
    import re
    return re.match(pattern, path)


# ---- Persistent central resource read cache ----
_V17_READ_MEMORY: dict[str, tuple[float, dict[str, Any], bool]] = {}
_CACHE_TTL = 30.0  # seconds
_V17_CACHE_LOCK = threading.RLock()
_V17_READ_LOCKS = tuple(threading.RLock() for _ in range(16))
_V17_DISK_CACHE_KEY: tuple[str, str] | None = None
_V17_DISK_CACHE: dict[str, dict[str, Any]] = {}
_V17_CACHE_VERSION = 2
_V17_CACHE_MAX_ENTRIES = 32
_V17_CACHE_MAX_ENTRIES_PER_RESOURCE = 4
_V17_CACHE_MAX_FILE_BYTES = 8 * 1024 * 1024
_V17_CACHE_MAX_ENTRY_BYTES = 2 * 1024 * 1024


def v17_read_cache_path() -> Path:
    return DATA_DIR / V17_READ_CACHE_FILENAME


def cleanup_orphan_v17_cache_temporary_files() -> dict[str, int]:
    """Remove only abandoned atomic-write files for the bounded v1.7 cache."""
    removed = 0
    bytes_removed = 0
    if not DATA_DIR.exists():
        return {"removed": 0, "bytes_removed": 0}
    for path in DATA_DIR.glob(f".{V17_READ_CACHE_FILENAME}.*.tmp"):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        removed += 1
        bytes_removed += size
    return {"removed": removed, "bytes_removed": bytes_removed}


def _canonical_v17_cache_key(path: str) -> str:
    parsed = urlparse(path)
    pairs = sorted(parse_qs(parsed.query, keep_blank_values=True).items())
    query = urlencode(
        [(key, value) for key, values in pairs for value in sorted(values)],
        doseq=True,
    )
    return parsed.path + (f"?{query}" if query else "")


def _v17_resource_name(path: str) -> str:
    return _canonical_v17_cache_key(path).split("?", 1)[0]


def _valid_v17_payload(path: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    plain_path = path.split("?", 1)[0]
    if plain_path == "/v1/devices":
        devices = payload.get("devices")
        return isinstance(devices, list) and all(
            isinstance(device, dict)
            and isinstance(device.get("device_id"), str)
            and device.get("platform") in {"android", "desktop", "web"}
            and isinstance(device.get("display_name"), str)
            and isinstance(device.get("reported_name"), str)
            and (device.get("custom_name") is None or isinstance(device.get("custom_name"), str))
            and isinstance(device.get("is_current"), bool)
            for device in devices
        )
    if plain_path == "/v1/wishes":
        return isinstance(payload.get("wishes"), list)
    if plain_path.startswith("/v1/wishes/"):
        return isinstance(payload.get("wish_id"), str) and isinstance(payload.get("wish_days"), list)
    if plain_path == "/v1/timeline-events":
        return isinstance(payload.get("window"), dict) and isinstance(payload.get("events"), list)
    if plain_path == "/v1/trigger-types":
        return isinstance(payload.get("trigger_types"), list)
    if plain_path == "/v1/event-triggers":
        return isinstance(payload.get("triggers"), list)
    if plain_path == "/v1/event-background":
        summary = payload.get("background_summary")
        guide = payload.get("ai_understanding")
        return (
            isinstance(payload.get("business_date"), str)
            and isinstance(payload.get("generated_at"), str)
            and isinstance(summary, dict)
            and isinstance(guide, dict)
            and isinstance(payload.get("real_time_items"), list)
            and all(isinstance(summary.get(key), dict) for key in ("wish", "device_and_apps", "blacklist", "location_and_activity"))
            and isinstance(guide.get("items"), list)
            and guide.get("timezone") == "Asia/Shanghai"
            and guide.get("real_time_valid_for_minutes") == 15
        )
    return False


def _trim_v17_entries(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    retained: dict[str, dict[str, Any]] = {}
    resource_counts: defaultdict[str, int] = defaultdict(int)
    for path, payload in reversed(list(entries.items())):
        resource = _v17_resource_name(path)
        if resource_counts[resource] >= _V17_CACHE_MAX_ENTRIES_PER_RESOURCE:
            continue
        retained[path] = payload
        resource_counts[resource] += 1
        if len(retained) >= _V17_CACHE_MAX_ENTRIES:
            break
    return dict(reversed(list(retained.items())))


def _v17_cache_document(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": _V17_CACHE_VERSION,
        "central_base_url": CENTRAL_BASE_URL.rstrip("/") if CENTRAL_BASE_URL else "",
        "entries": entries,
    }


def _v17_json_size(value: Any) -> int:
    return len((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _v17_payload_etag(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f'"{hashlib.sha256(canonical.encode("utf-8")).hexdigest()}"'


def _v17_read_lock(cache_key: str) -> threading.RLock:
    digest = hashlib.sha256(cache_key.encode("utf-8")).digest()
    return _V17_READ_LOCKS[digest[0] % len(_V17_READ_LOCKS)]


def _fit_v17_file_budget(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    retained = dict(entries)
    while retained:
        if _v17_json_size(_v17_cache_document(retained)) <= _V17_CACHE_MAX_FILE_BYTES:
            break
        retained.pop(next(iter(retained)))
    return retained


def _quarantine_oversized_v17_cache(target: Path) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.stem}.oversized-{timestamp}.bak")
    suffix = 1
    while backup.exists():
        backup = target.with_name(f"{target.stem}.oversized-{timestamp}-{suffix}.bak")
        suffix += 1
    target.replace(backup)


def _load_v17_read_cache() -> dict[str, Any]:
    global _V17_DISK_CACHE_KEY, _V17_DISK_CACHE
    target = v17_read_cache_path()
    expected_base = CENTRAL_BASE_URL.rstrip("/") if CENTRAL_BASE_URL else ""
    state_key = (str(target.resolve()), expected_base)
    with _V17_CACHE_LOCK:
        if _V17_DISK_CACHE_KEY == state_key:
            return dict(_V17_DISK_CACHE)
        try:
            if target.exists() and target.stat().st_size > _V17_CACHE_MAX_FILE_BYTES:
                _quarantine_oversized_v17_cache(target)
                document: Any = {}
            else:
                document = load_json(target, {})
        except OSError:
            document = {}
        entries = document.get("entries") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("version") not in {1, _V17_CACHE_VERSION}
            or document.get("central_base_url") != expected_base
            or not isinstance(entries, dict)
        ):
            loaded: dict[str, dict[str, Any]] = {}
        else:
            loaded = {}
            for path, payload in entries.items():
                if not isinstance(path, str):
                    continue
                key = _canonical_v17_cache_key(path)
                if _valid_v17_payload(key, payload):
                    loaded[key] = payload
        _V17_DISK_CACHE = _trim_v17_entries(loaded)
        _V17_DISK_CACHE_KEY = state_key
        return dict(_V17_DISK_CACHE)


def _save_v17_read_entry(path: str, payload: dict[str, Any]) -> None:
    global _V17_DISK_CACHE
    cache_key = _canonical_v17_cache_key(path)
    if not _valid_v17_payload(cache_key, payload):
        raise ValueError("invalid v1.7 central read response")
    if _v17_json_size(payload) > _V17_CACHE_MAX_ENTRY_BYTES:
        return
    with _V17_CACHE_LOCK:
        entries = _load_v17_read_cache()
        entries.pop(cache_key, None)
        entries[cache_key] = payload
        entries = _fit_v17_file_budget(_trim_v17_entries(entries))
        atomic_write_json(v17_read_cache_path(), _v17_cache_document(entries))
        _V17_DISK_CACHE = entries


def _invalidate_v17_read_cache(*prefixes: str) -> None:
    global _V17_READ_MEMORY, _V17_DISK_CACHE
    _V17_READ_MEMORY = {
        path: value for path, value in _V17_READ_MEMORY.items()
        if not any(path.split("?", 1)[0].startswith(prefix) for prefix in prefixes)
    }
    with _V17_CACHE_LOCK:
        entries = _load_v17_read_cache()
        retained = {
            path: payload for path, payload in entries.items()
            if not any(path.split("?", 1)[0].startswith(prefix) for prefix in prefixes)
        }
        atomic_write_json(v17_read_cache_path(), _v17_cache_document(retained))
        _V17_DISK_CACHE = retained


def _read_v17_resource(path: str) -> dict[str, Any]:
    return _read_v17_resource_with_status(path)[0]


def _read_v17_resource_with_status(path: str) -> tuple[dict[str, Any], bool]:
    cache_key = _canonical_v17_cache_key(path)
    with _v17_read_lock(cache_key):
        # Recheck after acquiring the per-key lock: another request may have
        # completed the same central read while this thread was waiting.
        cached = _V17_READ_MEMORY.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1], cached[2]
        fallback = _load_v17_read_cache().get(cache_key)
        try:
            if fallback is not None and _v17_resource_name(path) == "/v1/timeline-events":
                payload = _central_read_json(path, if_none_match=_v17_payload_etag(fallback))
            else:
                payload = _central_read_json(path)
            if not _valid_v17_payload(path, payload):
                raise CentralReadError("invalid_response", "central returned an invalid resource")
        except CentralReadNotModified:
            if fallback is None:
                raise CentralReadError("invalid_response", "central returned 304 without a cached resource")
            _V17_READ_MEMORY[cache_key] = (time.monotonic(), fallback, False)
            return fallback, False
        except CentralReadConfigurationError:
            if fallback is None:
                raise
            _V17_READ_MEMORY[cache_key] = (time.monotonic(), fallback, True)
            return fallback, True
        except CentralReadError as error:
            # A network/format failure may use the last confirmed read-only copy.
            # An explicit central rejection (401/403/404/etc.) must remain visible
            # and must never be disguised as a successful stale read.
            if error.category not in {"central_unavailable", "invalid_response"}:
                raise
            if fallback is None:
                raise
            _V17_READ_MEMORY[cache_key] = (time.monotonic(), fallback, True)
            return fallback, True
        _save_v17_read_entry(path, payload)
        _V17_READ_MEMORY[cache_key] = (time.monotonic(), payload, False)
        return payload, False


def _central_read_json(
    central_path: str, *, if_none_match: str | None = None,
) -> dict[str, Any]:
    """GET *central_path* using a read token and return parsed JSON."""
    token = get_central_read_token() or get_central_token()
    if not CENTRAL_BASE_URL or not token:
        raise CentralReadConfigurationError("central read proxy is not configured")
    client = CentralReadClient(CENTRAL_BASE_URL, token)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if if_none_match:
        headers["If-None-Match"] = if_none_match
    request = Request(
        f"{CENTRAL_BASE_URL}{central_path}",
        headers=headers,
        method="GET",
    )
    try:
        with client.opener(request, timeout=client.timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read()
    except HTTPError as exc:
        if exc.code == 304 and if_none_match:
            raise CentralReadNotModified() from exc
        raise CentralReadError("central_rejected", f"HTTP {exc.code}", http_status=exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CentralReadError("central_unavailable", str(exc)) from exc
    if status >= 400:
        raise CentralReadError("central_rejected", f"HTTP {status}", http_status=status)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CentralReadError("invalid_response", str(exc)) from exc


_HEALTH_INFO_MEMORY: dict[str, tuple[float, dict[str, Any], bool]] = {}


def health_info_cache_path() -> Path:
    return DATA_DIR / HEALTH_INFO_CACHE_FILENAME


def _valid_health_info_payload(date_str: str, payload: Any) -> bool:
    if not isinstance(payload, dict) or payload.get("date") != date_str or payload.get("timezone") != "Asia/Shanghai":
        return False
    sleep = payload.get("sleep")
    steps = payload.get("steps")
    return (
        isinstance(sleep, dict)
        and sleep.get("status") in {"final", "estimating", "insufficient_data"}
        and isinstance(steps, dict)
        and isinstance(steps.get("devices"), list)
    )


def _load_health_info_cache() -> dict[str, dict[str, Any]]:
    document = load_json(health_info_cache_path(), {})
    expected_base = CENTRAL_BASE_URL.rstrip("/") if CENTRAL_BASE_URL else ""
    if not isinstance(document, dict) or document.get("version") != 1 or document.get("central_base_url") != expected_base:
        return {}
    entries = document.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {date_str: payload for date_str, payload in entries.items()
            if isinstance(date_str, str) and _valid_health_info_payload(date_str, payload)}


def _save_health_info_cache(date_str: str, payload: dict[str, Any]) -> None:
    if not _valid_health_info_payload(date_str, payload):
        raise ValueError("invalid central health-info response")
    with data_lock:
        entries = _load_health_info_cache()
        entries[date_str] = payload
        atomic_write_json(health_info_cache_path(), {
            "version": 1,
            "central_base_url": CENTRAL_BASE_URL.rstrip("/") if CENTRAL_BASE_URL else "",
            "entries": entries,
        })


def read_central_health_info(date_str: str) -> tuple[dict[str, Any], bool]:
    """Return a current snapshot, or the last successful per-date read offline."""
    cached = _HEALTH_INFO_MEMORY.get(date_str)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
        return cached[1], cached[2]
    token = get_central_read_token() or get_central_token()
    if not CENTRAL_BASE_URL or not token:
        error: Exception = CentralReadConfigurationError("central health-info proxy is not configured")
    else:
        try:
            payload = CENTRAL_READ_CLIENT_CLASS(CENTRAL_BASE_URL, token).read_health_info(date_str)
            if not _valid_health_info_payload(date_str, payload):
                raise CentralReadError("invalid_response", "central returned an invalid health-info resource")
            _save_health_info_cache(date_str, payload)
            _HEALTH_INFO_MEMORY[date_str] = (time.monotonic(), payload, False)
            return payload, False
        except (ValueError, CentralReadError) as exc:
            error = exc
    fallback = _load_health_info_cache().get(date_str)
    if isinstance(error, CentralReadError) and error.category not in {"central_unavailable", "invalid_response"}:
        raise error
    if fallback is None:
        raise error
    _HEALTH_INFO_MEMORY[date_str] = (time.monotonic(), fallback, True)
    return fallback, True


def _central_write_json(method: str, central_path: str,
                        body: dict[str, Any] | None) -> tuple[dict[str, Any] | None, int | None]:
    """Send a write request to central using a device token.
    Returns (result, http_status)."""
    token = get_central_token()
    if not CENTRAL_BASE_URL or not token:
        raise CentralReadConfigurationError("central write proxy is not configured")
    client = CentralDeviceClient(CENTRAL_BASE_URL, token)
    return client._request_with_status(method, central_path, body)


def _read_central_blacklist_rules() -> list[dict[str, Any]]:
    """Fetch blacklist rules from central using the best available credential.

    Priority: central read token → device upload token.  If neither is available,
    raise CentralReadConfigurationError so callers fall back to cache.
    """
    if not CENTRAL_BASE_URL:
        raise CentralReadConfigurationError(
            "central read proxy requires LIFE_RADIO_CENTRAL_BASE_URL"
        )
    token = get_central_read_token() or get_central_token()
    if not token:
        raise CentralReadConfigurationError(
            "central blacklist read requires LIFE_RADIO_CENTRAL_READ_TOKEN or "
            "LIFE_RADIO_CENTRAL_TOKEN"
        )
    client = CENTRAL_READ_CLIENT_CLASS(CENTRAL_BASE_URL, token)
    return client.read_blacklist_rules()


def is_blacklisted_app(name: str) -> bool:
    lowered = name.casefold()
    apps, _ = _load_blacklist_for_matching()
    return any(term in lowered for term in apps)


def is_blacklisted_site(name: str) -> bool:
    cleaned = name.casefold()
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    cleaned = cleaned.rstrip(".")
    _, domains = _load_blacklist_for_matching()
    for domain in domains:
        if cleaned == domain or cleaned.endswith("." + domain):
            return True
    return False


def activitywatch_bucket_kind(bucket_id: str) -> str | None:
    if bucket_id.startswith("aw-watcher-window_"):
        return "window"
    if bucket_id.startswith("aw-watcher-web-"):
        return "web"
    if bucket_id.startswith("aw-watcher-afk_"):
        return "afk"
    return None


def activitywatch_raw_interval(event: dict[str, Any]) -> tuple[datetime, float] | None:
    normalised_timestamp = activitywatch_timestamp(event.get("timestamp"))
    start = parse_utc_datetime(normalised_timestamp)
    duration = event.get("duration", 0)
    if start is None or not isinstance(duration, (int, float)) or isinstance(duration, bool):
        return None
    return start, max(0.0, float(duration))


def select_latest_activitywatch_buckets(
    bucket_events: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Pick the currently active bucket for each AW collector kind.

    Copied ActivityWatch installations can leave historical bucket names on a
    machine. Summing all of them would duplicate current-hour usage, so live
    status follows the bucket with the newest observed interval per kind.
    """
    selected: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
    for bucket_id, events in bucket_events.items():
        kind = activitywatch_bucket_kind(bucket_id)
        if kind is None or not isinstance(events, list):
            continue
        newest = max(
            (
                interval[0] + timedelta(seconds=interval[1])
                for event in events if isinstance(event, dict)
                if (interval := activitywatch_raw_interval(event)) is not None
            ),
            default=None,
        )
        if newest is None:
            continue
        current = selected.get(kind)
        if current is None or newest > current[0]:
            selected[kind] = (newest, events)
    return {kind: events for kind, (_, events) in selected.items()}


def live_interval_seconds(
    start: datetime, duration: float, window_start: datetime, window_end: datetime,
) -> int:
    end = start + timedelta(seconds=duration)
    return max(0, int(round((min(end, window_end) - max(start, window_start)).total_seconds())))


def build_live_usage_status(
    bucket_events: dict[str, list[dict[str, Any]]],
    now: datetime | None = None,
    completed_today_app_seconds: int = 0,
    completed_today_blacklist_seconds: int = 0,
) -> dict[str, Any]:
    """Build one internally consistent live snapshot from raw AW events."""
    sampled_at = (now or utc_now()).astimezone(timezone.utc)
    local_now = sampled_at.astimezone(DISPLAY_TIMEZONE)
    hour_start = local_now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    selected = select_latest_activitywatch_buckets(bucket_events)

    parsed: dict[str, list[tuple[datetime, float, dict[str, Any]]]] = {}
    for kind in ("window", "web", "afk"):
        rows: list[tuple[datetime, float, dict[str, Any]]] = []
        for event in selected.get(kind, []):
            if not isinstance(event, dict):
                continue
            interval = activitywatch_raw_interval(event)
            if interval is None:
                continue
            raw = event.get("data") if isinstance(event.get("data"), dict) else {}
            rows.append((interval[0], interval[1], raw))
        parsed[kind] = sorted(rows, key=lambda item: item[0])

    freshness_cutoff = sampled_at - timedelta(seconds=20)
    latest_window = max(
        parsed["window"],
        key=lambda item: item[0] + timedelta(seconds=item[1]),
        default=None,
    )
    latest_afk = max(
        parsed["afk"],
        key=lambda item: item[0] + timedelta(seconds=item[1]),
        default=None,
    )
    is_afk = bool(
        latest_afk
        and latest_afk[0] + timedelta(seconds=latest_afk[1]) >= freshness_cutoff
        and str(latest_afk[2].get("status") or "").casefold() == "afk"
    )
    current_afk_seconds = (
        max(
            0,
            int(
                (
                    min(
                        sampled_at,
                        latest_afk[0] + timedelta(seconds=latest_afk[1]),
                    )
                    - latest_afk[0]
                ).total_seconds()
            ),
        )
        if is_afk and latest_afk
        else 0
    )
    window_is_current = bool(
        latest_window
        and latest_window[0] + timedelta(seconds=latest_window[1]) >= freshness_cutoff
    )
    current_app = (
        str(latest_window[2].get("app") or "未知应用")
        if latest_window and window_is_current and not is_afk else "空闲" if is_afk else "暂无活动"
    )

    current_hour_app_seconds = 0
    current_hour_blacklist_seconds = 0
    chrome_windows: list[tuple[datetime, int]] = []
    non_chrome_windows: list[tuple[datetime, int]] = []
    live_afk_intervals = merge_time_intervals([
        (start, start + timedelta(seconds=duration))
        for start, duration, raw in parsed["afk"]
        if duration > 0 and str(raw.get("status") or "").casefold() == "afk"
    ])
    for start, duration, raw in parsed["window"]:
        app_name = str(raw.get("app") or "未知应用")
        end = start + timedelta(seconds=max(0, duration))
        for piece_start, piece_end in subtract_time_intervals(start, end, live_afk_intervals):
            piece_seconds = int((piece_end - piece_start).total_seconds())
            if piece_seconds <= 0:
                continue
            target = chrome_windows if is_browser_app_name(app_name) else non_chrome_windows
            target.append((piece_start, piece_seconds))
            clipped = live_interval_seconds(piece_start, piece_seconds, hour_start, sampled_at)
            if clipped <= 0:
                continue
            current_hour_app_seconds += clipped
            if is_blacklisted_app(app_name):
                current_hour_blacklist_seconds += clipped

    web_markers: list[tuple[datetime, str]] = []
    for start, _, raw in parsed["web"]:
        domain = parse_url(str(raw.get("url") or "")).netloc.casefold().removeprefix("www.")
        if domain:
            web_markers.append((start, domain))

    for domain, start, duration in derive_chrome_domain_segments(
        chrome_windows, web_markers, non_chrome_windows,
    ):
        if is_blacklisted_site(domain):
            current_hour_blacklist_seconds += live_interval_seconds(start, duration, hour_start, sampled_at)
    current_hour_blacklist_seconds = min(current_hour_app_seconds, current_hour_blacklist_seconds)

    current_site = "无"
    if latest_window and window_is_current and not is_afk and is_browser_app_name(current_app):
        current_site = (
            latest_chrome_domain(
                chrome_windows, non_chrome_windows, web_markers, sampled_at,
            )
            or "无"
        )

    process_warning = current_app not in {"空闲", "暂无活动"} and is_blacklisted_app(current_app)
    site_warning = current_site != "无" and is_blacklisted_site(current_site)
    return {
        "status": "ok",
        "sampled_at": utc_timestamp(sampled_at),
        "hour_start": utc_timestamp(hour_start),
        "timezone": str(DISPLAY_TIMEZONE),
        "activity_state": "afk" if is_afk else "active" if window_is_current else "unknown",
        "current_afk_seconds": current_afk_seconds,
        "current_app": current_app,
        "current_hour_app_seconds": current_hour_app_seconds,
        "today_app_seconds": max(0, completed_today_app_seconds) + current_hour_app_seconds,
        "current_site": current_site,
        "current_hour_blacklist_seconds": current_hour_blacklist_seconds,
        "today_blacklist_seconds": (
            max(0, completed_today_blacklist_seconds) + current_hour_blacklist_seconds
        ),
        "current_is_blacklisted": bool(process_warning or site_warning),
        "blacklist_reason": "process" if process_warning else "site" if site_warning else None,
    }


def get_live_usage_status(now: datetime | None = None) -> dict[str, Any]:
    """Build the small-window snapshot from Life Link's local native outbox."""
    sampled_at = (now or utc_now()).astimezone(timezone.utc)
    local_now = sampled_at.astimezone(DISPLAY_TIMEZONE)
    hour_start = local_now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    date_str = business_date(sampled_at)
    try:
        completed_app_seconds, completed_blacklist_seconds = (
            get_cached_completed_local_usage_totals(date_str, local_now.hour)
        )
    except Exception:
        completed_app_seconds, completed_blacklist_seconds = 0, 0
    try:
        bucket_events: dict[str, list[dict[str, Any]]] = {
            "aw-watcher-window_lifelink": [],
            "aw-watcher-afk_lifelink": [],
            "aw-watcher-web-chrome_lifelink": [],
        }
        for event in get_central_outbox().list_events({
            "app.foreground", "device.input_state", "web.foreground",
        }):
            source = event.get("source") if isinstance(event.get("source"), dict) else {}
            collector = source.get("collector")
            if collector not in {"windows_native", "browser_extension"}:
                continue
            occurred = parse_utc_datetime(event.get("occurred_at"))
            if occurred is None or occurred >= sampled_at:
                continue
            duration = max(0, int(event.get("duration_seconds") or 0))
            if occurred + timedelta(seconds=duration) < hour_start - timedelta(seconds=180):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_type = event.get("event_type")
            raw: dict[str, Any]
            bucket_id: str
            if event_type == "app.foreground":
                app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
                raw = {"app": str(app.get("display_name") or app.get("package_name") or "未知应用")}
                bucket_id = "aw-watcher-window_lifelink"
            elif event_type == "device.input_state":
                raw = {"status": "afk" if payload.get("status") in {"afk", "locked"} else "not-afk"}
                bucket_id = "aw-watcher-afk_lifelink"
            else:
                domain = str(payload.get("domain") or "")
                if not domain:
                    continue
                raw = {"url": f"https://{domain}/"}
                bucket_id = "aw-watcher-web-chrome_lifelink"
            bucket_events[bucket_id].append({
                "id": event.get("event_id"),
                "timestamp": event.get("occurred_at"),
                "duration": duration,
                "data": raw,
            })
        result = build_live_usage_status(
            bucket_events,
            sampled_at,
            completed_today_app_seconds=completed_app_seconds,
            completed_today_blacklist_seconds=completed_blacklist_seconds,
        )
        result["collection"] = collection_runtime_status()
        return result
    except Exception as error:
        return {
            "status": "offline",
            "sampled_at": utc_timestamp(sampled_at),
            "hour_start": utc_timestamp(hour_start),
            "timezone": str(DISPLAY_TIMEZONE),
            "activity_state": "unknown",
            "current_afk_seconds": 0,
            "current_app": "本机采集暂不可用",
            "current_hour_app_seconds": 0,
            "today_app_seconds": completed_app_seconds,
            "current_site": "无",
            "current_hour_blacklist_seconds": 0,
            "today_blacklist_seconds": completed_blacklist_seconds,
            "current_is_blacklisted": False,
            "blacklist_reason": None,
            "error": str(error),
            "collection": collection_runtime_status(),
        }


def ai_context_dir(date_str: str) -> Path:
    return DATA_DIR / "ai_context" / date_str


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def read_central_ai_summary(kind: str, date_str: str) -> str:
    """Fetch one central AI summary markdown without exposing its read token."""
    token = get_central_read_token()
    if not CENTRAL_BASE_URL or not token:
        raise CentralReadConfigurationError(
            "central read proxy requires LIFE_RADIO_CENTRAL_BASE_URL and "
            "LIFE_RADIO_CENTRAL_READ_TOKEN"
        )
    from_utc, to_utc = business_day_utc_bounds(date_str)
    local_device_id = local_desktop_device_descriptor()["device_id"]
    try:
        client = CENTRAL_READ_CLIENT_CLASS(CENTRAL_BASE_URL, token)
    except ValueError as error:
        raise CentralReadConfigurationError(str(error)) from error
    return client.read_ai_summary(
        kind,
        from_utc=from_utc,
        to_utc=to_utc,
        local_device_id=local_device_id,
    )


def materialize_ai_context(date_str: str) -> dict[str, str]:
    contexts = {
        "usage": read_central_ai_summary("usage", date_str),
        "location": read_central_ai_summary("location", date_str),
    }
    filenames = {"usage": "application_usage.md", "location": "location.md"}
    for kind, content in contexts.items():
        atomic_write_text(ai_context_dir(date_str) / filenames[kind], content)
    return contexts


class SyncHandler(BaseHTTPRequestHandler):
    server_version = "LifeRadioSync/4.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Frequent dashboard polling must not grow sync_server.log forever.
        # Unhandled failures still reach stderr and the desktop child log.
        return

    def send_json(self, status_code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A dashboard refresh may abort an older request. The response is
            # already complete server-side; do not turn that normal browser
            # behaviour into a traceback in the service console.
            return

    def send_text(self, status_code: int, content: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = content.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def send_file(self, filepath: Path, content_type: str) -> None:
        try:
            content = filepath.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "File not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _proxy_tianditu_tile(self, tile_type: str, z: str, x: str, y: str) -> None:
        """Proxy a Tianditu map tile so the API key stays server-side."""
        if not TIANDITU_MAP_KEY:
            self.send_error(503, "Tianditu key not configured")
            return
        td_layer = {"vec": "vec_w", "cva": "cva_w"}.get(tile_type)
        if not td_layer:
            self.send_error(404, "Unknown tile type")
            return
        subdomain = int(x) % 8
        url = (
            f"https://t{subdomain}.tianditu.gov.cn/DataServer"
            f"?T={td_layer}&x={x}&y={y}&l={z}&tk={TIANDITU_MAP_KEY}"
        )
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"http://127.0.0.1:{PORT}/",
        })
        try:
            with urlopen(req, timeout=10) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "image/png")
        except HTTPError as exc:
            self.send_error(exc.code, f"Tianditu error: {exc.reason}")
            return
        except Exception as exc:
            self.send_error(502, f"Tile proxy error: {exc}")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def read_json_body(self) -> tuple[Any | None, str | None]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Content-Length must be an integer"
        if content_length < 1:
            return None, "request body is required"
        if content_length > MAX_BODY_BYTES:
            return None, "request body is too large"
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "request body must be UTF-8 JSON"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/v1/health":
            local_device = local_desktop_device_descriptor()
            self.send_json(200, {
                "status": "ok",
                "server_time": utc_timestamp(),
                "api_version": "v1",
                "device": {
                    "device_id": local_device["device_id"],
                    "platform": local_device["platform"],
                    "display_name": local_device["display_name"],
                },
            })
        elif parsed.path == "/health":
            self.send_json(200, {
                "status": "ok",
                "hostname": get_hostname(),
                "data_types": ["context_events"],
                "version": "4.0",
                "server_time": utc_timestamp(),
            })
        elif parsed.path == "/api/central-health":
            self._handle_central_health()
        elif parsed.path == "/api/devices":
            date_str = params.get("date", [display_date_today()])[0]
            try:
                business_day_utc_bounds(date_str)
            except ValueError:
                self.send_json(400, {"error": "date must use YYYY-MM-DD"})
                return
            try:
                payload = get_central_device_status_payload(date_str)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_read_not_configured", "message": str(error)})
            except CentralReadError as error:
                self.send_json(502, {"error": error.category, "message": str(error), "central_status": error.http_status})
            else:
                self.send_json(200, payload)
        elif parsed.path == "/api/device-management":
            self._proxy_device_management_get()
        elif parsed.path == "/api/runtime/login-startup":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "login startup status is local-only"})
                return
            self.send_json(200, pc_windows_startup.status())
        elif parsed.path == "/api/usage":
            date_str = params.get("date", [display_date_today()])[0]
            try:
                business_day_utc_bounds(date_str)
            except ValueError:
                self.send_json(400, {"error": "date must use YYYY-MM-DD"})
                return
            try:
                payload = get_central_usage_payload(date_str)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_read_not_configured", "message": str(error)})
            except CentralReadError as error:
                self.send_json(502, {"error": error.category, "message": str(error), "central_status": error.http_status})
            else:
                self.send_json(200, payload)
        elif parsed.path == "/api/live-usage":
            # This is a lightweight read-only AW snapshot. It intentionally
            # performs no durable import and therefore needs no storage lock.
            self.send_json(200, get_live_usage_status())
        elif parsed.path == "/api/locations":
            date_str = params.get("date", [display_date_today()])[0]
            try:
                business_day_utc_bounds(date_str)
            except ValueError:
                self.send_json(400, {"error": "date must use YYYY-MM-DD"})
                return
            try:
                payload = read_central_locations(date_str)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_read_not_configured", "message": str(error)})
            except CentralReadError as error:
                self.send_json(502, {"error": error.category, "message": str(error), "central_status": error.http_status})
            else:
                self.send_json(200, payload)
        elif (tile_match := _match_path(parsed.path, r"^/map-tiles/(vec|cva)/(\d+)/(\d+)/(\d+)\.png$")):
            self._proxy_tianditu_tile(tile_match.group(1), tile_match.group(2), tile_match.group(3), tile_match.group(4))
        elif parsed.path == "/api/health-info":
            date_str = params.get("date", [display_date_today()])[0]
            try:
                business_day_utc_bounds(date_str)
            except ValueError:
                self.send_json(400, {"error": "date must use YYYY-MM-DD"})
                return
            try:
                payload, stale = read_central_health_info(date_str)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_read_not_configured", "message": str(error)})
            except CentralReadError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if stale:
                    self.send_header("X-Life-Radio-Cache", "stale")
                self.end_headers()
                self.wfile.write(body)
        elif parsed.path == "/api/calendar-days":
            from_values = params.get("from", [])
            to_values = params.get("to", [])
            if len(from_values) != 1 or len(to_values) != 1:
                self.send_json(400, {"error": "from and to must each use YYYY-MM-DD"})
                return
            from_date, to_date = from_values[0], to_values[0]
            try:
                start = datetime.strptime(from_date, "%Y-%m-%d").date()
                end = datetime.strptime(to_date, "%Y-%m-%d").date()
                if end < start:
                    raise ValueError("calendar-days to must not precede from")
                if (end - start).days + 1 > 42:
                    raise ValueError("calendar-days range must not exceed 42 days")
                payload = read_central_calendar_days(from_date, to_date)
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_read_not_configured", "message": str(error)})
            except CentralReadError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(200, payload)
        elif parsed.path == "/api/custom-events":
            date_str = params.get("date", [display_date_today()])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                self.send_json(400, {"error": "date must use YYYY-MM-DD"})
                return
            with data_lock:
                self.send_json(200, get_custom_event_summary(date_str))
        elif parsed.path == "/api/ai-context/index.json":
            date_str = params.get("date", [display_date_today()])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                self.send_json(400, {"error": "date must use YYYY-MM-DD"})
                return
            try:
                with data_lock:
                    materialize_ai_context(date_str)
            except CentralReadConfigurationError as error:
                self.send_json(503, {
                    "error": "central_read_not_configured",
                    "message": str(error),
                })
                return
            except CentralReadError as error:
                self.send_json(502, {
                    "error": error.category,
                    "message": str(error),
                    "central_status": error.http_status,
                })
                return
            self.send_json(200, {
                "date": date_str,
                "contexts": {
                    "usage": f"/api/ai-context/usage.md?date={quote(date_str)}",
                    "location": f"/api/ai-context/location.md?date={quote(date_str)}",
                    "guide": "/api/ai-context/README.md",
                },
            })
        elif parsed.path in {"/api/ai-context/usage.md", "/api/ai-context/location.md"}:
            date_str = params.get("date", [display_date_today()])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                self.send_json(400, {"error": "date must use YYYY-MM-DD"})
                return
            kind = "usage" if parsed.path.endswith("usage.md") else "location"
            try:
                with data_lock:
                    contexts = materialize_ai_context(date_str)
            except CentralReadConfigurationError as error:
                self.send_json(503, {
                    "error": "central_read_not_configured",
                    "message": str(error),
                })
                return
            except CentralReadError as error:
                self.send_json(502, {
                    "error": error.category,
                    "message": str(error),
                    "central_status": error.http_status,
                })
                return
            self.send_text(200, contexts[kind], "text/markdown; charset=utf-8")
        elif parsed.path == "/api/ai-context/README.md":
            if not AI_CONTEXT_GUIDE.exists():
                self.send_json(404, {"error": "AI context guide is not installed"})
                return
            self.send_file(AI_CONTEXT_GUIDE, "text/markdown; charset=utf-8")
        elif parsed.path == "/api/sync/central":
            self.send_json(200, get_central_sync_payload())
        elif parsed.path == "/api/settings":
            # Settings are central-authoritative. Only a transport/format failure may
            # use the last confirmed read; 4xx responses must stay visible.
            try:
                token = get_central_token()
                if not CENTRAL_BASE_URL or not token:
                    raise CentralReadConfigurationError("central shared settings require a registered device credential")
                settings = CENTRAL_CLIENT_CLASS(CENTRAL_BASE_URL, token).get_shared_settings()
                save_shared_settings_cache(settings)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
                return
            except CentralReadError as error:
                cached = load_shared_settings_cache() if error.category in {"central_unavailable", "invalid_response"} else None
                if cached is None:
                    self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
                    return
                body = json.dumps(cached, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Life-Radio-Cache", "stale")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_json(200, settings)
        elif parsed.path == "/api/blacklist/rules":
            source = "unknown"
            rules = None
            try:
                rules = _read_central_blacklist_rules()
                save_cached_blacklist(rules)
                source = "central"
            except (CentralReadConfigurationError, CentralReadError):
                pass
            if rules is None:
                cached = load_cached_blacklist()
                if cached is not None:
                    rules = cached.get("rules")
                    source = "cache"
            if not isinstance(rules, list):
                rules = []
                source = source if source == "cache" else "unavailable"
            _BLACKLIST_MEMORY_RULES = rules
            _BLACKLIST_MEMORY_APPS = [
                str(r.get("normalized_pattern", r.get("pattern", ""))) for r in rules
                if isinstance(r, dict) and r.get("rule_type") == "app" and r.get("enabled") is not False
                and (r.get("platform_scope") or "pc") == "pc"
            ]
            _BLACKLIST_MEMORY_DOMAINS = [
                str(r.get("normalized_pattern", r.get("pattern", ""))) for r in rules
                if isinstance(r, dict) and r.get("rule_type") == "domain" and r.get("enabled") is not False
                and (r.get("platform_scope") or "web") == "web"
            ]
            _BLACKLIST_MEMORY_TIMESTAMP = time.monotonic()
            self.send_json(200, {"rules": rules, "source": source})
        elif parsed.path == "/api/media/items":
            try:
                _, payload = central_media_request("GET", "/v1/media/items", use_read=True)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_read_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(200, payload)
        elif parsed.path == "/api/media/jobs":
            try:
                _, payload = central_media_request("GET", "/v1/media/jobs", use_read=False)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(200, payload)
        # ---- Wishes (read) ----
        elif parsed.path == "/api/wishes":
            qs = parsed.query or ""
            self._proxy_central_get(f"/v1/wishes?{qs}" if qs else "/v1/wishes")
        elif (path_match := _match_path(parsed.path, r"^/api/wishes/([^/]+)$")):
            wish_id = path_match.group(1)
            self._proxy_central_get(f"/v1/wishes/{wish_id}")
        # ---- Timeline (read) ----
        elif parsed.path == "/api/timeline-events":
            qs = parsed.query or ""
            self._proxy_central_get(f"/v1/timeline-events?{qs}" if qs else "/v1/timeline-events")
        elif parsed.path == "/api/ai-readers":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI reader status is local-only"})
                return
            try:
                _, payload = central_media_request("GET", "/v1/ai-readers", use_read=False)
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(200, payload)
        elif (path_match := _match_path(parsed.path, r"^/api/ai-readers/([^/]+)/process-status$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI reader process status is local-only"})
                return
            reader_id = path_match.group(1)
            try:
                _, payload = central_media_request(
                    "GET", f"/v1/ai-readers/{reader_id}/process-status", use_read=False,
                )
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(200, payload)
        elif (path_match := _match_path(parsed.path, r"^/api/ai-readers/([^/]+)/access-logs$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI reader logs are local-only"})
                return
            reader_id = path_match.group(1)
            qs = parsed.query or "limit=20"
            try:
                _, payload = central_media_request(
                    "GET", f"/v1/ai-readers/{reader_id}/access-logs?{qs}", use_read=False,
                )
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(200, payload)
        elif (path_match := _match_path(parsed.path, r"^/api/ai-readers/([^/]+)/context-preview$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI reader preview is local-only"})
                return
            reader_id = path_match.group(1)
            try:
                _, payload = central_media_request(
                    "GET",
                    f"/v1/ai-readers/{reader_id}/context-preview?view=compact",
                    use_read=False,
                )
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(200, payload)
        elif parsed.path == "/api/event-background":
            qs = parsed.query or ""
            self._proxy_central_get(f"/v1/event-background?{qs}" if qs else "/v1/event-background")
        # ---- Trigger types (read) ----
        elif parsed.path == "/api/trigger-types":
            self._proxy_central_get("/v1/trigger-types")
        # ---- Event triggers (read) ----
        elif parsed.path == "/api/event-triggers":
            self._proxy_central_get("/v1/event-triggers")
        elif parsed.path in WEB_ASSETS:
            asset_path, content_type = WEB_ASSETS[parsed.path]
            self.send_file(asset_path, content_type)
        elif parsed.path in {"/", "/dashboard.html"}:
            self.send_file(DASHBOARD_FILE, "text/html; charset=utf-8")
        else:
            self.send_error(404, "Not found")

    def _handle_central_health(self) -> None:
        """Probe the central service directly; returns connected=true/false."""
        if not CENTRAL_BASE_URL or not get_central_token():
            self.send_json(200, {"connected": False})
            return
        try:
            redirect = CENTRAL_BASE_URL + "/v1/health"
            request = urllib.request.Request(redirect, method="GET")
            request.add_header("Authorization", f"Bearer {get_central_token()}")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode())
                self.send_json(200, {"connected": body.get("status") == "ok"})
        except Exception:
            self.send_json(200, {"connected": False})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/sync/central":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "central sync can only be triggered locally"})
                return
            if central_sync_lock.locked():
                self.send_json(202, {"status": "already_running"})
                return
            threading.Thread(
                target=lambda: sync_central_once(force_retry=True),
                daemon=True,
                name="life-radio-central-sync",
            ).start()
            self.send_json(202, {"status": "started", "mode": "central"})
        elif path == "/api/runtime/login-startup":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "login startup can only be changed locally"})
                return
            payload, error = self.read_json_body()
            enabled = payload.get("enabled") if isinstance(payload, dict) else None
            if error or not isinstance(payload, dict) or set(payload) != {"enabled"} or not isinstance(enabled, bool):
                self.send_json(400, {"error": error or "enabled must be a boolean"})
                return
            try:
                state = pc_windows_startup.set_enabled(enabled)
            except (OSError, RuntimeError) as exc:
                self.send_json(500, {"error": "login_startup_update_failed", "message": str(exc)})
                return
            self.send_json(200, state)
        elif path == "/api/ai-readers/pairings":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI pairing is local-only"})
                return
            try:
                status, payload = central_media_request(
                    "POST", "/v1/ai-readers/pairings", body={}, use_read=False,
                )
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(status, payload)
        elif path == "/api/ai-reader-skill/open":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI reader Skill export is local-only"})
                return
            try:
                exported_path = export_ai_reader_skill(open_location=True)
            except (OSError, shutil.Error) as error:
                self.send_json(
                    500,
                    {"error": "ai_reader_skill_open_failed", "message": str(error)},
                )
            else:
                self.send_json(200, {"path": str(exported_path)})
        elif path == "/api/ai-reader-connection-package/open":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI connection package export is local-only"})
                return
            try:
                if not AI_READER_MCP_EXECUTABLE.is_file():
                    self.send_json(503, {
                        "error": "life_link_mcp_not_built",
                        "message": "Life Link MCP executable has not been built",
                    })
                    return
                status, pairing_payload = central_media_request(
                    "POST", "/v1/ai-readers/pairings", body={}, use_read=False,
                )
                if status != 201:
                    self.send_json(status, pairing_payload)
                    return
                if not isinstance(pairing_payload, dict):
                    raise ValueError("central pairing response must be an object")
                exported_path = create_ai_reader_connection_bundle(
                    pairing_payload, open_location=True,
                )
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            except (OSError, shutil.Error, ValueError, zipfile.BadZipFile) as error:
                self.send_json(
                    500,
                    {"error": "ai_connection_package_open_failed", "message": str(error)},
                )
            else:
                self.send_json(201, {
                    "filename": exported_path.name,
                    "expires_at": pairing_payload.get("expires_at"),
                })
        elif (path_match := _match_path(path, r"^/api/ai-readers/([^/]+)/clear-reading-progress$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "AI reader progress is local-only"})
                return
            reader_id = path_match.group(1)
            try:
                status, payload = central_media_request(
                    "POST", f"/v1/ai-readers/{reader_id}/clear-reading-progress",
                    body={}, use_read=False,
                )
            except CentralReadConfigurationError as error:
                self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            except CentralMediaProxyError as error:
                self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            else:
                self.send_json(status, payload)
        elif path == "/api/custom-events":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "custom events can only be created locally"})
                return
            payload, error = self.read_json_body()
            if error or not isinstance(payload, dict):
                self.send_json(400, {"error": error or "custom event must be an object"})
                return
            event, event_error = create_local_custom_event(payload)
            if event_error:
                self.send_json(400, {"error": event_error})
                return
            self.send_json(201, {"status": "stored", "event": event})
        elif path == "/api/media/jobs":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "media jobs can only be triggered locally"})
                return
            payload, error = self.read_json_body()
            if error or not isinstance(payload, dict):
                self.send_json(400, {"error": error or "request body must be a JSON object"})
                return
            try:
                status, result = central_media_request("POST", "/v1/media/jobs", body={"url": payload.get("url")})
            except CentralReadConfigurationError as exc:
                self.send_json(503, {"error": "central_not_configured", "message": str(exc)})
            except CentralMediaProxyError as exc:
                self.send_json(exc.http_status or 502, {"error": exc.category, "message": str(exc)})
            else:
                self.send_json(status, result)
        elif path == "/api/media/open-folder":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "open folder can only be triggered locally"})
                return
            try:
                status, result = central_media_request("POST", "/v1/media/open-folder")
            except CentralReadConfigurationError as exc:
                self.send_json(503, {"error": "central_not_configured", "message": str(exc)})
            except CentralMediaProxyError as exc:
                self.send_json(exc.http_status or 502, {"error": exc.category, "message": str(exc)})
            else:
                self.send_json(status, result)
        elif path == "/api/blacklist/rules":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "blacklist rules can only be changed locally"})
                return
            payload, error = self.read_json_body()
            if error or not isinstance(payload, dict):
                self.send_json(400, {"error": error or "request body must be a JSON object"})
                return
            try:
                status, result = central_media_request(
                    "POST", "/v1/settings/blacklist-rules", body=payload, use_read=False,
                )
            except CentralReadConfigurationError as exc:
                self.send_json(503, {"error": "central_not_configured", "message": str(exc)})
            except CentralMediaProxyError as exc:
                self.send_json(exc.http_status or 502, {"error": exc.category, "message": str(exc)})
            else:
                invalidate_blacklist_memory()
                self.send_json(status, result)
        elif path == "/api/settings":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "settings can only be changed locally"})
                return
            payload, error = self.read_json_body()
            if error or not isinstance(payload, dict):
                self.send_json(400, {"error": error or "settings must be an object"})
                return
            allowed = {"day_start_hour", "primary_health_device_id", "sleep_local_time", "morning_report", "evening_report", "periodic_summary"}
            if not payload or not set(payload) <= allowed:
                self.send_json(400, {"error": "unsupported shared setting"})
                return
            hour = payload.get("day_start_hour")
            if "day_start_hour" in payload and (isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23):
                self.send_json(400, {"error": "day_start_hour must be an integer from 0 to 23"})
                return
            primary = payload.get("primary_health_device_id")
            if "primary_health_device_id" in payload and primary is not None and (not isinstance(primary, str) or not primary):
                self.send_json(400, {"error": "primary_health_device_id must be a non-empty string or null"})
                return
            if "sleep_local_time" in payload and (not isinstance(payload["sleep_local_time"], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", payload["sleep_local_time"])):
                self.send_json(400, {"error": "sleep_local_time must use HH:mm"})
                return
            for key in ("morning_report", "evening_report", "periodic_summary"):
                if key in payload and not _valid_report_schedule(key, payload[key]):
                    self.send_json(400, {"error": f"{key} is invalid"})
                    return
            token = get_central_token()
            if not CENTRAL_BASE_URL or not token:
                self.send_json(503, {"error": "central_not_configured", "message": "central shared settings require a registered device credential"})
                return
            try:
                update_value: Any = hour if set(payload) == {"day_start_hour"} else payload
                settings = CENTRAL_CLIENT_CLASS(CENTRAL_BASE_URL, token).update_shared_settings(update_value)
                save_shared_settings_cache(settings)
            except ValueError as exc:
                self.send_json(503, {"error": "central_not_configured", "message": str(exc)})
                return
            except CentralReadError as exc:
                self.send_json(exc.http_status or 502, {"error": exc.category, "message": str(exc)})
                return
            self.send_json(200, settings)
        # ---- Wishes (write) ----
        elif path == "/api/wishes":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "wishes can only be created locally"})
                return
            self._proxy_central_post("/v1/wishes")
        elif (path_match := _match_path(path, r"^/api/wishes/([^/]+)/cancel$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "wishes can only be cancelled locally"})
                return
            wish_id = path_match.group(1)
            self._proxy_central_empty_post(f"/v1/wishes/{wish_id}/cancel")
        elif (path_match := _match_path(path, r"^/api/wishes/([^/]+)/complete$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "wishes can only be completed locally"})
                return
            wish_id = path_match.group(1)
            self._proxy_central_empty_post(f"/v1/wishes/{wish_id}/complete")
        # ---- Event triggers (write) ----
        elif path == "/api/event-triggers":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "triggers can only be created locally"})
                return
            self._proxy_central_post("/v1/event-triggers")
        else:
            self.send_error(404, "Not found")

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if (path_match := _match_path(path, r"^/api/wishes/([^/]+)/days/([^/]+)$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "wish days can only be assessed locally"})
                return
            wish_id, biz_date = path_match.group(1), path_match.group(2)
            self._proxy_central_request("PUT", f"/v1/wishes/{wish_id}/days/{biz_date}")
            return
        self.send_error(405, "Method Not Allowed")

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if (path_match := _match_path(path, r"^/api/device-management/([^/]+)$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "devices can only be changed locally"})
                return
            device_id = path_match.group(1)
            self._proxy_central_request(
                "PATCH", f"/v1/devices/{device_id}", upstream_method="POST",
            )
            return
        if path.startswith("/api/blacklist/rules/"):
            self._handle_blacklist_rule_update(path)
            return
        # ---- Wishes (patch) ----
        if (path_match := _match_path(path, r"^/api/wishes/([^/]+)$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "wishes can only be changed locally"})
                return
            wish_id = path_match.group(1)
            self._proxy_central_request(
                "PATCH", f"/v1/wishes/{wish_id}", upstream_method="POST",
            )
            return
        # ---- Event triggers (patch) ----
        if (path_match := _match_path(path, r"^/api/event-triggers/([^/]+)$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "triggers can only be changed locally"})
                return
            trigger_id = path_match.group(1)
            self._proxy_central_request(
                "PATCH", f"/v1/event-triggers/{trigger_id}", upstream_method="POST",
            )
            return
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if (path_match := _match_path(path, r"^/api/device-management/([^/]+)$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "devices can only be deleted locally"})
                return
            device_id = path_match.group(1)
            self._proxy_central_request(
                "DELETE", f"/v1/devices/{device_id}/delete", upstream_method="POST",
            )
            return
        if path.startswith("/api/blacklist/rules/"):
            self._handle_blacklist_rule_delete(path)
            return
        # ---- Wishes (delete) ----
        if (path_match := _match_path(path, r"^/api/wishes/([^/]+)$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "wishes can only be deleted locally"})
                return
            wish_id = path_match.group(1)
            self._proxy_central_request(
                "DELETE", f"/v1/wishes/{wish_id}/delete", upstream_method="POST",
            )
            return
        # ---- Event triggers (delete) ----
        if (path_match := _match_path(path, r"^/api/event-triggers/([^/]+)$")):
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self.send_json(403, {"error": "triggers can only be deleted locally"})
                return
            trigger_id = path_match.group(1)
            self._proxy_central_request(
                "DELETE", f"/v1/event-triggers/{trigger_id}/delete", upstream_method="POST",
            )
            return
        self.send_error(405, "Method Not Allowed")

    # -----------------------------------------------------------------
    # Proxy helpers
    # -----------------------------------------------------------------
    def _proxy_device_management_get(self) -> None:
        central_path = "/v1/devices"
        stale = False
        try:
            payload, stale = _read_v17_resource_with_status(central_path)
        except CentralReadConfigurationError as error:
            self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            return
        except CentralReadError as error:
            self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            return
        local_device_id = local_desktop_device_descriptor()["device_id"]
        response_payload = {"devices": [
            {**device, "is_current": device.get("device_id") == local_device_id}
            for device in payload.get("devices", [])
            if isinstance(device, dict)
        ]}
        body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if stale:
            self.send_header("X-Life-Radio-Cache", "stale")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_central_get(self, central_path: str) -> None:
        stale = False
        try:
            payload, stale = _read_v17_resource_with_status(central_path)
        except CentralReadConfigurationError as error:
            self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            return
        except CentralReadError as error:
            self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if stale:
            self.send_header("X-Life-Radio-Cache", "stale")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _proxy_central_post(self, central_path: str) -> None:
        self._proxy_central_request("POST", central_path)

    def _proxy_central_empty_post(self, central_path: str) -> None:
        """POST a bodyless central action such as cancel or complete."""
        try:
            result, status = _central_write_json("POST", central_path, None)
        except CentralReadConfigurationError as error:
            self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            return
        except CentralReadError as error:
            self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            return
        final_status = status if status else 200
        if final_status < 400:
            _invalidate_v17_read_cache("/v1/wishes", "/v1/event-triggers")
        self.send_json(final_status, result or {})

    def _proxy_central_request(
        self, method: str, central_path: str, *, upstream_method: str | None = None,
    ) -> None:
        body, error = self.read_json_body()
        if error and method not in ("GET", "DELETE"):
            self.send_json(400, {"error": error})
            return
        transport_method = upstream_method or method
        try:
            result, status = _central_write_json(transport_method, central_path, body)
        except CentralReadConfigurationError as error:
            self.send_json(503, {"error": "central_not_configured", "message": str(error)})
            return
        except CentralReadError as error:
            self.send_json(error.http_status or 502, {"error": error.category, "message": str(error)})
            return
        final_status = status if status else (200 if transport_method != "POST" else 201)
        if final_status < 400:
            if central_path.startswith("/v1/wishes"):
                _invalidate_v17_read_cache("/v1/wishes", "/v1/event-triggers")
            elif central_path.startswith("/v1/event-triggers"):
                _invalidate_v17_read_cache("/v1/event-triggers")
            elif central_path.startswith("/v1/devices"):
                _invalidate_v17_read_cache("/v1/devices", "/v1/timeline-events")
        if result is None:
            self.send_response(final_status if status else 204)
            self.end_headers()
        else:
            self.send_json(final_status, result)

    def _handle_blacklist_rule_update(self, path: str) -> None:
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_json(403, {"error": "blacklist rules can only be changed locally"})
            return
        path_match = _match_path(path, r"^/api/blacklist/rules/([^/]+)$")
        if path_match is None:
            self.send_json(404, {"error": "not_found", "message": "endpoint not found"})
            return
        payload, error = self.read_json_body()
        if error or not isinstance(payload, dict):
            self.send_json(400, {"error": error or "request body must be a JSON object"})
            return
        rule_id = path_match.group(1)
        try:
            status, result = central_media_request(
                "POST", f"/v1/settings/blacklist-rules/{rule_id}", body=payload, use_read=False,
            )
        except CentralReadConfigurationError as exc:
            self.send_json(503, {"error": "central_not_configured", "message": str(exc)})
            return
        except CentralMediaProxyError as exc:
            self.send_json(exc.http_status or 502, {"error": exc.category, "message": str(exc)})
            return
        invalidate_blacklist_memory()
        self.send_json(status, result)

    def _handle_blacklist_rule_delete(self, path: str) -> None:
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_json(403, {"error": "blacklist rules can only be changed locally"})
            return
        path_match = _match_path(path, r"^/api/blacklist/rules/([^/]+)$")
        if path_match is None:
            self.send_json(404, {"error": "not_found", "message": "endpoint not found"})
            return
        rule_id = path_match.group(1)
        try:
            status, result = central_media_request(
                "DELETE", f"/v1/settings/blacklist-rules/{rule_id}", use_read=False,
            )
        except CentralReadConfigurationError as exc:
            self.send_json(503, {"error": "central_not_configured", "message": str(exc)})
            return
        except CentralMediaProxyError as exc:
            self.send_json(exc.http_status or 502, {"error": exc.category, "message": str(exc)})
            return
        invalidate_blacklist_memory()
        self.send_json(status, result)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Idempotency-Key")
        self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main() -> None:
    migrate_legacy_installation_client_state()
    migrate_legacy_appdata_client_state()
    migrate_presplit_client_state()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_orphan_v17_cache_temporary_files()

    print("=" * 50)
    print("  Life Link PC Sync Server v4.0")
    print("=" * 50)
    print(f"  Hostname : {get_hostname()}")
    print(f"  Listen   : {HOST}:{PORT}")
    print(f"  Data dir : {DATA_DIR}")
    print(f"  Dashboard: http://localhost:{PORT}/")
    print(f"  Central  : {CENTRAL_BASE_URL or 'not configured'} ({CENTRAL_SYNC_INTERVAL_SECONDS}s interval)")
    print("=" * 50)
    server = ThreadedHTTPServer((HOST, PORT), SyncHandler)
    browser_receiver = start_browser_receiver()
    if browser_receiver_status.get("status") == "port_in_use":
        print("  Browser : port 5600 is occupied; website collection is disabled")
    stop_central_sync = threading.Event()
    collection_thread = threading.Thread(
        target=native_collection_loop,
        args=(stop_central_sync,),
        daemon=True,
        name="life-link-windows-native-collector",
    )
    collection_thread.start()
    sync_thread = threading.Thread(
        target=central_sync_loop,
        args=(stop_central_sync,),
        daemon=True,
        name="life-radio-central-sync",
    )
    sync_thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        stop_central_sync.set()
        sync_thread.join(timeout=1)
        collection_thread.join(timeout=3)
        if browser_receiver is not None:
            browser_receiver.stop()
        close_central_outbox()
        server.server_close()


if __name__ == "__main__":
    main()
