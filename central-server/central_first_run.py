#!/usr/bin/env python3
"""Idempotent first-run guide for a Life Link source checkout."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from central.config import CentralConfig, default_config_path, default_data_dir
from central.operations import _secure_atomic_write_json
from central.storage import CentralStore
from central_endpoint import (
    EndpointError,
    default_endpoint_path,
    load_endpoint,
    probe_endpoint,
    read_token,
    save_endpoint,
)
from central_invitation import copy_to_clipboard, create_client_invitation
from central_server_app import ensure_server_configuration, health_is_ready
from configure_tailscale_endpoint import TailscaleSetupError, configure as configure_tailscale


SETUP_VERSION = 1
DEFAULT_PORT = 8091
PORT_SEARCH_LIMIT = 100


class SetupError(RuntimeError):
    pass


def _read_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise SetupError(f"无法读取配置文件：{path}") from error
    if not isinstance(payload, dict):
        raise SetupError(f"配置文件不是 JSON 对象：{path}")
    return payload


def setup_complete(payload: dict[str, object]) -> bool:
    setup = payload.get("setup")
    return isinstance(setup, dict) and int(setup.get("version") or 0) >= SETUP_VERSION


def port_state(port: int) -> str:
    """Return central, occupied, or free without modifying the listener."""
    if health_is_ready(port=port):
        return "central"
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return "occupied"
    finally:
        probe.close()
    return "free"


def first_free_port(start: int = DEFAULT_PORT) -> int:
    for port in range(start, min(65536, start + PORT_SEARCH_LIMIT)):
        if port_state(port) == "free":
            return port
    raise SetupError(f"在 {start} 起的 {PORT_SEARCH_LIMIT} 个端口中没有找到可用端口。")


def select_central_port(
    payload: dict[str, object], *, ask: Callable[[str], str] = input,
) -> tuple[int, bool]:
    try:
        configured = int(payload.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError) as error:
        raise SetupError("中央服务端口配置无效。") from error
    state = port_state(configured)
    if state != "occupied":
        return configured, False
    replacement = first_free_port(configured + 1)
    if not payload:
        return replacement, True
    print(f"端口 {configured} 已被其他程序占用，可用端口为 {replacement}。")
    answer = ask(
        f"切换中央服务到 {replacement} 吗？已有花生壳映射也需要改为该端口。[Y/n] "
    ).strip()
    if answer.lower().startswith("n"):
        raise SetupError("端口冲突尚未解决，中央服务没有启动。")
    return replacement, True


def _save_port(path: Path, payload: dict[str, object], port: int) -> None:
    updated = dict(payload)
    updated["host"] = "127.0.0.1"
    updated["port"] = port
    _secure_atomic_write_json(path, updated)


def _update_local_pc_endpoint(previous_port: int, new_port: int) -> bool:
    """Keep an already-paired same-host PC profile on the selected local port."""
    path = default_data_dir().parent / "client" / "config.json"
    payload = _read_object(path)
    expected = f"http://127.0.0.1:{previous_port}"
    if payload.get("central_base_url") != expected:
        return False
    updated = dict(payload)
    updated["central_base_url"] = f"http://127.0.0.1:{new_port}"
    _secure_atomic_write_json(path, updated)
    return True


def _wait_for_central(port: int, timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_is_ready(port=port):
            return
        time.sleep(0.25)
    raise SetupError(f"中央服务未能在 {timeout:.0f} 秒内监听 127.0.0.1:{port}。")


def _start_launcher(launcher: Path, port: int) -> None:
    if health_is_ready(port=port):
        print(f"中央服务已在 127.0.0.1:{port} 运行，直接复用。")
        return
    if not launcher.is_file():
        raise SetupError(f"未找到中央服务启动器：{launcher}")
    subprocess.Popen(
        [str(launcher)], cwd=str(launcher.parent), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _wait_for_central(port)
    print(f"中央服务已启动：127.0.0.1:{port}")


def _existing_endpoint() -> str | None:
    try:
        return str(load_endpoint(default_endpoint_path())["base_url"])
    except (EndpointError, ValueError):
        return None


def configure_connection(
    port: int,
    *,
    ask: Callable[[str], str] = input,
    force_reconfigure: bool = False,
    previous_port: int | None = None,
    previous_provider: str | None = None,
) -> tuple[str, str]:
    existing = _existing_endpoint()
    while True:
        can_keep = bool(existing) and not force_reconfigure
        print("\n请检查远程连接：")
        if can_keep:
            print(f"  当前地址：{existing}")
            print("  1. 保持当前配置并继续（默认）")
            print("  2. 配置或刷新 Tailscale")
            print("  3. 配置花生壳或其他 HTTPS")
            choice = ask("请输入 1、2 或 3，直接回车保持当前配置：").strip()
            if choice in {"", "1"}:
                return previous_provider or "existing", existing
            tailscale_choice, https_choice = "2", "3"
        else:
            if force_reconfigure and existing:
                print("中央端口已变化，原远程入口需要重新配置。")
            else:
                print("当前没有可用的远程地址。")
            print("  1. Tailscale（推荐，需已安装并登录）")
            print("  2. 花生壳或其他 HTTPS 内网穿透")
            choice = ask("请输入 1 或 2：").strip()
            tailscale_choice, https_choice = "1", "2"

        if choice == tailscale_choice:
            try:
                replace_port = (
                    previous_port
                    if force_reconfigure and previous_provider == "tailscale"
                    else None
                )
                return "tailscale", configure_tailscale(
                    central_port=port, previous_central_port=replace_port,
                )
            except TailscaleSetupError as error:
                print(f"Tailscale 配置失败：{error}")
                print("已保留原配置，请重新选择连接方式。")
        elif choice == https_choice:
            print("\n请先在内网穿透的基础设置中填写：")
            print("  内网主机：127.0.0.1")
            print(f"  内网端口：{port}")
            print("  协议：HTTPS")
            url = ask("建立映射后，请粘贴获得的 HTTPS 根地址：").strip()
            try:
                report = probe_endpoint(url, read_token(default_config_path()), timeout=15)
                save_endpoint(default_endpoint_path(), "peanuthull", str(report["base_url"]))
                return "https_tunnel", str(report["base_url"])
            except (EndpointError, ValueError) as error:
                print(f"地址验证失败：{error}")
                print("已保留原配置，请重新选择连接方式。")
        else:
            print("请输入菜单中的有效选项。")


def _paired_device_count(config: CentralConfig) -> int:
    return len(CentralStore(config.database_path, config.token_bindings).list_managed_devices())


def _offer_first_invitation(config: CentralConfig, base_url: str) -> None:
    if _paired_device_count(config) > 0:
        print("已发现配对设备，不再生成新的设备配对码。")
        return
    created = create_client_invitation(
        config_path=default_config_path(), endpoint_path=default_endpoint_path(),
        central_base_url=base_url,
    )
    copied = copy_to_clipboard(created.code)
    print("\n尚未发现配对设备，已生成一次设备配对码：")
    print(created.code)
    if copied:
        print("配对码也已复制到剪贴板。")
    print("启动 PC 客户端后，由客户端页面提示你粘贴配对码。")


def _mark_complete(path: Path, payload: dict[str, object], remote_mode: str) -> None:
    updated = dict(payload)
    updated["setup"] = {"version": SETUP_VERSION, "remote_mode": remote_mode}
    _secure_atomic_write_json(path, updated)


def run(launcher: Path, *, ask: Callable[[str], str] = input) -> int:
    config_path = default_config_path().expanduser().resolve()
    before = _read_object(config_path)
    try:
        previous_port = int(before.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        previous_port = DEFAULT_PORT
    endpoint_payload = before.get("public_endpoint")
    previous_provider = (
        str(endpoint_payload.get("provider"))
        if isinstance(endpoint_payload, dict) and endpoint_payload.get("provider")
        else None
    )
    port, changed = select_central_port(before, ask=ask)
    ensured = ensure_server_configuration(config_path=config_path, port=port)
    current = _read_object(ensured)
    if changed or int(current.get("port") or DEFAULT_PORT) != port:
        _save_port(ensured, current, port)
        if before and _update_local_pc_endpoint(previous_port, port):
            print("已同步更新同机 PC 客户端的本地中央地址。")
        current = _read_object(ensured)

    _start_launcher(launcher.expanduser().resolve(), port)
    was_complete = setup_complete(current)
    config = CentralConfig.from_environment({"LIFE_RADIO_CENTRAL_CONFIG": str(ensured)})
    if changed and before:
        print("\n中央端口已变化，需要重新确认远程连接。")
    elif was_complete:
        print("\n中央服务已就绪，请检查远程连接设置。")
    else:
        print("\n首次运行：中央服务本机部分已经就绪。")
    remote_mode, pairing_base_url = configure_connection(
        port,
        ask=ask,
        force_reconfigure=changed and bool(before),
        previous_port=previous_port,
        previous_provider=previous_provider,
    )
    if not was_complete:
        _offer_first_invitation(config, pairing_base_url)
    current = _read_object(ensured)
    _mark_complete(ensured, current, remote_mode)
    print("\n远程连接检查完成。" if was_complete else "\n首次设置完成。")
    print("日常启动可直接运行本目录中的 LifeLink Central Service.exe。")
    print(f"用户数据保存在：{default_data_dir().parent}")
    print("如果要让 AI 定时读取，请在完成 AI 配对后，由 AI 工具建立定时调用。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Life Link 首次安装与启动向导")
    parser.add_argument("--launcher", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.launcher)
    except (EOFError, SetupError, OSError, ValueError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
