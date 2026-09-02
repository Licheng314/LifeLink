"""Current-user Windows login startup for the LifeLink central service.

Windows needs an absolute target in a Startup-folder shortcut. Source checkouts
use the project-local formal EXE launcher; packaged builds use their own EXE.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only feature
    winreg = None


PREFERENCE_KEY = r"Software\Life Link"
PREFERENCE_NAME = "central_login_startup_enabled"
LEGACY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
LEGACY_STARTUP_APPROVED_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run32",
)
VALUE_NAME = "LifeLink Central Service"
LEGACY_VALUE_NAMES = (VALUE_NAME, "Life Link Central Service")
SHORTCUT_NAME = "LifeLink Central Service.lnk"
BASE_DIR = Path(__file__).resolve().parent
ICON_PATH = BASE_DIR / "assets" / "life-link-server-tray.ico"


def source_launcher_path() -> Path:
    return BASE_DIR / "LifeLink Central Service.exe"


def startup_target() -> Path:
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) else source_launcher_path()


def startup_folder(*, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    app_data = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(app_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path(*, environ: Mapping[str, str] | None = None) -> Path:
    return startup_folder(environ=environ) / SHORTCUT_NAME


def startup_command(target: Path | None = None, *, environ: Mapping[str, str] | None = None) -> str:
    """Return the formal EXE shortcut command without secrets."""
    return f'"{(target or startup_target()).resolve()}"'


def _preference(*, registry=winreg) -> bool | None:
    if registry is None:
        return None
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, PREFERENCE_KEY) as key:
            value, _ = registry.QueryValueEx(key, PREFERENCE_NAME)
    except FileNotFoundError:
        return None
    return bool(value)


def _windows_blocked(*, registry=winreg) -> bool:
    if registry is None:
        return False
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY) as key:
            value, _ = registry.QueryValueEx(key, SHORTCUT_NAME)
    except FileNotFoundError:
        return False
    return isinstance(value, bytes) and bool(value) and value[0] % 2 == 1


def status(*, registry=winreg, environ: Mapping[str, str] | None = None) -> dict[str, bool | str]:
    registered = shortcut_path(environ=environ).is_file()
    blocked = _windows_blocked(registry=registry)
    requested = bool(_preference(registry=registry))
    state = (
        "blocked" if blocked
        else ("enabled" if registered else ("missing" if requested else "disabled"))
    )
    return {"enabled": registered and not blocked, "registered": registered,
            "blocked_by_windows": blocked, "requested_enabled": requested,
            "state": state}


def is_enabled(*, registry=winreg) -> bool:
    return bool(status(registry=registry)["enabled"])


def _powershell_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_shortcut(*, environ: Mapping[str, str] | None = None) -> None:
    path = shortcut_path(environ=environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = startup_target()
    if not target.is_file():
        raise RuntimeError(f"未找到 Life Link 中央服务启动器：{target}")
    script = "; ".join((
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$shortcut = $shell.CreateShortcut({_powershell_string(str(path))})",
        f"$shortcut.TargetPath = {_powershell_string(str(target.resolve()))}",
        "$shortcut.Arguments = ''",
        f"$shortcut.WorkingDirectory = {_powershell_string(str(target.parent))}",
        f"$shortcut.IconLocation = {_powershell_string(str(target.resolve()) + ',0')}",
        "$shortcut.Description = 'LifeLink 中央服务'", "$shortcut.Save()",
        f"if (-not (Test-Path -LiteralPath {_powershell_string(str(path))} -PathType Leaf)) {{ throw 'Startup shortcut was not created.' }}",
        f"$saved = $shell.CreateShortcut({_powershell_string(str(path))})",
        f"if ([IO.Path]::GetFullPath($saved.TargetPath) -ne [IO.Path]::GetFullPath({_powershell_string(str(target.resolve()))})) {{ throw 'Startup shortcut target is incorrect.' }}",
    ))
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                       check=True, capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise RuntimeError(f"创建 Life Link 中央服务启动项失败：{detail}") from error
    if not path.is_file():
        raise RuntimeError(f"启动项未生成：{path}")


def _delete_registry_value(key_path: str, value_name: str, *, registry=winreg) -> None:
    if registry is None:
        return
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, key_path, 0, registry.KEY_SET_VALUE) as key:
            registry.DeleteValue(key, value_name)
    except FileNotFoundError:
        pass


def _remove_legacy_startup_records(*, registry=winreg) -> None:
    """Remove the retired wscript/VBS registration and its approval records."""
    for value_name in LEGACY_VALUE_NAMES:
        _delete_registry_value(LEGACY_RUN_KEY, value_name, registry=registry)
        for key_path in LEGACY_STARTUP_APPROVED_KEYS:
            _delete_registry_value(key_path, value_name, registry=registry)


def _clear_windows_block(*, registry=winreg) -> None:
    _delete_registry_value(STARTUP_APPROVED_KEY, SHORTCUT_NAME, registry=registry)


def set_enabled(enabled: bool, *, registry=winreg) -> dict[str, bool | str]:
    """Apply the user's explicit choice and recover LifeLink's own block."""
    if registry is None:
        raise RuntimeError("登录后启动仅支持 Windows")
    with registry.CreateKey(registry.HKEY_CURRENT_USER, PREFERENCE_KEY) as key:
        registry.SetValueEx(key, PREFERENCE_NAME, 0, registry.REG_DWORD, int(enabled))
    if enabled:
        _clear_windows_block(registry=registry)
        _remove_legacy_startup_records(registry=registry)
        _create_shortcut()
    else:
        shortcut_path().unlink(missing_ok=True)
        _clear_windows_block(registry=registry)
        _remove_legacy_startup_records(registry=registry)
    return status(registry=registry)


def ensure_default_enabled(*, registry=winreg) -> bool:
    """Register once by default, without overturning a later Windows block."""
    if registry is None:
        return False
    if not startup_target().is_file():
        return False
    preference = _preference(registry=registry)
    enabled = True if preference is None else preference
    if preference is None:
        with registry.CreateKey(registry.HKEY_CURRENT_USER, PREFERENCE_KEY) as key:
            registry.SetValueEx(key, PREFERENCE_NAME, 0, registry.REG_DWORD, 1)
    if enabled:
        _remove_legacy_startup_records(registry=registry)
        _create_shortcut()
    else:
        shortcut_path().unlink(missing_ok=True)
    return bool(status(registry=registry)["enabled"])


def main() -> int:
    """Register the project-local launcher immediately after it is built."""
    try:
        enabled = ensure_default_enabled()
    except (OSError, RuntimeError) as error:
        print(f"Life Link 中央服务开机启动登记失败：{error}", file=sys.stderr)
        return 2
    if enabled:
        print("Life Link 中央服务已登记为登录后自动启动。")
    else:
        print("中央服务启动器已生成；Windows 或用户设置当前阻止自动启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
