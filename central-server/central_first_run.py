#!/usr/bin/env python3
"""Idempotent first-run guide for a Life Link source checkout."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable, Sequence
from urllib.request import ProxyHandler, build_opener

from central.bootstrap import ensure_server_configuration
from central.config import default_config_path, default_data_dir
from central.operations import _secure_atomic_write_json


DEFAULT_PORT = 8091
MANAGEMENT_PORT = 8092
MANAGEMENT_URL = f"http://127.0.0.1:{MANAGEMENT_PORT}"
PORT_SEARCH_LIMIT = 100


class SetupError(RuntimeError):
    pass


def central_is_ready(port: int, timeout: float = 1.0) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/v1/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("role") == "central"
        )
    except Exception:
        return False


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


def port_state(port: int) -> str:
    """Return central, occupied, or free without modifying the listener."""
    if central_is_ready(port):
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
        f"切换中央服务到 {replacement} 吗？已有外部 HTTPS 转发也需要改为该端口。[Y/n] "
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


def management_is_ready(timeout: float = 1.0) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"{MANAGEMENT_URL}/api/status", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("role") == "life-link-central-management"
        )
    except Exception:
        return False


def _wait_for_central(port: int, timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if central_is_ready(port) and management_is_ready():
            return
        time.sleep(0.25)
    raise SetupError(
        f"中央服务未能在 {timeout:.0f} 秒内同时准备数据端口 "
        f"127.0.0.1:{port} 和管理端口 127.0.0.1:{MANAGEMENT_PORT}。"
    )


def _start_launcher(launcher: Path, port: int) -> None:
    if central_is_ready(port) and management_is_ready():
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


def run(launcher: Path, *, ask: Callable[[str], str] = input) -> int:
    config_path = default_config_path().expanduser().resolve()
    before = _read_object(config_path)
    try:
        previous_port = int(before.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        previous_port = DEFAULT_PORT
    port, changed = select_central_port(before, ask=ask)
    ensured = ensure_server_configuration(config_path=config_path, port=port)
    current = _read_object(ensured)
    if changed or int(current.get("port") or DEFAULT_PORT) != port:
        _save_port(ensured, current, port)
        if before and _update_local_pc_endpoint(previous_port, port):
            print("已同步更新同机 PC 客户端的本地中央地址。")
        current = _read_object(ensured)

    _start_launcher(launcher.expanduser().resolve(), port)
    if changed and before:
        print("\n中央端口已变化，请在管理 WebUI 中重新验证外部 HTTPS 地址。")
    else:
        print("\n中央服务本机部分已经就绪。")
    if not webbrowser.open(MANAGEMENT_URL):
        print(f"请手动打开中央管理 WebUI：{MANAGEMENT_URL}")
    else:
        print(f"中央管理 WebUI：{MANAGEMENT_URL}")
    print("请在 WebUI 中配置并验证网络地址，再生成设备或 AI 配对材料。")
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
