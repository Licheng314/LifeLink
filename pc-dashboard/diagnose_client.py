#!/usr/bin/env python3
"""Read-only diagnostics for split Life Link PC client state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from central_client_setup import default_client_config_path
from device_identity import default_identity_path


def diagnose(
    identity_path: Path | None = None,
    config_path: Path | None = None,
    outbox_path: Path | None = None,
) -> dict[str, Any]:
    identity_file = (identity_path or default_identity_path()).expanduser().resolve()
    config_file = (config_path or default_client_config_path()).expanduser().resolve()
    outbox_file = (
        outbox_path or identity_file.with_name("outbox.sqlite3")
    ).expanduser().resolve()
    identity = json.loads(identity_file.read_text(encoding="utf-8"))
    config = json.loads(config_file.read_text(encoding="utf-8"))
    database = sqlite3.connect(f"file:{outbox_file.as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(database.execute("PRAGMA integrity_check").fetchone()[0])
        states = {
            str(state): int(count)
            for state, count in database.execute(
                "SELECT state, COUNT(*) FROM outbox_events GROUP BY state ORDER BY state"
            )
        }
    finally:
        database.close()
    configured_device = config.get("device") if isinstance(config, dict) else None
    configured_device_id = (
        configured_device.get("device_id")
        if isinstance(configured_device, dict)
        else None
    )
    return {
        "role": "pc_client",
        "identity_path": str(identity_file),
        "config_path": str(config_file),
        "outbox_path": str(outbox_file),
        "identity_matches_profile": identity.get("device_id") == configured_device_id,
        "outbox_integrity": integrity,
        "outbox_states": states,
    }


def main() -> int:
    try:
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
        return 0
    except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"role": "pc_client", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
