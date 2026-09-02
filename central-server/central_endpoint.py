#!/usr/bin/env python3
"""Configure and verify a public HTTPS entrance for Life Link Central."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from central.config import CentralConfig, default_config_path, legacy_data_dir
from central.operations import _secure_atomic_write_json


PROVIDERS = ("peanuthull", "ngrok", "cloudflare", "tailscale", "custom")


class EndpointError(RuntimeError):
    pass


def default_endpoint_path() -> Path:
    return default_config_path()


def legacy_endpoint_path() -> Path:
    return legacy_data_dir() / "public_endpoint.json"


def normalize_base_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https":
        raise ValueError("public endpoint must use https://")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("public endpoint must contain a valid hostname")
    if parsed.query or parsed.fragment:
        raise ValueError("public endpoint must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("public endpoint must be an origin URL without a path")
    return urlunsplit(("https", parsed.netloc.lower(), "", "", ""))


def _fetch_json(request: Request, timeout: float) -> dict[str, Any]:
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise EndpointError("endpoint returned a non-object JSON response")
    return payload


def probe_endpoint(
    base_url: str,
    read_token: str | None = None,
    *,
    timeout: float = 15,
    fetch_json: Callable[[Request, float], dict[str, Any]] = _fetch_json,
) -> dict[str, Any]:
    normalized = normalize_base_url(base_url)
    try:
        health = fetch_json(Request(f"{normalized}/v1/health"), timeout)
    except Exception as error:
        raise EndpointError(f"public endpoint is unreachable: {error}") from error

    if health.get("status") != "ok":
        raise EndpointError("public endpoint health check is not ok")
    if health.get("role") != "central":
        if isinstance(health.get("device"), dict):
            raise EndpointError(
                "this address points to the PC Dashboard, not Life Link Central; "
                "make the tunnel's internal port match the central configuration"
            )
        raise EndpointError("the endpoint is not a Life Link Central service")

    report: dict[str, Any] = {
        "base_url": normalized,
        "health": "ok",
        "role": "central",
        "authenticated_read": False,
    }
    if read_token:
        now = datetime.now(timezone.utc)
        query = urlencode({
            "from": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "to": now.isoformat().replace("+00:00", "Z"),
        })
        request = Request(
            f"{normalized}/v1/read/devices?{query}",
            headers={"Authorization": f"Bearer {read_token}"},
        )
        try:
            payload = fetch_json(request, timeout)
        except Exception as error:
            raise EndpointError(f"authenticated central read failed: {error}") from error
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise EndpointError("authenticated central read returned an invalid response")
        report["authenticated_read"] = True
        report["device_count"] = len(devices)
    return report


def save_endpoint(path: Path, provider: str, base_url: str) -> None:
    path = path.expanduser().resolve()
    try:
        container = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        container = {}
    except (OSError, json.JSONDecodeError) as error:
        raise EndpointError("central configuration is unreadable") from error
    if not isinstance(container, dict):
        raise EndpointError("central configuration must be a JSON object")
    endpoint = {
        "version": 1,
        "provider": provider,
        "base_url": normalize_base_url(base_url),
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    container["public_endpoint"] = endpoint
    _secure_atomic_write_json(path, container)


def load_endpoint(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        container = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        container = {}
    except (OSError, json.JSONDecodeError) as error:
        raise EndpointError("public endpoint configuration is unreadable") from error
    if not isinstance(container, dict):
        raise EndpointError("public endpoint configuration must be a JSON object")
    payload = container.get("public_endpoint")
    if payload is None and {"provider", "base_url"}.issubset(container):
        payload = container
    if payload is None and resolved == default_config_path().expanduser().resolve():
        legacy = legacy_endpoint_path().expanduser().resolve()
        if legacy.exists():
            migrated = load_endpoint(legacy)
            save_endpoint(resolved, str(migrated["provider"]), str(migrated["base_url"]))
            legacy.unlink(missing_ok=True)
            return migrated
    if not isinstance(payload, dict):
        raise EndpointError("no public endpoint has been configured")
    provider = payload.get("provider")
    if provider not in PROVIDERS:
        raise EndpointError("public endpoint configuration has an invalid provider")
    payload["base_url"] = normalize_base_url(str(payload.get("base_url") or ""))
    return payload


def read_token(config_path: Path) -> str | None:
    environment = dict(os.environ)
    environment["LIFE_RADIO_CENTRAL_CONFIG"] = str(config_path.expanduser().resolve())
    return CentralConfig.from_environment(environment).read_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the Life Link public endpoint")
    parser.add_argument("command", choices=("configure", "status"))
    parser.add_argument("--url", help="public HTTPS origin")
    parser.add_argument("--provider", choices=PROVIDERS, default="custom")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--endpoint-config", type=Path, default=default_endpoint_path())
    parser.add_argument("--timeout", type=float, default=15)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            if not args.url:
                raise ValueError("configure requires --url https://...")
            base_url = normalize_base_url(args.url)
            provider = args.provider
        else:
            configured = load_endpoint(args.endpoint_config)
            base_url = configured["base_url"]
            provider = configured["provider"]

        report = probe_endpoint(
            base_url,
            read_token(args.config),
            timeout=args.timeout,
        )
        if args.command == "configure":
            save_endpoint(args.endpoint_config, provider, base_url)
        print(json.dumps({"provider": provider, **report}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, EndpointError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
