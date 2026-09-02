"""Persistent, file-backed blacklist rule cache for the PC client.

The central SQLite is the sole authoritative source.  This module provides
a write-through, disk-persisted cache that survives process restarts.

- `None` (attribute absent) means "never loaded".
- An empty list of rules means "central returned an empty ruleset" — this
  is a valid, intentional state and must not be overwritten by seed defaults.
- Only a successful central response changes the cache.
- A missing file at startup means the cache is uninitialised.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from device_identity import default_client_data_dir


CACHE_FILENAME = "blacklist_cache.json"


def cache_path() -> Path:
    return default_client_data_dir() / CACHE_FILENAME


def load() -> dict[str, Any] | None:
    """Return the last known-good ruleset, or None if never loaded."""
    target = cache_path()
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or "rules" not in payload:
        return None
    return payload


def save(rules: list[dict[str, Any]]) -> None:
    """Persist a successful central response to disk."""
    payload = {
        "rules": rules,
        "cached_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    target = cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear() -> None:
    """Remove the cache file (used to reset state in tests)."""
    target = cache_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass
