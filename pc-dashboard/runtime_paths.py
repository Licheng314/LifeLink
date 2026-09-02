"""Resolve read-only program resources and the one per-user data root."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Directory containing bundled HTML, assets and Python modules."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def installation_dir() -> Path:
    """Program installation root; never use this for mutable user state."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_root(environ: Mapping[str, str] | None = None) -> Path:
    """Return the stable data root shared by every Life Link installation."""
    env = os.environ if environ is None else environ
    configured = env.get("LIFE_LINK_DATA_ROOT") or env.get("LIFE_LINK_RUNTIME_ROOT")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    profile = env.get("USERPROFILE")
    return (Path(profile) if profile else Path.home()) / "LifeLink"


def client_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    return user_data_root(environ) / "client"


def executable_path() -> Path:
    if not is_frozen():
        raise RuntimeError("only packaged builds have an executable entry")
    return Path(sys.executable).resolve()
