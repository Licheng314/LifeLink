#!/usr/bin/env python3
"""Create one-time Life Link device invitation codes."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from central.config import CentralConfig, default_config_path
from central.invitations import CreatedInvitation, create_invitation
from central.storage import CentralStore
from central_endpoint import EndpointError, default_endpoint_path, load_endpoint


WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def create_client_invitation(
    *,
    config_path: Path,
    endpoint_path: Path,
    scope: str = "dashboard",
    hours: float = 24,
    central_base_url: str | None = None,
) -> CreatedInvitation:
    if hours <= 0:
        raise ValueError("hours must be positive")
    config = CentralConfig.from_environment(
        {"LIFE_RADIO_CENTRAL_CONFIG": str(config_path.expanduser().resolve())}
    )
    if scope == "dashboard" and config.read_token is None:
        raise ValueError("dashboard invitations require a configured read token")
    endpoint = (
        {"base_url": central_base_url.rstrip("/")}
        if central_base_url is not None
        else load_endpoint(endpoint_path.expanduser().resolve())
    )
    store = CentralStore(config.database_path, config.token_bindings)
    return create_invitation(
        store,
        central_base_url=str(endpoint["base_url"]),
        scope=scope,
        lifetime=timedelta(hours=hours),
    )


def copy_to_clipboard(value: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        completed = subprocess.run(
            ["clip.exe"],
            input=value,
            text=True,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=WINDOWS_NO_WINDOW,
        )
    except OSError:
        return False
    return completed.returncode == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a one-time Life Link device invitation",
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument(
        "--endpoint-config",
        type=Path,
        default=default_endpoint_path(),
    )
    parser.add_argument("--scope", choices=("dashboard", "upload"), default="dashboard")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--no-clipboard", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        created = create_client_invitation(
            config_path=args.config,
            endpoint_path=args.endpoint_config,
            scope=args.scope,
            hours=args.hours,
        )
        if not args.no_clipboard:
            copy_to_clipboard(created.code)
        # Deliberately emit exactly one line: it is the user-requested transfer
        # artifact. No logger receives the invitation token.
        print(created.code)
        return 0
    except (ValueError, EndpointError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
