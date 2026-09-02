#!/usr/bin/env python3
"""Start the PC desktop application as a remote central client only."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.request import ProxyHandler, build_opener

from central_client_setup import (
    default_client_config_path,
    ensure_client_config,
    load_client_config,
    load_client_runtime_config,
)
from central_client_setup_server import create_setup_server
from device_identity import (
    default_identity_path,
    migrate_legacy_appdata_client_state,
    migrate_legacy_installation_client_state,
    migrate_presplit_client_state,
)
from runtime_paths import is_frozen
import pc_windows_startup


BASE_DIR = Path(__file__).resolve().parent
DESKTOP_SCRIPT = BASE_DIR / "desktop_app.py"
WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_DASHBOARD_PORT = 8090


def dashboard_port(config_path: Path | None = None) -> int:
    path = ensure_client_config(config_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"客户端配置无法读取：{path}") from error
    try:
        return int(os.environ.get("LIFE_RADIO_PORT") or payload.get("port") or 8090)
    except (TypeError, ValueError):
        return DEFAULT_DASHBOARD_PORT


def dashboard_mode(port: int, timeout: float = 0.8) -> str | None:
    """Return whether a current or incompatible dashboard owns the port."""
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(
            f"http://127.0.0.1:{port}/api/sync/central", timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            "current"
            if isinstance(payload, dict) and payload.get("mode") == "central"
            else "incompatible"
        )
    except Exception:
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout):
                return "incompatible"
        except Exception:
            return None


def ensure_dashboard_mode_is_compatible(
    port: int,
    *,
    mode_reader: Any = dashboard_mode,
) -> str | None:
    """Refuse to attach to an incompatible process already using the port."""
    mode = mode_reader(port)
    if mode == "incompatible":
        raise RuntimeError(
            "本机 Dashboard 端口已由不兼容的服务占用。请先退出该进程，"
            "再重新启动 Life Link 客户端。"
        )
    return mode


def wait_for_loopback_port_release(port: int, *, timeout: float = 5.0) -> None:
    """Ensure the temporary pairing server has released the dashboard port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.1)
    raise RuntimeError("配对页面尚未释放 8090 端口，请等待几秒后重新启动客户端。")


def client_environment(
    profile: Mapping[str, Any],
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment.update({
        "LIFE_RADIO_HOST": "127.0.0.1",
        "LIFE_RADIO_PORT": str(profile.get("port") or DEFAULT_DASHBOARD_PORT),
        "LIFE_RADIO_APP_USAGE_COLLECTION_ENABLED": (
            "true" if profile.get("app_usage_collection_enabled", True) else "false"
        ),
        "LIFE_RADIO_TIANDITU_KEY": str(profile.get("tianditu_key") or ""),
        "LIFE_RADIO_CENTRAL_BASE_URL": str(profile["central_base_url"]),
        "LIFE_RADIO_CENTRAL_TOKEN": str(profile["upload_token"]),
    })
    config_path = profile.get("_config_path")
    if config_path:
        environment["LIFE_RADIO_CLIENT_CONFIG"] = str(config_path)
    read_token = profile.get("read_token")
    if isinstance(read_token, str) and read_token:
        environment["LIFE_RADIO_CENTRAL_READ_TOKEN"] = read_token
    else:
        environment.pop("LIFE_RADIO_CENTRAL_READ_TOKEN", None)
    return environment


def start_desktop_client(
    profile: Mapping[str, Any],
    *,
    background_start: bool = False,
    popen: Any = subprocess.Popen,
) -> int:
    """Start only desktop_app.py; it owns the local dashboard sync process."""
    environment = client_environment(profile)
    if background_start:
        environment["LIFE_RADIO_NO_BROWSER"] = "true"
        environment["LIFE_RADIO_BACKGROUND_START"] = "true"
    command = (
        [sys.executable, "--lifelink-desktop-worker"]
        if is_frozen() else [sys.executable, str(DESKTOP_SCRIPT)]
    )
    process = popen(
        command,
        cwd=str(BASE_DIR),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=WINDOWS_NO_WINDOW,
    )
    return int(process.wait())


def run_setup_only(
    *,
    config_path: Path,
    identity_path: Path,
    port: int = DEFAULT_DASHBOARD_PORT,
    allow_loopback_http: bool = False,
    browser_open: Any = webbrowser.open,
) -> dict[str, Any]:
    """Run a loopback-only setup page until one invitation is claimed."""
    existing_mode = dashboard_mode(port)
    if existing_mode is not None:
        raise RuntimeError(
            "本机 Dashboard 端口已被正在运行的 Life Link 实例占用。"
            "请先从系统托盘退出旧实例，再重新启动中央客户端设置。"
        )
    server = create_setup_server(
        config_path=config_path,
        identity_path=identity_path,
        port=port,
        allow_loopback_http=allow_loopback_http,
    )
    try:
        browser_open(f"http://127.0.0.1:{server.server_port}/")
        server.serve_forever()
    finally:
        server.server_close()
    if server.state.completed_profile is None:
        raise RuntimeError("中央客户端设置未完成")
    return server.state.completed_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start Life Link as a central PC client")
    parser.add_argument("--config", type=Path, default=default_client_config_path())
    parser.add_argument("--identity", type=Path, default=default_identity_path())
    parser.add_argument("--dry-run", action="store_true", help="validate without starting")
    parser.add_argument(
        "--background-start",
        action="store_true",
        help="start after Windows login without opening the browser or status window",
    )
    parser.add_argument(
        "--allow-loopback-http",
        action="store_true",
        help="deprecated compatibility option; loopback HTTP is allowed automatically",
    )
    return parser


def show_error(message: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "Life Link 客户端启动失败", 0x10)
    else:
        print(f"Error: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    internal_args = list(sys.argv[1:] if argv is None else argv)
    if internal_args == ["--lifelink-desktop-worker"]:
        from desktop_app import main as desktop_main
        return desktop_main()
    if internal_args == ["--lifelink-sync-worker"]:
        from sync_server import main as sync_main
        sync_main()
        return 0
    migrate_legacy_installation_client_state()
    migrate_legacy_appdata_client_state()
    migrate_presplit_client_state()
    args = build_parser().parse_args(argv)
    try:
        try:
            profile = load_client_config(
                args.config,
                identity_path=args.identity,
                allow_loopback_http=args.allow_loopback_http,
            )
        except (OSError, ValueError):
            profile = None
        if args.dry_run:
            if profile is None:
                print("Central client setup is required; no service was started.")
                return 0
            print("Central client configuration is valid.")
            print(f"Device: {profile['device']['device_id']}")
            print(f"Central: {profile['central_base_url']}")
            print(f"Read access: {'configured' if profile.get('read_token') else 'not configured'}")
            print("Credentials were not printed.")
            return 0
        # A configured source checkout can now use the project-local formal
        # launcher, while packaged builds retain their own EXE entry.
        pc_windows_startup.ensure_default_enabled()
        if profile is None:
            profile = run_setup_only(
                config_path=args.config.expanduser().resolve(),
                identity_path=args.identity.expanduser().resolve(),
                port=dashboard_port(args.config),
                allow_loopback_http=args.allow_loopback_http,
            )
            profile.update(load_client_runtime_config(args.config))
            wait_for_loopback_port_release(dashboard_port(args.config))
        profile["_config_path"] = str(args.config.expanduser().resolve())
        ensure_dashboard_mode_is_compatible(dashboard_port(args.config))
        if args.background_start:
            return start_desktop_client(profile, background_start=True)
        return start_desktop_client(profile)
    except (OSError, RuntimeError, ValueError) as error:
        show_error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
