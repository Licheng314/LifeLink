#!/usr/bin/env python3
"""One-time migration from installation-local data to %USERPROFILE%/LifeLink."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _device_id(config: dict) -> str:
    device = config.get("device")
    return str(device.get("device_id") or "") if isinstance(device, dict) else ""


def _assert_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"port {port} is active; stop Life Link before migration")


def _copy_optional(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _quick_check(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {path}")


def _remove(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--preserve-client-id", required=True)
    parser.add_argument("--retire-client-id", required=True)
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    root = args.data_root.resolve()
    package = args.package_dir.resolve()
    source_client = repo / "runtime" / "client"
    source_central = repo / "runtime" / "central"
    source_ai = repo / "runtime" / "ai"
    source_client_config = repo / "pc-dashboard" / "config.json"
    source_central_config = repo / "central-server" / "config.json"
    package_client = package / "runtime" / "client"
    package_config = package / "config.json"

    for port in (8090, 8091):
        _assert_port_free(port)
    original_identity = _json(source_client / "identity.json").get("device_id")
    original_config_id = _device_id(_json(source_client_config))
    package_identity = _json(package_client / "identity.json").get("device_id")
    package_config_id = _device_id(_json(package_config))
    if {original_identity, original_config_id} != {args.preserve_client_id}:
        raise RuntimeError("original client identity/config do not match the preserved ID")
    if {package_identity, package_config_id} != {args.retire_client_id}:
        raise RuntimeError("package client identity/config do not match the retired ID")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "backups" / f"installation-data-migration-{timestamp}"
    if backup.exists():
        raise RuntimeError(f"backup already exists: {backup}")
    _copy_optional(source_client, backup / "project-runtime-client")
    _copy_optional(source_central, backup / "project-runtime-central")
    _copy_optional(source_ai, backup / "project-runtime-ai")
    _copy_optional(repo / "runtime" / "backups", backup / "older-project-backups")
    _copy_optional(source_client_config, backup / "project-pc-config.json")
    _copy_optional(source_central_config, backup / "project-central-config.json")
    _copy_optional(repo / "central-server" / "central.sqlite3", backup / "non-authoritative-central.sqlite3")
    _copy_optional(package_client, backup / "retired-package-client")
    _copy_optional(package_config, backup / "retired-package-config.json")

    stage = root / f".migration-{uuid.uuid4().hex}"
    try:
        _copy_optional(source_client, stage / "client")
        _copy_optional(source_central, stage / "central")
        _copy_optional(source_ai, stage / "ai")
        shutil.copy2(source_client_config, stage / "client" / "config.json")
        central_config = _json(source_central_config)
        bindings = central_config.get("token_bindings")
        if not isinstance(bindings, dict):
            raise RuntimeError("central token_bindings are missing")
        central_config["token_bindings"] = {
            token: device_id for token, device_id in bindings.items()
            if device_id != args.retire_client_id
        }
        central_config["database_path"] = str(root / "central" / "life_radio.sqlite3")
        (stage / "central" / "config.json").write_text(
            json.dumps(central_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if _json(stage / "client" / "identity.json").get("device_id") != args.preserve_client_id:
            raise RuntimeError("staged client identity changed")
        _quick_check(stage / "client" / "outbox.sqlite3")
        _quick_check(stage / "central" / "life_radio.sqlite3")

        for name in ("client", "central", "ai"):
            destination = root / name
            if destination.exists():
                if any(destination.iterdir()):
                    raise RuntimeError(f"destination is not empty: {destination}")
                destination.rmdir()
            staged = stage / name
            if staged.exists():
                staged.replace(destination)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    sys.path.insert(0, str(repo / "central-server"))
    from central.storage import CentralStore

    final_central_config = _json(root / "central" / "config.json")
    store = CentralStore(
        root / "central" / "life_radio.sqlite3",
        final_central_config["token_bindings"],
    )
    retired = store.retire_device(args.retire_client_id)

    # Keep the project-local originals as an explicit rollback source until a
    # real startup has verified the new root. Only the user-selected trial
    # package identity and its local database are deleted immediately.
    for obsolete in (package_client, package_config):
        _remove(obsolete)
    package_runtime = package / "runtime"
    if package_runtime.exists() and not any(package_runtime.iterdir()):
        package_runtime.rmdir()

    print(json.dumps({
        "data_root": str(root),
        "preserved_client_id": args.preserve_client_id,
        "retired_client_id": args.retire_client_id,
        "central_device_retired": retired,
        "backup": str(backup),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
