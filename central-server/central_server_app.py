#!/usr/bin/env python3
"""Windows tray host for the independent Life Link central server."""

from __future__ import annotations

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
import uuid
import webbrowser
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox
from urllib.request import ProxyHandler, Request, build_opener

from central.config import (
    CentralConfig,
    default_config_path,
    default_data_dir,
    legacy_config_path,
    legacy_project_config_path,
)
from central.operations import _secure_atomic_write_json, initialize_device
from central_endpoint import default_endpoint_path
from central_invitation import copy_to_clipboard, create_client_invitation
import central_windows_startup


BASE_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = BASE_DIR / "central_server.py"
TRAY_ICON_FILE = BASE_DIR / "assets" / "life-link-server-tray.ico"
DEFAULT_SERVER_PORT = 8091
HEALTH_URL = f"http://127.0.0.1:{DEFAULT_SERVER_PORT}/v1/health"
MCP_CONNECTION_PACKAGE_URL = (
    "http://127.0.0.1:8090/api/ai-reader-connection-package/open"
)
MUTEX_NAME = "Local\\LifeRadioCentralServer"
WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def request_mcp_connection_package(*, opener=None) -> dict[str, object]:
    """Ask the local PC service to create and reveal the complete MCP bundle."""
    client = opener or build_opener(ProxyHandler({}))
    request = Request(MCP_CONNECTION_PACKAGE_URL, data=b"", method="POST")
    with client.open(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("filename"), str):
        raise ValueError("PC 客户端返回了无效的 MCP 连接包结果")
    return payload


def configure_logging() -> None:
    log_dir = default_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "central_server_app.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"配置文件不是 JSON 对象：{path}")
    return payload


def ensure_server_configuration(
    config_path: Path | None = None,
    identity_path: Path | None = None,
    *,
    port: int = DEFAULT_SERVER_PORT,
) -> Path:
    """Ensure the server has a stable identity and a usable configuration."""
    target = (config_path or default_config_path()).expanduser().resolve()
    if (
        not target.exists()
        and target == default_config_path().expanduser().resolve()
    ):
        candidates = (
            legacy_project_config_path().expanduser().resolve(),
            legacy_config_path().expanduser().resolve(),
        )
        for legacy in candidates:
            if legacy != target and legacy.exists():
                _secure_atomic_write_json(target, _read_json(legacy))
                break
    existing = _read_json(target)
    bindings = existing.get("token_bindings")
    read_token = existing.get("read_token")

    identity_path = identity_path or (default_data_dir() / "server_identity.json")
    identity = _read_json(identity_path)
    server_id = identity.get("device_id")
    if not isinstance(server_id, str) or not server_id:
        server_id = f"central-server-{uuid.uuid4()}"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(
            json.dumps({"device_id": server_id}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # Older installations can already have a complete central config but no
    # server_identity.json because that file was introduced later. Identity
    # initialization must therefore happen before this early return.
    if isinstance(bindings, dict) and bindings and isinstance(read_token, str) and read_token:
        central_windows_startup.ensure_default_enabled()
        return target

    if isinstance(bindings, dict) and server_id in bindings.values():
        server_id = f"central-server-{uuid.uuid4()}"
    if not identity_path.exists() or identity.get("device_id") != server_id:
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(
            json.dumps({"device_id": server_id}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    initialize_device(device_id=server_id, config_path=target, host="127.0.0.1", port=port)
    central_windows_startup.ensure_default_enabled()
    return target


def health_is_ready(timeout: float = 1.0, *, port: int = DEFAULT_SERVER_PORT) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/v1/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("role") == "central"
        )
    except Exception:
        return False


if sys.platform == "win32":
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
            ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HANDLE),
        ]


class CentralServerTray:
    CALLBACK_MESSAGE = 0x8000 + 29
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    WM_NULL = 0
    PM_REMOVE = 1
    NIM_ADD = 0
    NIM_DELETE = 2
    NIF_MESSAGE = 1
    NIF_ICON = 2
    NIF_TIP = 4
    MF_STRING = 0
    MF_GRAYED = 1
    MF_SEPARATOR = 0x800
    TPM_RIGHTBUTTON = 2
    TPM_RETURNCMD = 0x100
    ID_OPEN_HEALTH = 2001
    ID_RESTART = 2002
    ID_EXIT = 2003
    ID_CREATE_INVITATION = 2004
    ID_CREATE_MCP_CONNECTION_PACKAGE = 2005
    ID_TOGGLE_LOGIN_STARTUP = 2006
    IDI_INFORMATION = 32516
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    SM_CXSMICON = 49
    SM_CYSMICON = 50

    def __init__(self, root: tk.Tk, app: "CentralServerApp") -> None:
        self.root = root
        self.app = app
        self.commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.closed = False
        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self.user32.DefWindowProcW.restype = LRESULT
        self.user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        self.user32.LoadIconW.restype = wintypes.HANDLE
        self.user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        self.user32.LoadImageW.restype = wintypes.HANDLE
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.DestroyIcon.argtypes = [wintypes.HANDLE]
        self.user32.DestroyIcon.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.AppendMenuW.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
        ]
        self.user32.AppendMenuW.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.POINTER(wintypes.RECT),
        ]
        self.user32.TrackPopupMenu.restype = wintypes.UINT
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self.user32.DestroyMenu.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT, wintypes.UINT,
        ]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = LRESULT
        self.shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW),
        ]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
        self.class_name = f"LifeRadioCentralTray_{os.getpid()}"
        self.hinstance = self.kernel32.GetModuleHandleW(None)
        self.owns_icon = False
        self.icon = self._load_tray_icon()
        self._wndproc = WNDPROC(self._window_proc)
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = self.hinstance
        window_class.hIcon = self.icon
        window_class.lpszClassName = self.class_name
        if not self.user32.RegisterClassW(ctypes.byref(window_class)):
            self._release_icon()
            raise ctypes.WinError()
        self.hwnd = self.user32.CreateWindowExW(
            0, self.class_name, "Life Link Central Server", 0,
            0, 0, 0, 0, None, None, self.hinstance, None,
        )
        if not self.hwnd:
            self.user32.UnregisterClassW(self.class_name, self.hinstance)
            self._release_icon()
            raise ctypes.WinError()
        self.notify_data = NOTIFYICONDATAW()
        self.notify_data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self.notify_data.hWnd = self.hwnd
        self.notify_data.uID = 1
        self.notify_data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        self.notify_data.uCallbackMessage = self.CALLBACK_MESSAGE
        self.notify_data.hIcon = self.icon
        self.notify_data.szTip = "Life Link 中央服务端"
        if not self.shell32.Shell_NotifyIconW(0, ctypes.byref(self.notify_data)):
            self.user32.DestroyWindow(self.hwnd)
            self.user32.UnregisterClassW(self.class_name, self.hinstance)
            self._release_icon()
            raise ctypes.WinError()
        self.root.after(50, self.pump_messages)

    def _load_tray_icon(self) -> wintypes.HANDLE:
        if TRAY_ICON_FILE.is_file():
            width = self.user32.GetSystemMetrics(self.SM_CXSMICON)
            height = self.user32.GetSystemMetrics(self.SM_CYSMICON)
            icon = self.user32.LoadImageW(
                None, str(TRAY_ICON_FILE), self.IMAGE_ICON,
                width, height, self.LR_LOADFROMFILE,
            )
            if icon:
                self.owns_icon = True
                return icon
            logging.warning("Life Link 中央服务托盘图标加载失败，改用系统默认图标")
        icon_resource = ctypes.cast(
            ctypes.c_void_p(self.IDI_INFORMATION), wintypes.LPCWSTR,
        )
        return self.user32.LoadIconW(None, icon_resource)

    def _release_icon(self) -> None:
        if self.owns_icon:
            self.user32.DestroyIcon(self.icon)
            self.owns_icon = False

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == self.CALLBACK_MESSAGE:
            if lparam == self.WM_LBUTTONUP:
                self.commands.put("health")
                return 0
            if lparam in {self.WM_RBUTTONUP, self.WM_CONTEXTMENU}:
                self.show_menu()
                return 0
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def show_menu(self) -> None:
        menu = self.user32.CreatePopupMenu()
        if not menu:
            return
        try:
            status = "中央服务：运行中" if self.app.health_is_ready() else "中央服务：离线"
            self.user32.AppendMenuW(menu, self.MF_STRING | self.MF_GRAYED, 0, status)
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_OPEN_HEALTH, "打开服务状态")
            startup_state = central_windows_startup.status()
            startup_label = (
                "✓ 登录后自动启动（点击关闭）"
                if startup_state["enabled"]
                else ("Windows 已拦截启动（点击恢复）"
                      if startup_state["blocked_by_windows"]
                      else ("启动项缺失（点击修复）"
                            if startup_state["state"] == "missing"
                            else "启用登录后自动启动"))
            )
            self.user32.AppendMenuW(
                menu, self.MF_STRING, self.ID_TOGGLE_LOGIN_STARTUP, startup_label,
            )
            self.user32.AppendMenuW(
                menu,
                self.MF_STRING,
                self.ID_CREATE_INVITATION,
                "生成设备配对码",
            )
            self.user32.AppendMenuW(
                menu,
                self.MF_STRING,
                self.ID_CREATE_MCP_CONNECTION_PACKAGE,
                "生成 AI 配对包",
            )
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_RESTART, "重启中央服务")
            self.user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT, "退出中央服务")
            point = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            self.user32.SetForegroundWindow(self.hwnd)
            command = self.user32.TrackPopupMenu(
                menu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
                point.x, point.y, 0, self.hwnd, None,
            )
            self.user32.PostMessageW(self.hwnd, self.WM_NULL, 0, 0)
            if command == self.ID_OPEN_HEALTH:
                self.commands.put("health")
            elif command == self.ID_TOGGLE_LOGIN_STARTUP:
                self.commands.put("toggle_login_startup")
            elif command == self.ID_CREATE_INVITATION:
                self.commands.put("invitation")
            elif command == self.ID_CREATE_MCP_CONNECTION_PACKAGE:
                self.commands.put("mcp_connection_package")
            elif command == self.ID_RESTART:
                self.commands.put("restart")
            elif command == self.ID_EXIT:
                self.commands.put("exit")
        finally:
            self.user32.DestroyMenu(menu)

    def pump_messages(self) -> None:
        if self.closed:
            return
        message = wintypes.MSG()
        while self.user32.PeekMessageW(
            ctypes.byref(message), self.hwnd, 0, 0, self.PM_REMOVE,
        ):
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            if command == "health":
                webbrowser.open(self.app.health_url)
            elif command == "toggle_login_startup":
                self.app.toggle_login_startup()
            elif command == "invitation":
                self.app.generate_invitation()
            elif command == "mcp_connection_package":
                self.app.generate_mcp_connection_package()
            elif command == "restart":
                self.app.restart_server()
            elif command == "exit":
                self.app.exit_application()
        if not self.closed:
            self.root.after(50, self.pump_messages)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.notify_data))
        self.user32.DestroyWindow(self.hwnd)
        self.user32.UnregisterClassW(self.class_name, self.hinstance)
        self._release_icon()


class CentralServerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.process: subprocess.Popen[bytes] | None = None
        self.log_handle = None
        self.mutex_handle: int | None = None
        self.tray: CentralServerTray | None = None
        self.quitting = False
        self.failures = 0
        self.server_port = DEFAULT_SERVER_PORT

    @property
    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1/health"

    def health_is_ready(self) -> bool:
        return health_is_ready(port=self.server_port)

    def acquire_single_instance(self) -> bool:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        return bool(self.mutex_handle) and kernel32.GetLastError() != 183

    def start_server(self) -> None:
        config_path = ensure_server_configuration()
        config = CentralConfig.from_environment({
            **os.environ,
            "LIFE_RADIO_CENTRAL_CONFIG": str(config_path),
        })
        self.server_port = config.port
        if self.health_is_ready():
            if self.process is not None and self.process.poll() is None:
                return
            raise RuntimeError(
                f"{self.server_port} 端口已有中央服务运行，但它不属于当前服务端托盘。"
                "请先退出旧的组合启动实例，再重新启动独立服务端。"
            )
        log_dir = default_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_handle = (log_dir / "central_server.log").open("ab", buffering=0)
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT), "run", "--config", str(config_path)],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            creationflags=WINDOWS_NO_WINDOW,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"中央服务启动失败，退出码 {self.process.returncode}")
            if self.health_is_ready():
                logging.info("central server started on %s:%s", config.host, config.port)
                return
            time.sleep(0.2)
        raise RuntimeError("中央服务未能在 20 秒内启动")

    def stop_server(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    def restart_server(self) -> None:
        self.stop_server()
        try:
            self.start_server()
            messagebox.showinfo("Life Link 中央服务", "中央服务已重新启动。")
        except Exception as error:
            messagebox.showerror("Life Link 中央服务", str(error))

    def generate_invitation(self) -> None:
        """Create one standard dashboard invitation without exposing it to logs."""
        try:
            created = create_client_invitation(
                config_path=ensure_server_configuration(),
                endpoint_path=default_endpoint_path(),
            )
            copied = copy_to_clipboard(created.code)
        except Exception as error:
            logging.exception("central invitation creation failed")
            messagebox.showerror("生成设备配对码失败", str(error))
            return
        if copied:
            messagebox.showinfo(
                "设备配对码已生成",
                "统一设备配对码已复制到剪贴板。\n"
                "它可由一台 PC 或手机领取一次，并将在 24 小时后过期。",
            )
        else:
            messagebox.showwarning(
                "设备配对码已生成",
                "配对码已经生成，但未能复制到剪贴板。请重新生成，"
                "或使用 central-server/maintenance/create_invitation.bat。",
            )

    def toggle_login_startup(self) -> None:
        try:
            startup_state = central_windows_startup.status()
            enabled = bool(startup_state["enabled"])
            if enabled:
                if not messagebox.askyesno(
                    "关闭登录后启动",
                    "不再在你登录 Windows 后自动启动 Life Link 中央服务吗？",
                ):
                    return
                central_windows_startup.set_enabled(False)
                messagebox.showinfo("Life Link 中央服务", "已关闭登录后自动启动。")
            else:
                if not messagebox.askyesno(
                    "恢复登录后启动" if startup_state["blocked_by_windows"] else "启用登录后启动",
                    (("恢复 Life Link 的 Windows 启动许可，并在你登录 Windows 后于后台启动中央服务吗？\n"
                      if startup_state["blocked_by_windows"]
                      else ("重新创建已缺失的 Life Link 中央服务启动项吗？\n"
                            if startup_state["state"] == "missing"
                            else "允许 Life Link 在你登录 Windows 后于后台启动中央服务吗？\n"))
                     + "它不会要求管理员权限，可随时从此菜单关闭。"),
                ):
                    return
                central_windows_startup.set_enabled(True)
                messagebox.showinfo("Life Link 中央服务", "已启用登录后自动启动。")
        except (OSError, RuntimeError) as error:
            logging.exception("central login startup update failed")
            messagebox.showerror("登录后启动设置失败", str(error))

    def generate_mcp_connection_package(self) -> None:
        """Create the complete MCP connection bundle through the local PC service."""
        try:
            created = request_mcp_connection_package()
        except Exception as error:
            logging.exception("MCP connection package creation failed")
            messagebox.showerror(
                "生成 AI 配对包失败",
                f"请先启动 Life Link PC 客户端，再重试。\n\n{error}",
            )
            return
        messagebox.showinfo(
            "AI 配对包已生成",
            f"已生成 {created['filename']}，并已打开所在文件夹。\n"
            "将压缩包发送给 AI 来完成连接。\n"
            "此前由 Life Link 管理的旧连接包已经清理。",
        )

    def monitor(self) -> None:
        if self.quitting:
            return
        if self.health_is_ready():
            self.failures = 0
        else:
            self.failures += 1
            if self.failures >= 3:
                logging.warning("central server health check failed; restarting")
                self.stop_server()
                try:
                    self.start_server()
                    self.failures = 0
                except Exception:
                    logging.exception("central server automatic restart failed")
        self.root.after(5_000, self.monitor)

    def exit_application(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        if self.tray is not None:
            self.tray.close()
        self.stop_server()
        if self.mutex_handle:
            ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None
        self.root.quit()
        self.root.destroy()

    def run(self) -> int:
        if not self.acquire_single_instance():
            try:
                config_path = ensure_server_configuration()
                config = CentralConfig.from_environment({
                    **os.environ,
                    "LIFE_RADIO_CENTRAL_CONFIG": str(config_path),
                })
                self.server_port = config.port
            except Exception:
                pass
            webbrowser.open(self.health_url)
            self.root.destroy()
            return 0
        try:
            self.start_server()
            self.tray = CentralServerTray(self.root, self)
        except Exception as error:
            self.stop_server()
            messagebox.showerror("Life Link 中央服务启动失败", str(error))
            self.root.destroy()
            return 1
        self.root.after(5_000, self.monitor)
        self.root.mainloop()
        return 0


def main() -> int:
    if sys.platform != "win32":
        print("The central server tray currently supports Windows only.", file=sys.stderr)
        return 1
    configure_logging()
    try:
        central_windows_startup.ensure_default_enabled()
    except OSError:
        logging.exception("central login startup entry refresh failed")
    return CentralServerApp().run()


if __name__ == "__main__":
    raise SystemExit(main())
