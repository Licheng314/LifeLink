"""Formal Windows entry point for a source checkout of the PC client."""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path


TITLE = "LifeLink PC 客户端启动失败"


def module_root() -> Path:
    """Resolve the owning module from source or the generated root EXE."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def source_python() -> Path | None:
    configured = os.environ.get("LIFE_LINK_SOURCE_PYTHON")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate
    local = os.environ.get("LOCALAPPDATA")
    if local:
        try:
            candidates = sorted((Path(local) / "Programs" / "Python").glob("Python*/pythonw.exe"), reverse=True)
        except OSError:
            candidates = []
        if candidates:
            return candidates[0]
    for name in ("pythonw.exe", "pythonw"):
        discovered = shutil.which(name)
        if discovered and "WindowsApps" not in Path(discovered).parts:
            return Path(discovered)
    return None


def command_for() -> tuple[list[str], Path]:
    python = source_python()
    script = module_root() / "start_central_client.py"
    if python is None:
        raise RuntimeError("未找到 Python 无窗口解释器；请安装 Python 或设置 LIFE_LINK_SOURCE_PYTHON。")
    if not script.is_file():
        raise RuntimeError(f"未找到 PC 客户端源码：{script}。请从完整项目目录启动。")
    return [str(python), str(script), "--background-start"], script.parent


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
    else:  # pragma: no cover - Windows entry point
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)
    try:
        command, workdir = command_for()
        if args.validate:
            print("Life Link PC source launcher is valid.")
            print("Python:", command[0])
            return 0
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(command, cwd=str(workdir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
        return 0
    except RuntimeError as error:
        show_error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
