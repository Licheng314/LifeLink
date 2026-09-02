#!/usr/bin/env python3
"""Run, initialize, or diagnose the independent Life Link central service."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from central.config import CentralConfig, default_config_path
from central.http import MissingDeviceTokenError, create_server
from central.operations import initialize_device
from central.storage import readonly_diagnostics
import central_windows_startup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Life Link central context service")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "init", "diagnose"),
        default="run",
        help="run the service (default), initialize a device, or inspect local metadata",
    )
    parser.add_argument("--config", type=Path, help="external JSON config path")
    parser.add_argument("--host", help="listen address")
    parser.add_argument("--port", type=int, help="listen port")
    parser.add_argument("--database", type=Path, help="SQLite database path")
    parser.add_argument("--device-id", help="stable device ID used by the init command")
    return parser


def load_config(config_path: Path | None) -> CentralConfig:
    env = dict(os.environ)
    if config_path is not None:
        env["LIFE_RADIO_CENTRAL_CONFIG"] = str(config_path.expanduser().resolve())
    return CentralConfig.from_environment(env)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            return run_init(args)
        if args.command == "diagnose":
            return run_diagnose(args)
        return run_server(args)
    except (FileNotFoundError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


def run_init(args: argparse.Namespace) -> int:
    if not args.device_id:
        raise ValueError("init requires --device-id <stable-device-id>")
    result = initialize_device(
        device_id=args.device_id,
        config_path=args.config,
        host=args.host or "127.0.0.1",
        port=args.port if args.port is not None else 8091,
        database_path=args.database,
    )
    action = "created" if result.created_config else "updated"
    print(f"Central configuration {action}: {result.config_path}")
    print(f"Device credential added: {result.device_id}")
    read_action = "generated" if result.created_read_token else "preserved"
    print(f"Independent read credential {read_action} in the same external file.")
    print("Generated secrets were stored only in that external file and were not printed.")
    central_windows_startup.ensure_default_enabled()
    return 0


def run_diagnose(args: argparse.Namespace) -> int:
    database_path = args.database
    if database_path is None:
        database_path = load_config(args.config).database_path
    report = readonly_diagnostics(database_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_server(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.host is not None:
        config = replace(config, host=args.host)
    if args.port is not None:
        config = replace(config, port=args.port)
    if args.database is not None:
        config = replace(config, database_path=args.database)

    try:
        server = create_server(config)
    except MissingDeviceTokenError as error:
        selected_path = args.config or default_config_path()
        print(f"Error: {error}", file=sys.stderr)
        print(f"Configuration path: {selected_path}", file=sys.stderr)
        return 2

    print("=" * 54)
    print("  Life Link — Personal Context for AI")
    print("=" * 54)
    print(f"  Listen   : {config.host}:{config.port}")
    print(f"  Database : {config.database_path}")
    print(f"  Devices  : {len(config.token_bindings)} credential(s) configured")
    print("  P2P/AW/TS: disabled")
    print("=" * 54)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCentral service stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
