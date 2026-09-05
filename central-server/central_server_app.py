#!/usr/bin/env python3
"""Windows tray host for the independent Life Link central server.

The central process owns all management behaviour. This small Windows-only
host owns at most one child process and exposes only lifecycle controls.
"""

from __future__ import annotations

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import secrets
import subprocess
import sys
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from central.bootstrap import ensure_server_configuration
from central.config import CentralConfig, default_data_dir
import central_windows_startup


BASE_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = BASE_DIR / "central_server.py"
TRAY_ICON_FILE = BASE_DIR / "assets" / "life-link-server-tray.ico"
DEFAULT_SERVER_PORT = 8091
MANAGEMENT_URL = "http://127.0.0.1:8092"
MANAGEMENT_STATUS_URL = f"{MANAGEMENT_URL}/api/status"
MANAGEMENT_SHUTDOWN_URL = f"{MANAGEMENT_URL}/api/shutdown"
MUTEX_NAME = "Local\\LifeRadioCentralServer"
WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ERROR_ALREADY_EXISTS = 183


def configure_logging() -> None:
    log_dir = default_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "central_server_app.log", maxBytes=2_000_000,
        backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def management_is_ready(timeout: float = 1.0, *, opener=None) -> bool:
    """Accept only the intended loopback management service."""
    client = opener or build_opener(ProxyHandler({}))
    try:
        with client.open(MANAGEMENT_STATUS_URL, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("role") == "life-link-central-management"
        )
    except Exception:
        return False


def request_managed_shutdown(token: str, timeout: float = 3.0, *, opener=None) -> bool:
    """Ask the owned child to exit without exposing its capability elsewhere."""
    client = opener or build_opener(ProxyHandler({}))
    request = Request(
        MANAGEMENT_SHUTDOWN_URL, data=b"", method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with client.open(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


if sys.platform == "win32":
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
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
    WM_TIMER = 0x0113
    NIM_ADD = 0
    NIM_DELETE = 2
    NIF_MESSAGE = 1
    NIF_ICON = 2
    NIF_TIP = 4
    MF_STRING = 0
    TPM_RIGHTBUTTON = 2
    TPM_RETURNCMD = 0x100
    ID_OPEN_WEBUI = 2001
    ID_RESTART = 2002
    ID_EXIT = 2003
    IDI_INFORMATION = 32516
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    SM_CXSMICON = 49
    SM_CYSMICON = 50
    MONITOR_TIMER_ID = 1

    def __init__(self, app: "CentralServerApp") -> None:
        self.app = app
        self.commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.closed = False
        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32
        self.kernel32 = ctypes.windll.kernel32
        self._configure_api()
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
        if not self.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self.notify_data)):
            self.close()
            raise ctypes.WinError()
        self.user32.SetTimer(self.hwnd, self.MONITOR_TIMER_ID, 1000, None)

    def _configure_api(self) -> None:
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
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
        self.user32.DestroyIcon.argtypes = [wintypes.HANDLE]
        self.user32.DestroyIcon.restype = wintypes.BOOL
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.SetTimer.argtypes = [
            wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID,
        ]
        self.user32.SetTimer.restype = ctypes.c_size_t
        self.user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
        self.user32.KillTimer.restype = wintypes.BOOL
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
        self.user32.AppendMenuW.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
        ]
        self.user32.AppendMenuW.restype = wintypes.BOOL
        self.user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self.user32.DestroyMenu.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
        ]
        self.user32.TrackPopupMenu.restype = wintypes.UINT
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
        ]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = LRESULT
        self.user32.PostQuitMessage.argtypes = [ctypes.c_int]
        self.user32.PostQuitMessage.restype = None
        self.shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW),
        ]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    def _load_tray_icon(self) -> wintypes.HANDLE:
        if TRAY_ICON_FILE.is_file():
            icon = self.user32.LoadImageW(
                None, str(TRAY_ICON_FILE), self.IMAGE_ICON,
                self.user32.GetSystemMetrics(self.SM_CXSMICON),
                self.user32.GetSystemMetrics(self.SM_CYSMICON), self.LR_LOADFROMFILE,
            )
            if icon:
                self.owns_icon = True
                return icon
        return self.user32.LoadIconW(
            None, ctypes.cast(ctypes.c_void_p(self.IDI_INFORMATION), wintypes.LPCWSTR),
        )

    def _release_icon(self) -> None:
        if self.owns_icon:
            self.user32.DestroyIcon(self.icon)
            self.owns_icon = False

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == self.CALLBACK_MESSAGE:
            if lparam == self.WM_LBUTTONUP:
                self.commands.put("open")
                return 0
            if lparam in {self.WM_RBUTTONUP, self.WM_CONTEXTMENU}:
                self.show_menu()
                return 0
        if message == self.WM_TIMER and wparam == self.MONITOR_TIMER_ID:
            self.app.monitor()
            return 0
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def show_menu(self) -> None:
        menu = self.user32.CreatePopupMenu()
        if not menu:
            return
        try:
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_OPEN_WEBUI, "打开 WebUI")
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_RESTART, "重启服务器")
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT, "关闭服务器")
            point = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            self.user32.SetForegroundWindow(self.hwnd)
            command = self.user32.TrackPopupMenu(
                menu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
                point.x, point.y, 0, self.hwnd, None,
            )
            self.user32.PostMessageW(self.hwnd, self.WM_NULL, 0, 0)
            if command == self.ID_OPEN_WEBUI:
                self.commands.put("open")
            elif command == self.ID_RESTART:
                self.commands.put("restart")
            elif command == self.ID_EXIT:
                self.commands.put("exit")
        finally:
            self.user32.DestroyMenu(menu)

    def run(self) -> None:
        message = wintypes.MSG()
        while not self.closed and self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            while True:
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    break
                if command == "open":
                    self.app.open_webui()
                elif command == "restart":
                    self.app.restart_server()
                elif command == "exit":
                    self.app.exit_application()
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if getattr(self, "hwnd", None):
            self.user32.KillTimer(self.hwnd, self.MONITOR_TIMER_ID)
            self.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.notify_data))
            self.user32.DestroyWindow(self.hwnd)
            self.user32.UnregisterClassW(self.class_name, self.hinstance)
        self._release_icon()
        self.user32.PostQuitMessage(0)


class CentralServerApp:
    RESTART_DELAYS_SECONDS = (2, 5, 10)

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.log_handle = None
        self.mutex_handle: int | None = None
        self.tray: CentralServerTray | None = None
        self.quitting = False
        self.server_port = DEFAULT_SERVER_PORT
        self.management_token: str | None = None
        self.restart_attempts = 0
        self.next_restart_at: float | None = None
        self.reused_existing_server = False

    @property
    def management_url(self) -> str:
        return MANAGEMENT_URL

    def management_is_ready(self) -> bool:
        return management_is_ready()

    def acquire_single_instance(self) -> bool:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        self.mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        return bool(self.mutex_handle) and kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    def _show_message(self, title: str, text: str, *, error: bool = False) -> None:
        user32 = ctypes.windll.user32
        user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)

    def _load_config(self) -> Path:
        config_path = ensure_server_configuration()
        config = CentralConfig.from_environment({
            **os.environ, "LIFE_RADIO_CENTRAL_CONFIG": str(config_path),
        })
        self.server_port = config.port
        return config_path

    def start_server(self) -> None:
        config_path = self._load_config()
        if self.management_is_ready():
            self.reused_existing_server = True
            return
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("中央服务子进程仍在运行，但管理入口尚未就绪。")
        log_dir = default_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_handle = (log_dir / "central_server.log").open("ab", buffering=0)
        self.management_token = secrets.token_urlsafe(48)
        environment = dict(os.environ)
        environment["LIFE_LINK_MANAGEMENT_TOKEN"] = self.management_token
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT), "run", "--config", str(config_path)],
            cwd=str(BASE_DIR), stdin=subprocess.DEVNULL, stdout=self.log_handle,
            stderr=subprocess.STDOUT, creationflags=WINDOWS_NO_WINDOW, env=environment,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"中央服务启动失败，退出码 {self.process.returncode}")
            if self.management_is_ready():
                logging.info("central management server started on %s", MANAGEMENT_URL)
                return
            time.sleep(0.2)
        raise RuntimeError("中央服务未能在 20 秒内启动管理 WebUI")

    def stop_server(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            if self.management_token:
                request_managed_shutdown(self.management_token)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.process = None
        self.management_token = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    def open_webui(self) -> None:
        if self.management_is_ready():
            webbrowser.open(self.management_url)
        else:
            self._show_message("Life Link 中央服务", "服务尚未就绪，无法打开 WebUI。", error=True)

    def restart_server(self) -> None:
        if self.reused_existing_server and self.process is None:
            self._show_message(
                "Life Link 中央服务",
                "当前中央服务不是由此托盘启动，不能安全重启。请先关闭原有服务实例。",
                error=True,
            )
            return
        self.restart_attempts = 0
        self.next_restart_at = None
        self.stop_server()
        self.reused_existing_server = False
        try:
            self.start_server()
            self._show_message("Life Link 中央服务", "中央服务已重新启动。")
        except Exception as error:
            logging.exception("central server restart failed")
            self._show_message("Life Link 中央服务", str(error), error=True)

    def monitor(self) -> None:
        if self.quitting or self.reused_existing_server:
            return
        if self.process is not None and self.process.poll() is not None:
            self.process = None
            if self.log_handle is not None:
                self.log_handle.close()
                self.log_handle = None
            self.management_token = None
            if self.restart_attempts >= len(self.RESTART_DELAYS_SECONDS):
                self._show_message("Life Link 中央服务", "服务器已多次异常退出，请从托盘手动重启。", error=True)
                return
            delay = self.RESTART_DELAYS_SECONDS[self.restart_attempts]
            self.restart_attempts += 1
            self.next_restart_at = time.monotonic() + delay
        if self.next_restart_at is not None and time.monotonic() >= self.next_restart_at:
            self.next_restart_at = None
            try:
                self.start_server()
            except Exception:
                logging.exception("central server automatic restart failed")
                if self.restart_attempts >= len(self.RESTART_DELAYS_SECONDS):
                    self._show_message("Life Link 中央服务", "服务器未能自动恢复，请从托盘手动重启。", error=True)
                else:
                    delay = self.RESTART_DELAYS_SECONDS[self.restart_attempts]
                    self.restart_attempts += 1
                    self.next_restart_at = time.monotonic() + delay

    def exit_application(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        if self.reused_existing_server and self.process is None:
            self._show_message(
                "Life Link 中央服务",
                "当前中央服务不是由此托盘启动；本次只关闭托盘，原有服务继续运行。",
            )
        self.stop_server()
        if self.tray is not None:
            self.tray.close()
        if self.mutex_handle:
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None

    def run(self) -> int:
        if not self.acquire_single_instance():
            if self.management_is_ready():
                webbrowser.open(MANAGEMENT_URL)
                return 0
            self._show_message(
                "Life Link 中央服务",
                "已有托盘程序正在运行，但管理 WebUI 尚未就绪。请稍后从托盘重试。",
                error=True,
            )
            return 1
        try:
            self.start_server()
            self.tray = CentralServerTray(self)
            self.tray.run()
            return 0
        except Exception as error:
            logging.exception("central tray startup failed")
            self.stop_server()
            self._show_message("Life Link 中央服务启动失败", str(error), error=True)
            return 1


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
