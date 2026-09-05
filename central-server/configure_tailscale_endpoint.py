#!/usr/bin/env python3
"""Safely create Life Link's private Tailscale HTTPS entrance.

This is intentionally separate from the public-tunnel helper: it only configures
an explicitly selected Tailscale HTTPS port and never rewrites another Serve
route.  The central service itself remains loopback-only on port 8091.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence
from urllib.request import Request, urlopen

from central_endpoint import EndpointError, default_endpoint_path, probe_endpoint, read_token, save_endpoint


DEFAULT_HTTPS_PORT = 8443
TAILSCALE_COMMAND_TIMEOUT_SECONDS = 20


class TailscaleSetupError(RuntimeError):
    pass


def _run_tailscale(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["tailscale", *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TAILSCALE_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise TailscaleSetupError("未找到 Tailscale。请先安装并登录 Tailscale。") from error
    except subprocess.TimeoutExpired as error:
        command = " ".join(arguments)
        raise TailscaleSetupError(
            f"Tailscale 命令等待超过 {TAILSCALE_COMMAND_TIMEOUT_SECONDS} 秒：{command}"
        ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if "Access is denied" in detail:
            raise TailscaleSetupError(
                "Tailscale 拒绝修改配置。请以管理员身份启动 Life Link，"
                "或手动配置 Tailscale Serve。"
            )
        raise TailscaleSetupError(detail or f"Tailscale 命令失败（退出码 {completed.returncode}）。")
    return completed.stdout


def _load_json(raw: str, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TailscaleSetupError(f"无法读取 {description}。") from error
    if not isinstance(payload, dict):
        raise TailscaleSetupError(f"{description} 格式无效。")
    return payload


def _central_target(central_port: int) -> str:
    if not 1 <= central_port <= 65535:
        raise TailscaleSetupError("中央服务端口必须介于 1 和 65535 之间。")
    return f"http://127.0.0.1:{central_port}"


def _check_local_central(central_port: int) -> None:
    target = _central_target(central_port)
    try:
        with urlopen(Request(f"{target}/v1/health"), timeout=5) as response:
            payload = _load_json(response.read().decode("utf-8"), "中央服务状态")
    except Exception as error:
        raise TailscaleSetupError(
            f"中央服务未在 127.0.0.1:{central_port} 正常运行，请先启动中央服务。"
        ) from error
    if payload.get("status") != "ok" or payload.get("role") != "central":
        raise TailscaleSetupError(
            f"127.0.0.1:{central_port} 不是可用的 Life Link 中央服务。"
        )


def _tailnet_dns_name(status: dict[str, Any]) -> str:
    if status.get("BackendState") != "Running":
        raise TailscaleSetupError("Tailscale 当前未连接，请先登录并连接到 Tailnet。")
    self_node = status.get("Self")
    if not isinstance(self_node, dict):
        raise TailscaleSetupError("无法读取当前 Tailscale 设备。")
    dns_name = str(self_node.get("DNSName") or "").strip().rstrip(".")
    if not dns_name:
        raise TailscaleSetupError("当前 Tailnet 未提供 HTTPS DNS 名称。请在 Tailscale 中启用 MagicDNS/HTTPS。")
    return dns_name


def detect_https_endpoint(*, https_port: int = DEFAULT_HTTPS_PORT) -> str:
    """Return the current Tailnet HTTPS candidate without changing any route."""
    if not 1 <= https_port <= 65535:
        raise TailscaleSetupError("HTTPS 端口必须介于 1 和 65535 之间。")
    status = _load_json(_run_tailscale("status", "--json"), "Tailscale 状态")
    return f"https://{_tailnet_dns_name(status)}:{https_port}"


def _existing_proxy(serve_config: dict[str, Any], key: str) -> str | None:
    web = serve_config.get("Web")
    if not isinstance(web, dict):
        return None
    entry = web.get(key)
    if not isinstance(entry, dict):
        return None
    handlers = entry.get("Handlers")
    if not isinstance(handlers, dict):
        return ""
    root_handler = handlers.get("/")
    if not isinstance(root_handler, dict):
        return ""
    proxy = root_handler.get("Proxy")
    return str(proxy) if isinstance(proxy, str) else ""


def ensure_tailscale_route(
    *,
    https_port: int = DEFAULT_HTTPS_PORT,
    central_port: int = 8091,
    previous_central_port: int | None = None,
) -> str:
    """Detect Tailscale and non-destructively ensure its HTTPS route."""
    if not 1 <= https_port <= 65535:
        raise TailscaleSetupError("HTTPS 端口必须介于 1 和 65535 之间。")
    central_target = _central_target(central_port)
    print(f"1/5 检查本机中央服务 {central_port}…", flush=True)
    _check_local_central(central_port)
    print("2/5 读取 Tailscale 连接状态…", flush=True)
    status = _load_json(_run_tailscale("status", "--json"), "Tailscale 状态")
    dns_name = _tailnet_dns_name(status)
    endpoint = f"https://{dns_name}:{https_port}"
    serve_key = f"{dns_name}:{https_port}"
    print(f"3/5 检查 Tailscale HTTPS 端口 {https_port}…", flush=True)
    serve_config = _load_json(_run_tailscale("serve", "status", "--json"), "Tailscale Serve 配置")
    existing = _existing_proxy(serve_config, serve_key)
    previous_target = (
        _central_target(previous_central_port)
        if previous_central_port is not None else None
    )
    replacing_previous = existing is not None and existing == previous_target
    if existing is not None and existing != central_target and not replacing_previous:
        raise TailscaleSetupError(
            f"Tailscale HTTPS 端口 {https_port} 已被 {existing or '其他规则'} 使用；"
            "为避免覆盖，未做任何修改。"
        )
    if existing is None or replacing_previous:
        print(f"4/5 建立 {https_port} -> 127.0.0.1:{central_port} 转发…", flush=True)
        # `tailscale serve` without --bg intentionally stays in the foreground;
        # this initializer needs the persistent background configuration instead.
        _run_tailscale("serve", "--bg", f"--https={https_port}", central_target)
    else:
        print(f"4/5 端口 {https_port} 已正确指向 Life Link，复用现有转发。", flush=True)

    return endpoint


def configure(
    *,
    https_port: int = DEFAULT_HTTPS_PORT,
    central_port: int = 8091,
    previous_central_port: int | None = None,
) -> str:
    endpoint = ensure_tailscale_route(
        https_port=https_port,
        central_port=central_port,
        previous_central_port=previous_central_port,
    )
    try:
        print(f"5/5 验证 HTTPS 地址 {endpoint}…", flush=True)
        probe_endpoint(endpoint, read_token(default_endpoint_path()), timeout=15)
        save_endpoint(default_endpoint_path(), "tailscale", endpoint)
    except (EndpointError, ValueError) as error:
        raise TailscaleSetupError(
            f"Tailscale 转发已设置，但 Life Link HTTPS 验证失败：{error}"
        ) from error
    return endpoint


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配置 Life Link 的独立 Tailscale HTTPS 入口。")
    parser.add_argument("--https-port", type=int, default=DEFAULT_HTTPS_PORT)
    parser.add_argument("--central-port", type=int, default=8091)
    parser.add_argument("--previous-central-port", type=int)
    args = parser.parse_args(argv)
    try:
        endpoint = configure(
            https_port=args.https_port,
            central_port=args.central_port,
            previous_central_port=args.previous_central_port,
        )
    except TailscaleSetupError as error:
        print(f"失败：{error}", file=sys.stderr)
        return 2
    print("Life Link Tailscale HTTPS 入口已就绪：")
    print(endpoint)
    print("后续新生成的设备邀请码和 AI 配对包将使用此地址。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
