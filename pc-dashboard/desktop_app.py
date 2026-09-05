#!/usr/bin/env python3
"""Windows desktop shell for the Life Link PC sync service."""

from __future__ import annotations

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox
from typing import Callable
from urllib.request import ProxyHandler, Request, build_opener

import pc_windows_startup
from runtime_paths import is_frozen, resource_dir

from device_identity import default_client_data_dir

if sys.platform == "win32":
    import winsound


BASE_DIR = resource_dir()
SERVER_SCRIPT = BASE_DIR / "sync_server.py"
TRAY_ICON_FILE = BASE_DIR / "assets" / "life-link-client-tray.ico"
CLIENT_DATA_DIR = default_client_data_dir() / "data"
SETTINGS_FILE = CLIENT_DATA_DIR / "desktop_app_settings.json"
LOG_DIR = CLIENT_DATA_DIR / "logs"
DESKTOP_LOG_FILE = LOG_DIR / "desktop_app.log"
SERVER_LOG_FILE = LOG_DIR / "sync_server.log"
SERVER_LOG_MAX_BYTES = 5_000_000
SERVER_LOG_BACKUP_COUNT = 2
SYNC_SERVER_START_TIMEOUT_SECONDS = 60 if is_frozen() else 20


def rotate_server_log_if_needed() -> None:
    """Bound child-process output retained across PC client restarts."""
    if not SERVER_LOG_FILE.exists() or SERVER_LOG_FILE.stat().st_size <= SERVER_LOG_MAX_BYTES:
        return
    oldest = SERVER_LOG_FILE.with_name(f"{SERVER_LOG_FILE.name}.{SERVER_LOG_BACKUP_COUNT}")
    oldest.unlink(missing_ok=True)
    for index in range(SERVER_LOG_BACKUP_COUNT - 1, 0, -1):
        source = SERVER_LOG_FILE.with_name(f"{SERVER_LOG_FILE.name}.{index}")
        if source.exists():
            source.replace(SERVER_LOG_FILE.with_name(f"{SERVER_LOG_FILE.name}.{index + 1}"))
    with SERVER_LOG_FILE.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - SERVER_LOG_MAX_BYTES))
        tail = source.read()
    first = SERVER_LOG_FILE.with_name(f"{SERVER_LOG_FILE.name}.1")
    with first.open("wb") as destination:
        destination.write(tail)
    SERVER_LOG_FILE.unlink()


_DEFAULT_PORT = int(os.environ.get("LIFE_RADIO_PORT") or 8090)
DASHBOARD_URL = os.environ.get(
    "LIFE_RADIO_DASHBOARD_URL",
    f"http://127.0.0.1:{_DEFAULT_PORT}/",
)
LIVE_USAGE_URL = f"{DASHBOARD_URL}api/live-usage"
HEALTH_URL = f"{DASHBOARD_URL}v1/health"
CUSTOM_EVENT_URL = f"{DASHBOARD_URL}api/custom-events"
TIMELINE_EVENTS_URL = f"{DASHBOARD_URL}api/timeline-events"
SETTINGS_URL = f"{DASHBOARD_URL}api/settings"
REFRESH_MILLISECONDS = 2_000
TIMELINE_REFRESH_MILLISECONDS = 30_000
DAY_START_REFRESH_SECONDS = 30.0
TIMELINE_FETCH_FAILED = object()
TIMELINE_UNCHANGED = object()
SEDENTARY_POLL_MILLISECONDS = 1_000
MINIMUM_WIDTH = 320
MINIMUM_HEIGHT = 420


def business_day_timeline_url(start: datetime) -> str:
    """Use the same stable full-day resource key as the WebUI."""
    end = start + timedelta(days=1)
    return (
        f"{TIMELINE_EVENTS_URL}"
        f"?from={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&to={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )


def visible_timeline_events(
    events: object, now: datetime,
) -> list[dict[str, object]]:
    """Keep valid events that have actually occurred by *now*."""
    if not isinstance(events, list):
        return []
    visible: list[dict[str, object]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        raw = str(event.get("occurred_at") or "")
        try:
            occurred = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if occurred.tzinfo is not None and occurred.astimezone(timezone.utc) <= now:
            visible.append(event)
    return visible
WINDOW_ALPHA = 0.90
SEDENTARY_LIMIT_SECONDS = 60 * 60
AFK_TOLERANCE_SECONDS = 3 * 60
AFK_RESET_SECONDS = 5 * 60
SHANGHAI_TZ = timezone(timedelta(hours=8))


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        DESKTOP_LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    def log_uncaught(
        exc_type: type[BaseException], exc_value: BaseException, exc_traceback: object,
    ) -> None:
        logging.critical(
            "桌面程序未捕获异常", exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = log_uncaught

    def log_thread_error(args: threading.ExceptHookArgs) -> None:
        logging.error(
            "后台线程未捕获异常: %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = log_thread_error


class SedentaryTimer:
    """In-memory sedentary reminder state; every process start begins at zero."""

    def __init__(
        self,
        limit_seconds: int = SEDENTARY_LIMIT_SECONDS,
        afk_reset_seconds: int = AFK_RESET_SECONDS,
    ) -> None:
        self.limit_seconds = max(1, int(limit_seconds))
        self.afk_reset_seconds = max(1, int(afk_reset_seconds))
        self.active_seconds = 0.0
        self.afk_seconds = 0.0
        self.reminder_active = False
        self.next_notification_multiple = 1
        self.afk_reset_applied = False

    @property
    def online_progress(self) -> float:
        return min(1.0, self.active_seconds / self.limit_seconds)

    @property
    def afk_progress(self) -> float:
        return min(1.0, self.afk_seconds / self.afk_reset_seconds)

    def advance(
        self,
        activity_state: str,
        elapsed_seconds: float,
        observed_afk_seconds: float = 0,
    ) -> bool:
        """Advance state and return True when a system reminder is due."""
        elapsed = max(0.0, float(elapsed_seconds))
        if activity_state == "afk":
            self.afk_seconds = min(
                float(self.afk_reset_seconds),
                max(self.afk_seconds + elapsed, max(0.0, float(observed_afk_seconds))),
            )
            if self.afk_seconds >= self.afk_reset_seconds and not self.afk_reset_applied:
                self.active_seconds = 0.0
                self.reminder_active = False
                self.next_notification_multiple = 1
                self.afk_reset_applied = True
            return False
        if activity_state != "active":
            return False

        self.afk_seconds = 0.0
        self.afk_reset_applied = False
        self.active_seconds += elapsed
        notify = False
        while self.active_seconds >= self.next_notification_multiple * self.limit_seconds:
            self.next_notification_multiple += 1
            self.reminder_active = True
            notify = True
        return notify

    def acknowledge(self) -> None:
        self.active_seconds = 0.0
        self.reminder_active = False
        self.next_notification_multiple = 1


def windows_idle_seconds() -> float:
    """Return seconds since the last keyboard or mouse input in this session."""
    if sys.platform != "win32":
        raise OSError("Windows input detection is only available on Windows")

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD),
        ]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError()

    # GetLastInputInfo and GetTickCount both use wrapping 32-bit millisecond
    # ticks. DWORD subtraction keeps the result correct across the wrap.
    current_tick = int(kernel32.GetTickCount())
    elapsed_milliseconds = (current_tick - int(info.dwTime)) & 0xFFFFFFFF
    return elapsed_milliseconds / 1_000.0


class WindowsInputActivityDetector:
    """Classify local input with an AW-compatible grace period."""

    def __init__(
        self,
        tolerance_seconds: int = AFK_TOLERANCE_SECONDS,
        idle_reader: Callable[[], float] = windows_idle_seconds,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.tolerance_seconds = max(0.0, float(tolerance_seconds))
        self.idle_reader = idle_reader
        self.monotonic_clock = monotonic_clock
        self.started_at = self.monotonic_clock()

    def sample(self) -> tuple[str, float]:
        runtime_seconds = max(0.0, self.monotonic_clock() - self.started_at)
        raw_idle_seconds = max(0.0, float(self.idle_reader()))
        # A new Life Link process always starts its own timing from zero,
        # even if Windows had already been idle before the process launched.
        effective_idle_seconds = min(raw_idle_seconds, runtime_seconds)
        if effective_idle_seconds < self.tolerance_seconds:
            return "active", 0.0
        return "afk", effective_idle_seconds - self.tolerance_seconds


def load_desktop_settings() -> dict[str, object]:
    try:
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def save_desktop_settings(settings: dict[str, object]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_FILE)


def format_duration(seconds: object) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}时{minutes:02d}分{seconds:02d}秒"
    return f"{minutes:02d}分{seconds:02d}秒"


def format_compact_duration(seconds: object) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


def sedentary_status_text(timer: SedentaryTimer, activity_state: str) -> str:
    if activity_state == "afk":
        return f"AFK 时间 {format_compact_duration(timer.afk_seconds)}"
    return f"本轮在线 {format_compact_duration(timer.active_seconds)}"


class UsageStatusWindow:
    def __init__(
        self,
        root: tk.Tk,
        notify_break: Callable[[str, str], None] | None = None,
        open_dashboard: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.notify_break = notify_break or (lambda _title, _message: None)
        self.open_dashboard = open_dashboard or (lambda: None)
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.title("Life Link · 用时状态")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.overrideredirect(True)
        self.window.resizable(False, False)
        self.window.attributes("-alpha", WINDOW_ALPHA)
        self.settings = load_desktop_settings()
        self.sedentary_paused = bool(self.settings.get("sedentary_paused", False))
        self.topmost_value = tk.BooleanVar(value=bool(self.settings.get("topmost", False)))
        self.result_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self.fetch_in_progress = False
        self.refresh_job: str | None = None
        self.poll_job: str | None = None
        self.sedentary_job: str | None = None
        self.personal_time_job: str | None = None
        self.initial_position_applied = False
        self.minimized = False
        self.drag_origin: tuple[int, int, int, int] | None = None
        self.opener = build_opener(ProxyHandler({}))
        self.sedentary_timer = SedentaryTimer()
        self.input_activity_detector = WindowsInputActivityDetector()
        self.last_timer_tick: float | None = None
        self.current_activity_state = "unknown"
        self.online_progress_ratio = 0.0
        self.afk_progress_ratio = 0.0
        self.collapsed = False
        self.expanded_size: tuple[int, int] | None = None

        # 事件列表面板状态
        self.timeline_queue: queue.Queue[object] = queue.Queue()
        self.timeline_fetch_in_progress = False
        self.timeline_refresh_job: str | None = None
        self.timeline_snapshot: list[dict[str, object]] | None = None
        self.seen_event_ids: set[str] = set()
        self.new_event_ids: set[str] = set()  # 待悬浮褪去的新事件
        self.timeline_first_load = True
        self.day_start_hour: int = 0
        self.day_start_hour_loaded = False
        self.day_start_hour_loaded_at = 0.0

        self.normal_color = "#f8fafc"
        self.muted_color = "#94a3b8"
        self.accent_color = "#7dd3fc"
        self.warning_color = "#fb7185"
        self.sedentary_orange = "#f59e0b"
        self.afk_green = "#22c55e"
        self.personal_time_purple = "#a78bfa"
        self.background = "#0f172a"
        self.card_background = "#1e293b"
        self.card_border = "#334155"
        self.window.configure(background=self.background)

        shell = tk.Frame(self.window, background=self.background)
        shell.pack(fill="both", expand=True)
        titlebar = tk.Frame(shell, background=self.background, height=30)
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        tk.Label(
            titlebar, text="Life Link · 用时状态", anchor="w",
            background=self.background, foreground=self.normal_color,
            font=("Microsoft YaHei UI", 9, "bold"), padx=10,
        ).pack(side="left", fill="y")
        close_button = self._title_button(titlebar, "×", self.hide)
        close_button.pack(side="right", fill="y")
        minimize_button = self._title_button(titlebar, "—", self.minimize)
        minimize_button.pack(side="right", fill="y")
        self.collapse_button = self._title_button(
            titlebar, "▼", self.toggle_collapsed,
        )
        self.collapse_button.pack(side="right", fill="y")
        # 顶部“置顶”勾选框：位于标题与折叠三角之间
        self.title_topmost_check = tk.Checkbutton(
            titlebar, text="置顶", variable=self.topmost_value,
            command=self.apply_topmost, background=self.background,
            foreground=self.normal_color, activebackground=self.background,
            activeforeground=self.normal_color, selectcolor=self.card_background,
            font=("Microsoft YaHei UI", 8), borderwidth=0, padx=4,
        )
        self.title_topmost_check.pack(side="left", fill="y")

        self.outer = tk.Frame(shell, background=self.background, padx=10, pady=8)
        self.outer.pack(fill="both", expand=True)
        self.value_labels: dict[str, tk.Label] = {}

        # 用量区精简为并排两卡：左卡=本机应用用时(今日)，右卡=当前应用。
        self.totals_row = tk.Frame(self.outer, background=self.background)
        self.totals_row.pack(fill="x")
        self.totals_row.columnconfigure(0, weight=1, uniform="totals")
        self.totals_row.columnconfigure(1, weight=1, uniform="totals")

        app_card = tk.Frame(
            self.totals_row, background=self.card_background,
            highlightbackground=self.card_border, highlightthickness=1,
            padx=9, pady=6,
        )
        app_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        tk.Label(
            app_card, text="本机应用用时", anchor="w", background=self.card_background,
            foreground=self.accent_color, font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(fill="x", pady=(0, 3))
        today_row = tk.Frame(app_card, background=self.card_background)
        today_row.pack(fill="x", pady=1)
        tk.Label(
            today_row, text="今日", anchor="w", background=self.card_background,
            foreground=self.muted_color, font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        today_value = tk.Label(
            today_row, text="00分00秒", anchor="e", background=self.card_background,
            foreground=self.normal_color, font=("Microsoft YaHei UI", 9, "bold"),
        )
        today_value.pack(side="right")
        self.value_labels["today_app"] = today_value

        current_card = tk.Frame(
            self.totals_row, background=self.card_background,
            highlightbackground=self.card_border, highlightthickness=1,
            padx=9, pady=6,
        )
        current_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        tk.Label(
            current_card, text="当前应用", anchor="w", background=self.card_background,
            foreground=self.muted_color, font=("Microsoft YaHei UI", 8),
        ).pack(fill="x", pady=(0, 3))
        current_app_value = tk.Label(
            current_card, text="加载中…", anchor="w", background=self.card_background,
            foreground=self.normal_color, font=("Microsoft YaHei UI", 10, "bold"),
            wraplength=130, justify="left",
        )
        current_app_value.pack(fill="x")
        self.value_labels["current_app"] = current_app_value

        self.sedentary_card = tk.Frame(
            self.outer, background=self.card_background,
            highlightbackground=self.card_border, highlightthickness=1,
            padx=9, pady=7,
        )
        self.sedentary_card.pack(fill="x", pady=(5, 0))
        timer_row = tk.Frame(self.sedentary_card, background=self.card_background)
        timer_row.pack(fill="x")
        tk.Label(
            timer_row, text="久坐提醒", anchor="w", background=self.card_background,
            foreground=self.sedentary_orange, font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        self.sedentary_time_label = tk.Label(
            timer_row, text="本轮在线 00:00", anchor="w",
            background=self.card_background, foreground=self.normal_color,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.sedentary_time_label.pack(side="left", padx=(7, 0))
        self.active_leave_button = tk.Button(
            timer_row, text="主动离开", command=self.active_leave,
            background=self.card_border, foreground=self.normal_color,
            activebackground="#475569", activeforeground=self.normal_color,
            relief="flat", borderwidth=0, padx=2, pady=0,
            font=("Microsoft YaHei UI", 9),
        )
        self.active_leave_button.pack(side="right")
        self.sedentary_pause_button = tk.Button(
            timer_row, text="启用" if self.sedentary_paused else "停用",
            command=self.toggle_sedentary_pause,
            background=self.card_border, foreground=self.normal_color,
            activebackground="#475569", activeforeground=self.normal_color,
            relief="flat", borderwidth=0, padx=2, pady=0,
            font=("Microsoft YaHei UI", 9),
        )
        self.sedentary_pause_button.pack(side="left", padx=(12, 0))
        self.ack_slot = tk.Frame(
            timer_row, background=self.card_background, width=40, height=20,
        )
        self.ack_slot.pack(side="right")
        self.ack_slot.pack_propagate(False)
        self.ack_button = tk.Button(
            self.ack_slot, text="收到", command=self.acknowledge_reminder,
            background=self.sedentary_orange, foreground="#111827",
            activebackground="#fbbf24", activeforeground="#111827",
            relief="flat", borderwidth=0, font=("Microsoft YaHei UI", 8, "bold"),
        )

        self.progress_row = tk.Frame(
            self.sedentary_card, background=self.card_background,
        )
        self.progress_row.pack(fill="x", pady=(7, 0))
        self.progress_row.columnconfigure(0, weight=1)
        self.online_canvas = tk.Canvas(
            self.progress_row, width=1, height=10, background=self.card_border,
            highlightthickness=0, borderwidth=0,
        )
        self.online_canvas.grid(row=0, column=0, sticky="ew")
        self.afk_canvas = tk.Canvas(
            self.progress_row, width=1, height=6, background=self.card_border,
            highlightthickness=0, borderwidth=0,
        )
        self.afk_canvas.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.online_canvas.bind("<Configure>", lambda _event: self.redraw_progress())
        self.afk_canvas.bind("<Configure>", lambda _event: self.redraw_progress())
        self.dashboard_button = tk.Button(
            self.sedentary_card, text="打开 Dashboard", command=self.open_dashboard,
            background=self.card_border, foreground=self.normal_color,
            activebackground="#475569", activeforeground=self.normal_color,
            relief="flat", borderwidth=0, padx=6, pady=3,
            font=("Microsoft YaHei UI", 9),
        )
        self.dashboard_button.pack(fill="x", pady=(7, 0))

        # ---- 个人时光倒计时 ----
        self.personal_time_card = tk.Frame(
            self.outer, background=self.card_background,
            highlightbackground=self.card_border, highlightthickness=1,
            padx=9, pady=7,
        )
        self.personal_time_card.pack(fill="x", pady=(5, 0))
        personal_row = tk.Frame(self.personal_time_card, background=self.card_background)
        personal_row.pack(fill="x")
        tk.Label(
            personal_row, text="个人时光", anchor="w", background=self.card_background,
            foreground=self.personal_time_purple, font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        self.personal_time_label = tk.Label(
            personal_row, text="剩余 --:--:--", anchor="w",
            background=self.card_background, foreground=self.normal_color,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.personal_time_label.pack(side="left", padx=(7, 0))
        self.personal_progress_canvas = tk.Canvas(
            self.personal_time_card, width=1, height=8, background=self.card_border,
            highlightthickness=0, borderwidth=0,
        )
        self.personal_progress_canvas.pack(fill="x", pady=(7, 0))
        self.personal_progress_canvas.bind("<Configure>", lambda _event: self.redraw_personal_time())

        # ---- 事件列表面板（Canvas 滚动，每个事件独立带边框卡片）----
        self.events_card = tk.Frame(
            self.outer, background=self.card_background,
            highlightbackground=self.card_border, highlightthickness=1,
            padx=6, pady=6,
        )
        self.events_card.pack(fill="both", expand=True, pady=(5, 0))
        self.events_canvas = tk.Canvas(
            self.events_card, highlightthickness=0, borderwidth=0,
            background=self.card_background, yscrollincrement=1,
        )
        self.events_scroll = None
        self.events_canvas.pack(side="left", fill="both", expand=True)
        self.events_inner = tk.Frame(self.events_canvas, background=self.card_background)
        self.events_inner_window_id = self.events_canvas.create_window(
            (0, 0), window=self.events_inner, anchor="nw",
        )
        self.events_canvas.bind(
            "<Configure>", lambda _e: self._on_events_canvas_resize()
        )
        self.events_inner.bind(
            "<Configure>", lambda _e: self._on_events_content_change()
        )
        # 鼠标滚轮 + 拖拽（Enter/Leave 绑定，覆盖 Canvas 及其子控件）
        self.events_canvas.bind("<Enter>", self._on_events_enter)
        self.events_canvas.bind("<Leave>", self._on_events_leave)
        # 新事件配色常量
        self.event_border_system = "#8B95A5"
        self.event_border_normal = "#3B82F6"
        self.event_border_high = "#F59E0B"
        self.event_new_bg = "#3a2f13"
        self.event_new_fg = "#ffe9b0"
        # 当前渲染的事件卡片引用（timeline_event_id -> Frame）
        self.event_cards: dict[str, dict[str, object]] = {}
        self._events_drag_start: float | None = None
        self._events_drag_remainder: float = 0.0
        # 初始占位
        tk.Label(
            self.events_inner, text="加载中…", anchor="w",
            background=self.card_background, foreground=self.muted_color,
            font=("Microsoft YaHei UI", 8),
        ).pack(fill="x")

        self.controls = tk.Frame(self.outer, background=self.background)
        self.controls.pack(fill="x", pady=(4, 0))
        topmost_check = tk.Checkbutton(
            self.controls, text="置于最上层", variable=self.topmost_value,
            command=self.apply_topmost, background=self.background,
            foreground=self.muted_color, activebackground=self.background,
            activeforeground=self.normal_color, selectcolor=self.card_background,
            font=("Microsoft YaHei UI", 8), borderwidth=0,
        )
        topmost_check.pack(side="left")

        self.functional_widgets = {
            close_button, minimize_button, self.collapse_button, topmost_check,
            self.title_topmost_check,
            self.active_leave_button, self.sedentary_pause_button,
            self.ack_slot, self.ack_button, self.dashboard_button,
            self.events_canvas, self.events_inner,
        }
        self._bind_draggable_tree(shell)
        self.poll_job = self.root.after(100, self.poll_results)
        self.refresh_job = self.root.after(0, self.refresh_now)
        self.sedentary_job = self.root.after(0, self.update_sedentary_timer)
        self.personal_time_job = self.root.after(0, self.update_personal_time)
        self.timeline_refresh_job = self.root.after(0, self.refresh_timeline)

    def _title_button(
        self, parent: tk.Widget, text: str, command: Callable[[], None],
    ) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, width=4,
            background=self.background, foreground=self.muted_color,
            activebackground=self.card_border, activeforeground=self.normal_color,
            relief="flat", borderwidth=0, font=("Segoe UI", 10),
        )

    def _bind_draggable_tree(self, widget: tk.Widget) -> None:
        if widget in self.functional_widgets:
            return
        widget.bind("<ButtonPress-1>", self._start_drag, add="+")
        widget.bind("<B1-Motion>", self._drag_window, add="+")
        widget.bind("<ButtonRelease-1>", self._stop_drag, add="+")
        for child in widget.winfo_children():
            self._bind_draggable_tree(child)

    def _start_drag(self, event: tk.Event) -> None:
        self.drag_origin = (
            event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y(),
        )

    def _drag_window(self, event: tk.Event) -> None:
        if self.drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self.drag_origin
        x = window_x + event.x_root - start_x
        y = window_y + event.y_root - start_y
        self.window.geometry(f"+{x}+{y}")

    def _stop_drag(self, _event: tk.Event) -> None:
        self.drag_origin = None

    def toggle_collapsed(self) -> None:
        self.window.update_idletasks()
        x = self.window.winfo_x()
        y = self.window.winfo_y()
        if not self.collapsed:
            self.expanded_size = (
                self.window.winfo_width(),
                self.window.winfo_height(),
            )
            self.totals_row.pack_forget()
            self.personal_time_card.pack_forget()
            self.events_card.pack_forget()
            self.controls.pack_forget()
            self.collapsed = True
            self.collapse_button.configure(text="▲")
            self.window.update_idletasks()
            width = max(MINIMUM_WIDTH, self.window.winfo_width())
            height = self.window.winfo_reqheight()
            self.window.geometry(f"{width}x{height}+{x}+{y}")
        else:
            self.sedentary_card.pack_forget()
            self.personal_time_card.pack_forget()
            self.totals_row.pack(fill="x")
            self.sedentary_card.pack(fill="x", pady=(5, 0))
            # 个人时光卡片在时间范围内重新显示
            self.personal_time_card.pack(fill="x", pady=(5, 0))
            self.events_card.pack(fill="x", pady=(5, 0))
            self.controls.pack(fill="x", pady=(4, 0))
            self.collapsed = False
            self.collapse_button.configure(text="▼")
            if self.expanded_size:
                width, height = self.expanded_size
                self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.update_idletasks()
        self.redraw_progress()

    def active_leave(self) -> None:
        self.sedentary_timer.acknowledge()
        self.render_sedentary_timer()

    def toggle_sedentary_pause(self) -> None:
        self.sedentary_paused = not self.sedentary_paused
        self.settings["sedentary_paused"] = self.sedentary_paused
        save_desktop_settings(self.settings)
        if self.sedentary_paused:
            self.sedentary_timer.acknowledge()
        self.sedentary_pause_button.configure(
            text="启用" if self.sedentary_paused else "停用"
        )
        self.render_sedentary_timer()

    # ---- 事件列表面板 ----

    _EVENT_ICONS = {
        "wish.scheduled_reminder": "⏰",
        "wish.created": "🎯",
        "wish.cancelled": "🎯",
        "wish.result_revised": "🎯",
        "wish.period_completed": "🎯",
        "system.device_usage_milestone": "📊",
        "system.blacklist_usage_milestone": "🚫",
        "system.location_stay_milestone": "📍",
        "system.activity_duration_milestone": "🚶",
        "system.late_online_check": "🌙",
        "sedentary.reminder_triggered": "🪑",
        "report.morning": "📅",
        "report.evening": "📅",
        "report.periodic": "📅",
        "application.started": "⚙️",
    }

    def _event_border_color(self, event: dict[str, object]) -> str:
        if event.get("importance") == "high":
            return self.event_border_high
        if event.get("category") == "system":
            return self.event_border_system
        return self.event_border_normal

    def _event_icon(self, event: dict[str, object]) -> str:
        key = str(event.get("event_key") or "")
        return self._EVENT_ICONS.get(key, "📊")

    def _event_title(self, event: dict[str, object]) -> str:
        return str(event.get("title") or "未命名事件")

    def _event_time_hhmm(self, event: dict[str, object]) -> str:
        raw = str(event.get("occurred_at") or "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(SHANGHAI_TZ).strftime("%H:%M")
        except ValueError:
            return "--:--"

    def _on_events_canvas_resize(self) -> None:
        canvas_width = self.events_canvas.winfo_width()
        self.events_canvas.itemconfigure(
            self.events_inner_window_id, width=canvas_width
        )

    def _on_events_content_change(self) -> None:
        self.events_canvas.configure(
            scrollregion=self.events_canvas.bbox("all")
        )

    def _on_events_enter(self, _event: tk.Event) -> None:
        self.events_canvas.bind_all("<MouseWheel>", self._on_events_mousewheel)
        self.events_canvas.bind_all("<ButtonPress-1>", self._on_events_drag_start)
        self.events_canvas.bind_all("<B1-Motion>", self._on_events_drag_motion)

    def _on_events_leave(self, _event: tk.Event) -> None:
        self.events_canvas.unbind_all("<MouseWheel>")
        self.events_canvas.unbind_all("<ButtonPress-1>")
        self.events_canvas.unbind_all("<B1-Motion>")

    def _on_events_mousewheel(self, event: tk.Event) -> None:
        # yscrollincrement=1 后 1 unit = 1px；每格滚轮滚约 45px
        self.events_canvas.yview_scroll(int(-45 * (event.delta / 120)), "units")

    def _on_events_drag_start(self, event: tk.Event) -> None:
        self._events_drag_start = event.y_root
        self._events_drag_remainder = 0.0

    def _on_events_drag_motion(self, event: tk.Event) -> None:
        if self._events_drag_start is None:
            return
        raw = self._events_drag_start - event.y_root
        self._events_drag_start = event.y_root
        # 拖拽倍率 3.0（鼠标移 1px → 内容滚 3px），用浮点累加避免慢速拖动归零
        self._events_drag_remainder += raw * 3.0
        scroll_units = int(self._events_drag_remainder)
        if scroll_units != 0:
            self._events_drag_remainder -= scroll_units
            self.events_canvas.yview_scroll(scroll_units, "units")

    def refresh_timeline(self) -> None:
        if not self.timeline_fetch_in_progress:
            self.timeline_fetch_in_progress = True
            threading.Thread(
                target=self._fetch_timeline, daemon=True, name="life-radio-timeline",
            ).start()
        if self.timeline_refresh_job is not None:
            self.root.after_cancel(self.timeline_refresh_job)
        self.timeline_refresh_job = self.root.after(
            TIMELINE_REFRESH_MILLISECONDS, self.refresh_timeline,
        )

    def _load_day_start_hour(self) -> int:
        """Fetch and cache the shared business-day boundary hour from the local proxy."""
        if (
            self.day_start_hour_loaded
            and time.monotonic() - self.day_start_hour_loaded_at < DAY_START_REFRESH_SECONDS
        ):
            return self.day_start_hour
        try:
            with self.opener.open(SETTINGS_URL, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            hour = result.get("day_start_hour") if isinstance(result, dict) else None
            if isinstance(hour, int) and 0 <= hour <= 23:
                self.day_start_hour = hour
                self.day_start_hour_loaded = True
                self.day_start_hour_loaded_at = time.monotonic()
        except Exception as error:
            logging.warning("跨日设置读取失败，回退到 0 点: %s", error)
        return self.day_start_hour

    def _business_day_start_utc(self) -> datetime:
        """Return the UTC datetime of the current business day's start."""
        hour = self._load_day_start_hour()
        local_now = datetime.now(SHANGHAI_TZ)
        start = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if local_now < start:
            start -= timedelta(days=1)
        return start.astimezone(timezone.utc)

    def _fetch_timeline(self) -> None:
        start = self._business_day_start_utc()
        now = datetime.now(timezone.utc)
        url = business_day_timeline_url(start)
        try:
            with self.opener.open(url, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
            # The fixed full-business-day query is shared with WebUI caching.
            # Do not show scheduled/future events in the live desktop window.
            events = visible_timeline_events(
                result.get("events") if isinstance(result, dict) else None, now,
            )
        except Exception as error:
            logging.warning("事件时间线读取失败: %s", error)
            self.timeline_queue.put(TIMELINE_FETCH_FAILED)
            return
        if getattr(self, "timeline_snapshot", None) == events:
            self.timeline_queue.put(TIMELINE_UNCHANGED)
            return
        self.timeline_snapshot = events
        self.timeline_queue.put(events)

    def poll_results(self) -> None:
        newest: dict[str, object] | None = None
        try:
            while True:
                newest = self.result_queue.get_nowait()
        except queue.Empty:
            pass
        newest_timeline: object | None = None
        try:
            while True:
                newest_timeline = self.timeline_queue.get_nowait()
        except queue.Empty:
            pass
        if newest is not None:
            self.fetch_in_progress = False
            self.render(newest)
        if newest_timeline is not None:
            self.timeline_fetch_in_progress = False
        if isinstance(newest_timeline, list):
            try:
                self._render_timeline(newest_timeline)
            except Exception:
                logging.exception("事件时间线渲染失败")
        self.poll_job = self.root.after(100, self.poll_results)

    def _render_timeline(self, events: list[dict[str, object]]) -> None:
        ordered = sorted(
            (e for e in events if isinstance(e, dict)),
            key=lambda e: str(e.get("occurred_at") or ""),
            reverse=True,
        )
        current_ids = {
            str(e.get("timeline_event_id") or "") for e in ordered
        }
        if self.timeline_first_load:
            self.seen_event_ids = set(current_ids)
            self.new_event_ids = set()
            self.timeline_first_load = False
        else:
            fresh = current_ids - self.seen_event_ids
            self.new_event_ids |= fresh
            self.seen_event_ids = current_ids

        # 清空旧卡片
        for child in self.events_inner.winfo_children():
            child.destroy()
        self.event_cards = {}

        if not ordered:
            tk.Label(
                self.events_inner, text="  今日暂无事件", anchor="w",
                background=self.card_background, foreground=self.muted_color,
                font=("Microsoft YaHei UI", 8),
            ).pack(fill="x", padx=4, pady=8)
            self._on_events_content_change()
            return

        for event in ordered:
            self._create_event_card(event)

        self.events_canvas.yview_moveto(0)

    def _create_event_card(self, event: dict[str, object]) -> None:
        event_id = str(event.get("timeline_event_id") or "")
        border_color = self._event_border_color(event)
        icon = self._event_icon(event)
        time_text = self._event_time_hhmm(event)
        title = self._event_title(event)
        detail = str(event.get("detail") or "").strip()
        is_new = event_id in self.new_event_ids
        is_high = event.get("importance") == "high"

        bg = self.event_new_bg if is_new else self.card_background
        title_fg = self.event_new_fg if is_new else self.normal_color
        time_fg = self.event_new_fg if is_new else self.muted_color
        border_w = 2 if is_high else 1

        card = tk.Frame(
            self.events_inner,
            background=bg,
            highlightbackground=border_color,
            highlightthickness=border_w,
            padx=8, pady=5,
        )
        card.pack(fill="x", padx=2, pady=3)

        header = tk.Frame(card, background=bg)
        header.pack(fill="x")
        icon_label = tk.Label(
            header, text=f"{icon}", background=bg, foreground=title_fg,
            font=("Microsoft YaHei UI", 9),
        )
        icon_label.pack(side="left")
        time_label = tk.Label(
            header, text=time_text, background=bg, foreground=time_fg,
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        time_label.pack(side="left", padx=(4, 8))
        title_label = tk.Label(
            header, text=title + ("  ⭐" if is_high else ""),
            background=bg, foreground=title_fg,
            font=("Microsoft YaHei UI", 9, "bold"), anchor="w", justify="left",
            wraplength=200,
        )
        title_label.pack(side="left", fill="x", expand=True)

        detail_label = None
        if detail:
            first_line = detail.split("\n", 1)[0][:80]
            detail_fg = self.event_new_fg if is_new else self.muted_color
            detail_label = tk.Label(
                card, text=first_line, background=bg, foreground=detail_fg,
                font=("Microsoft YaHei UI", 8), anchor="w", justify="left",
                wraplength=230,
            )
            detail_label.pack(fill="x", pady=(2, 0))

        if event_id:
            self.event_cards[event_id] = {
                "card": card, "header": header,
                "icon_label": icon_label, "time_label": time_label,
                "title_label": title_label, "detail_label": detail_label,
            }
            if is_new:
                card.bind("<Enter>", lambda _e, eid=event_id: self._fade_event(eid))
                for child in card.winfo_children():
                    child.bind("<Enter>", lambda _e, eid=event_id: self._fade_event(eid))
                    for grandchild in child.winfo_children():
                        grandchild.bind(
                            "<Enter>", lambda _e, eid=event_id: self._fade_event(eid)
                        )

    def _fade_event(self, event_id: str) -> None:
        if event_id not in self.new_event_ids:
            return
        self.new_event_ids.discard(event_id)
        self._update_card_style(event_id)

    def _update_card_style(self, event_id: str) -> None:
        """Update a single event card from 'new' to 'normal' style without re-fetching."""
        info = self.event_cards.get(event_id)
        if info is None:
            return
        bg = self.card_background
        title_fg = self.normal_color
        time_fg = self.muted_color
        detail_fg = self.muted_color
        card = info["card"]
        header = info["header"]
        try:
            card.configure(background=bg)
            header.configure(background=bg)
            info["icon_label"].configure(background=bg, foreground=title_fg)
            info["time_label"].configure(background=bg, foreground=time_fg)
            info["title_label"].configure(background=bg, foreground=title_fg)
            if info.get("detail_label") is not None:
                info["detail_label"].configure(background=bg, foreground=detail_fg)
        except tk.TclError:
            pass

    def _rerender_current_timeline(self) -> None:
        if not self.timeline_fetch_in_progress:
            self.timeline_fetch_in_progress = True
            threading.Thread(
                target=self._fetch_timeline, daemon=True, name="life-radio-timeline",
            ).start()

    def apply_initial_geometry(self) -> None:
        if self.initial_position_applied:
            return
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        width = max(MINIMUM_WIDTH, int(round(screen_width * 0.15)))
        height = max(MINIMUM_HEIGHT, int(round(screen_height * 0.22)))
        x = max(0, screen_width - width - 24)
        y = max(0, screen_height - height - 72)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.initial_position_applied = True

    def show(self) -> None:
        self.apply_initial_geometry()
        if self.minimized:
            self.window.withdraw()
            self.window.overrideredirect(True)
            self.minimized = False
        self.window.deiconify()
        self.window.lift()
        self.root.after_idle(self._reapply_topmost)

    def hide(self) -> None:
        self.minimized = False
        self.window.withdraw()
        self.window.overrideredirect(True)

    def minimize(self) -> None:
        if not self.window.winfo_viewable():
            return
        self.minimized = True
        self.window.overrideredirect(False)
        self.window.update_idletasks()
        self._ensure_taskbar_style()
        self.window.bind("<Map>", self._restore_borderless_after_minimize, add="+")
        self.window.iconify()

    def _ensure_taskbar_style(self) -> None:
        if sys.platform != "win32":
            return
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        client_hwnd = self.window.winfo_id()
        wrapper_hwnd = user32.GetParent(client_hwnd)
        if not wrapper_hwnd:
            wrapper_hwnd = client_hwnd
        gwl_exstyle = -20
        ws_ex_appwindow = 0x00040000
        ws_ex_toolwindow = 0x00000080
        swp_flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        style = user32.GetWindowLongW(wrapper_hwnd, gwl_exstyle)
        user32.SetWindowLongW(
            wrapper_hwnd,
            gwl_exstyle,
            (style | ws_ex_appwindow) & ~ws_ex_toolwindow,
        )
        user32.SetWindowPos(wrapper_hwnd, None, 0, 0, 0, 0, swp_flags)

    def _restore_borderless_after_minimize(self, _event: tk.Event) -> None:
        if not self.minimized or self.window.state() == "iconic":
            return
        self.minimized = False
        self.window.unbind("<Map>")
        self.root.after_idle(self._restore_borderless)

    def _restore_borderless(self) -> None:
        if self.window.winfo_exists():
            self.window.overrideredirect(True)
            self.window.lift()
            self._reapply_topmost()

    def apply_topmost(self) -> None:
        self.settings["topmost"] = bool(self.topmost_value.get())
        save_desktop_settings(self.settings)
        self._reapply_topmost()

    def _reapply_topmost(self) -> None:
        if self.window.winfo_exists():
            self.window.attributes("-topmost", bool(self.topmost_value.get()))

    def refresh_now(self) -> None:
        if not self.fetch_in_progress:
            self.fetch_in_progress = True
            threading.Thread(
                target=self._fetch_snapshot, daemon=True, name="life-radio-live-usage",
            ).start()
        if self.refresh_job is not None:
            self.root.after_cancel(self.refresh_job)
        self.refresh_job = self.root.after(REFRESH_MILLISECONDS, self.refresh_now)

    def _fetch_snapshot(self) -> None:
        try:
            with self.opener.open(LIVE_USAGE_URL, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("实时状态响应格式错误")
        except Exception as error:
            logging.warning("实时状态读取失败: %s", error)
            result = {
                "status": "offline",
                "current_app": "服务未连接",
                "current_hour_app_seconds": 0,
                "today_app_seconds": 0,
                "current_site": "无",
                "current_hour_blacklist_seconds": 0,
                "today_blacklist_seconds": 0,
                "current_is_blacklisted": False,
                "activity_state": "unknown",
                "current_afk_seconds": 0,
                "error": str(error),
            }
        self.result_queue.put(result)

    def render(self, data: dict[str, object]) -> None:
        current_app = str(data.get("current_app") or "暂无活动")
        warning_reason = str(data.get("blacklist_reason") or "")
        if data.get("status") != "ok":
            current_app = str(data.get("current_app") or "ActivityWatch 未连接")
        self.value_labels["current_app"].configure(
            text=current_app,
            foreground=self.warning_color if warning_reason == "process" else self.normal_color,
        )
        self.value_labels["today_app"].configure(
            text=format_duration(data.get("today_app_seconds")),
            foreground=self.accent_color,
        )

    def update_sedentary_timer(self) -> None:
        now = time.monotonic()
        elapsed = 0.0 if self.last_timer_tick is None else min(6.0, now - self.last_timer_tick)
        self.last_timer_tick = now
        try:
            state, observed_afk_seconds = self.input_activity_detector.sample()
        except Exception as error:
            logging.warning("Windows 键鼠空闲状态读取失败: %s", error)
            state, observed_afk_seconds = "unknown", 0.0
        self.current_activity_state = state
        notify = False
        if not self.sedentary_paused:
            notify = self.sedentary_timer.advance(
                state,
                elapsed,
                observed_afk_seconds,
            )
        self.render_sedentary_timer()
        if notify:
            self.notify_break(
                "久坐提醒",
                f"本轮已在线 {format_duration(self.sedentary_timer.active_seconds)}，请起身活动。",
            )
        self.sedentary_job = self.root.after(
            SEDENTARY_POLL_MILLISECONDS,
            self.update_sedentary_timer,
        )

    PERSONAL_TIME_START_HOUR = 20
    PERSONAL_TIME_END_HOUR = 23

    def _personal_time_window(self) -> tuple[datetime, datetime, float] | None:
        """Return (start, end, total_seconds) for tonight's 20:00-23:00 in Shanghai TZ."""
        now = datetime.now(SHANGHAI_TZ)
        start = now.replace(hour=self.PERSONAL_TIME_START_HOUR, minute=0, second=0, microsecond=0)
        end = now.replace(hour=self.PERSONAL_TIME_END_HOUR, minute=0, second=0, microsecond=0)
        if now < start:
            return None  # 还没到晚上8点
        if now >= end:
            return None  # 已过晚上11点
        return start, end, (end - start).total_seconds()

    def update_personal_time(self) -> None:
        window = self._personal_time_window()
        if window is None:
            self.personal_time_card.pack_forget()
        else:
            start, end, total_seconds = window
            remaining = max(0, (end - datetime.now(SHANGHAI_TZ)).total_seconds())
            self.personal_time_remaining = remaining / total_seconds if total_seconds > 0 else 0
            if not self.personal_time_card.winfo_ismapped():
                self.personal_time_card.pack(
                    fill="x", pady=(5, 0),
                    before=self.events_card if self.events_card.winfo_ismapped() else None,
                )
            self.personal_time_label.configure(
                text=f"剩余 {format_duration(int(remaining))}",
            )
            self.redraw_personal_time()
        self.personal_time_job = self.root.after(1_000, self.update_personal_time)

    def redraw_personal_time(self) -> None:
        ratio = getattr(self, 'personal_time_remaining', 0)
        canvas = self.personal_progress_canvas
        width = max(1, canvas.winfo_width())
        canvas.delete("all")
        fill_width = int(width * ratio)
        if fill_width > 0:
            canvas.create_rectangle(
                0, 0, fill_width, canvas.winfo_height(),
                fill=self.personal_time_purple, outline="",
            )

    def render_sedentary_timer(self) -> None:
        timer = self.sedentary_timer
        self.sedentary_time_label.configure(
            text=sedentary_status_text(timer, self.current_activity_state),
            foreground=(
                self.afk_green
                if self.current_activity_state == "afk"
                else self.sedentary_orange
                if timer.reminder_active
                else self.normal_color
            ),
        )
        self.online_progress_ratio = timer.online_progress
        self.afk_progress_ratio = timer.afk_progress
        self.sedentary_card.configure(
            highlightbackground=self.sedentary_orange if timer.reminder_active else self.card_border,
        )
        if timer.reminder_active:
            self.ack_button.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.sedentary_pause_button.pack_forget()
        else:
            self.ack_button.place_forget()
            if not self.sedentary_pause_button.winfo_ismapped():
                self.sedentary_pause_button.pack(side="left", padx=(12, 0), after=self.sedentary_time_label)
        self.redraw_progress()

    def redraw_progress(self) -> None:
        for canvas, ratio, color in (
            (self.online_canvas, self.online_progress_ratio, self.sedentary_orange),
            (self.afk_canvas, self.afk_progress_ratio, self.afk_green),
        ):
            width = max(1, canvas.winfo_width())
            height = max(1, canvas.winfo_height())
            canvas.delete("progress")
            if ratio > 0:
                canvas.create_rectangle(
                    0, 0, int(width * min(1.0, ratio)), height,
                    fill=color, outline="", tags="progress",
                )

    def acknowledge_reminder(self) -> None:
        self.sedentary_timer.acknowledge()
        self.render_sedentary_timer()

    def destroy(self) -> None:
        if self.refresh_job is not None:
            try:
                self.root.after_cancel(self.refresh_job)
            except tk.TclError:
                pass
        if self.poll_job is not None:
            try:
                self.root.after_cancel(self.poll_job)
            except tk.TclError:
                pass
        if self.sedentary_job is not None:
            try:
                self.root.after_cancel(self.sedentary_job)
            except tk.TclError:
                pass
        if self.window.winfo_exists():
            self.window.destroy()


if sys.platform == "win32":
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HANDLE),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HANDLE),
        ]


class WindowsTrayIcon:
    WM_APP = 0x8000
    CALLBACK_MESSAGE = WM_APP + 17
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    WM_NULL = 0x0000
    PM_REMOVE = 0x0001
    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010
    NIIF_WARNING = 0x00000002
    MF_STRING = 0x00000000
    MF_CHECKED = 0x00000008
    MF_GRAYED = 0x00000001
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100
    ID_OPEN_DASHBOARD = 1001
    ID_OPEN_STATUS = 1002
    ID_TOGGLE_LOGIN_STARTUP = 1003
    ID_EXIT = 1004
    IDI_APPLICATION = 32512
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    SM_CXSMICON = 49
    SM_CYSMICON = 50

    def __init__(
        self, root: tk.Tk, open_dashboard: Callable[[], None],
        open_status: Callable[[], None], toggle_login_startup: Callable[[], None],
        exit_application: Callable[[], None],
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("通知区域图标仅支持 Windows")
        self.root = root
        self.open_dashboard = open_dashboard
        self.open_status = open_status
        self.toggle_login_startup = toggle_login_startup
        self.exit_application = exit_application
        self.command_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
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
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
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
        self.class_name = f"LifeRadioTrayWindow_{os.getpid()}"
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
            0, self.class_name, "Life Link Tray", 0,
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
        self.notify_data.szTip = "Life Link PC 客户端"
        if not self.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self.notify_data)):
            self.user32.DestroyWindow(self.hwnd)
            self.user32.UnregisterClassW(self.class_name, self.hinstance)
            self._release_icon()
            raise ctypes.WinError()
        self.pump_job = self.root.after(50, self.pump_messages)

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
            logging.warning("Life Link 客户端托盘图标加载失败，改用系统默认图标")
        icon_resource = ctypes.cast(ctypes.c_void_p(self.IDI_APPLICATION), wintypes.LPCWSTR)
        return self.user32.LoadIconW(None, icon_resource)

    def _release_icon(self) -> None:
        if self.owns_icon:
            self.user32.DestroyIcon(self.icon)
            self.owns_icon = False

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == self.CALLBACK_MESSAGE:
            if lparam == self.WM_LBUTTONUP:
                self.command_queue.put("status")
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
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_OPEN_DASHBOARD, "打开 Dashboard")
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_OPEN_STATUS, "打开用时状态窗口")
            self.user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
            try:
                startup_enabled = bool(pc_windows_startup.status().get("enabled"))
            except (OSError, RuntimeError):
                startup_enabled = False
            startup_flags = self.MF_STRING | (self.MF_CHECKED if startup_enabled else 0)
            self.user32.AppendMenuW(menu, startup_flags, self.ID_TOGGLE_LOGIN_STARTUP, "开机启动")
            self.user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
            self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT, "退出 PC 客户端")
            point = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            self.user32.SetForegroundWindow(self.hwnd)
            command = self.user32.TrackPopupMenu(
                menu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
                point.x, point.y, 0, self.hwnd, None,
            )
            self.user32.PostMessageW(self.hwnd, self.WM_NULL, 0, 0)
            if command == self.ID_OPEN_DASHBOARD:
                self.command_queue.put("dashboard")
            elif command == self.ID_OPEN_STATUS:
                self.command_queue.put("status")
            elif command == self.ID_TOGGLE_LOGIN_STARTUP:
                self.command_queue.put("toggle-login-startup")
            elif command == self.ID_EXIT:
                self.command_queue.put("exit")
        finally:
            self.user32.DestroyMenu(menu)

    def pump_messages(self) -> None:
        if self.closed:
            return
        message = wintypes.MSG()
        # Only dispatch messages belonging to the hidden tray window. Passing
        # None here also consumes Tk's native move/paint messages, which makes
        # the status window jump or stop dragging and can re-enter tkinter from
        # DispatchMessageW on Python 3.13.
        while self.user32.PeekMessageW(
            ctypes.byref(message), self.hwnd, 0, 0, self.PM_REMOVE,
        ):
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))
        while not self.closed:
            try:
                command = self.command_queue.get_nowait()
            except queue.Empty:
                break
            if command == "dashboard":
                self.open_dashboard()
            elif command == "status":
                self.open_status()
            elif command == "toggle-login-startup":
                self.toggle_login_startup()
            elif command == "exit":
                self.exit_application()
        if not self.closed:
            self.pump_job = self.root.after(50, self.pump_messages)

    def notify(self, title: str, message: str, *, warning: bool = True) -> bool:
        if self.closed:
            return False
        original_flags = self.notify_data.uFlags
        self.notify_data.uFlags = self.NIF_INFO
        self.notify_data.szInfoTitle = title[:63]
        self.notify_data.szInfo = message[:255]
        self.notify_data.dwInfoFlags = self.NIIF_WARNING if warning else 0
        self.notify_data.uTimeoutOrVersion = 10_000
        delivered = bool(
            self.shell32.Shell_NotifyIconW(
                self.NIM_MODIFY, ctypes.byref(self.notify_data),
            )
        )
        self.notify_data.uFlags = original_flags
        if not delivered:
            logging.error("Windows 久坐通知提交失败")
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except RuntimeError:
            logging.warning("久坐提醒提示音播放失败")
        return delivered

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if getattr(self, "pump_job", None) is not None:
            try:
                self.root.after_cancel(self.pump_job)
            except tk.TclError:
                pass
            self.pump_job = None
        if getattr(self, "notify_data", None) is not None:
            self.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.notify_data))
        if getattr(self, "hwnd", None):
            self.user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        self.user32.UnregisterClassW(self.class_name, self.hinstance)
        self._release_icon()


class LifeRadioDesktopApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.server_process: subprocess.Popen[bytes] | None = None
        self.quitting = False
        self.mutex_handle: int | None = None
        self.status_window: UsageStatusWindow | None = None
        self.tray: WindowsTrayIcon | None = None
        self.server_log_handle = None
        self.http_opener = build_opener(ProxyHandler({}))
        self.server_health_failures = 0
        self.last_server_health_check = 0.0
        self.root.report_callback_exception = self.report_tk_callback_exception

    @staticmethod
    def report_tk_callback_exception(
        exc_type: type[BaseException], exc_value: BaseException, exc_traceback: object,
    ) -> None:
        """Keep Tk callback failures visible in the durable desktop log."""
        logging.error(
            "桌面回调执行失败",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def acquire_single_instance(self) -> bool:
        if sys.platform != "win32":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.mutex_handle = kernel32.CreateMutexW(None, True, "Local\\LifeRadioDesktopApp")
        return bool(self.mutex_handle) and kernel32.GetLastError() != 183

    def server_is_ready(self) -> bool:
        try:
            with self.http_opener.open(HEALTH_URL, timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and payload.get("status") == "ok"
        except Exception:
            return False

    def start_server(self) -> None:
        if self.server_is_ready():
            logging.info("检测到已运行的同步服务，桌面程序直接连接")
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if self.server_log_handle is not None:
            self.server_log_handle.close()
            self.server_log_handle = None
        try:
            rotate_server_log_if_needed()
        except OSError as error:
            logging.warning("同步服务日志轮转失败，保留原日志继续启动：%s", error)
        self.server_log_handle = SERVER_LOG_FILE.open("ab", buffering=0)
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        command = (
            [sys.executable, "--lifelink-sync-worker"]
            if is_frozen() else [sys.executable, str(SERVER_SCRIPT)]
        )
        self.server_process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            env=os.environ.copy(),
            creationflags=creation_flags,
            stdin=subprocess.DEVNULL,
            stdout=self.server_log_handle,
            stderr=subprocess.STDOUT,
        )
        logging.info("已启动后台同步服务 pid=%s", self.server_process.pid)
        deadline = time.monotonic() + SYNC_SERVER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.server_process.poll() is not None:
                raise RuntimeError(f"同步服务启动失败，退出码 {self.server_process.returncode}")
            if self.server_is_ready():
                return
            time.sleep(0.2)
        raise RuntimeError(
            f"同步服务在 {SYNC_SERVER_START_TIMEOUT_SECONDS} 秒内未能监听 {_DEFAULT_PORT} 端口"
        )

    def open_dashboard(self) -> None:
        """Open the central WebUI without exposing this client's credential."""
        central_url = os.environ.get("LIFE_RADIO_CENTRAL_BASE_URL", "").rstrip("/")
        token = os.environ.get("LIFE_RADIO_CENTRAL_TOKEN", "")
        try:
            if not central_url or not token:
                raise RuntimeError("客户端尚未完成中央配对")
            request = Request(
                f"{central_url}/v1/web-sessions",
                data=b"{}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with self.http_opener.open(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
            web_url = payload.get("web_url") if isinstance(payload, dict) else None
            if not isinstance(web_url, str) or not web_url.startswith("https://"):
                raise RuntimeError("中央没有返回可用的 HTTPS WebUI 地址")
            webbrowser.open(web_url)
            return
        except Exception as error:
            logging.warning("打开中央 WebUI 失败：%s", error)
            if getattr(error, "code", None) == 404:
                detail = "当前运行的中央服务版本不支持 HTTPS WebUI；请重启为当前 LifeLink 中央服务。"
            else:
                detail = "请先在中央服务端配置并验证 HTTPS 外部地址。"
            messagebox.showerror(
                "Life Link",
                f"无法打开中央 HTTPS WebUI：{error}\n\n{detail}",
            )

    def open_status(self) -> None:
        if self.status_window is not None:
            self.status_window.show()

    def toggle_login_startup(self) -> None:
        """Toggle only this PC client's own Windows login shortcut."""
        try:
            current = pc_windows_startup.status()
            enabled = not bool(current.get("enabled"))
            updated = pc_windows_startup.set_enabled(enabled)
        except (OSError, RuntimeError) as error:
            logging.exception("PC 登录后启动设置失败")
            if self.tray is not None:
                self.tray.notify("Life Link PC 客户端", f"开机启动设置失败：{error}")
            return
        if self.tray is not None:
            message = "已开启开机启动" if updated.get("enabled") else "已关闭开机启动"
            self.tray.notify("Life Link PC 客户端", message, warning=False)

    def record_custom_event(
        self, event_key: str, title: str, detail: str = "",
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Submit a local app event without blocking the Tk message loop."""
        payload = {
            "event_key": event_key,
            "title": title,
            "detail": detail,
            "metadata": metadata or {},
        }

        def submit() -> None:
            request = Request(
                CUSTOM_EVENT_URL,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self.http_opener.open(request, timeout=3) as response:
                    response.read()
                logging.info("已记录自定义事件：%s", event_key)
            except Exception:
                logging.exception("自定义事件记录失败：%s", event_key)

        threading.Thread(
            target=submit,
            daemon=True,
            name=f"life-radio-event-{event_key}",
        ).start()

    def show_sedentary_notification(self, title: str, message: str) -> None:
        logging.info("%s: %s", title, message)
        active_seconds = (
            int(self.status_window.sedentary_timer.active_seconds)
            if self.status_window is not None else 0
        )
        self.record_custom_event(
            "sedentary.reminder_triggered",
            title,
            message,
            {
                "active_seconds": active_seconds,
                "limit_seconds": SEDENTARY_LIMIT_SECONDS,
            },
        )
        if self.tray is not None:
            self.tray.notify(title, message)

    def stop_server(self) -> None:
        process = self.server_process
        if process is not None and process.poll() is None:
            try:
                # The child is created without a console window. Sending
                # CTRL_BREAK_EVENT to such a process can raise WinError 6 and
                # leave a stale sync server plus desktop mutex behind.
                process.terminate()
                process.wait(timeout=5)
            except (OSError, SystemError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        if self.server_log_handle is not None:
            self.server_log_handle.close()
            self.server_log_handle = None

    def exit_application(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        if self.status_window is not None:
            self.status_window.destroy()
        if self.tray is not None:
            self.tray.close()
        self.stop_server()
        if self.mutex_handle and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None
        self.root.quit()
        self.root.destroy()

    def monitor_server(self) -> None:
        try:
            if self.quitting:
                return
            process_stopped = (
                self.server_process is not None
                and self.server_process.poll() is not None
            )
            now = time.monotonic()
            health_check_due = now - self.last_server_health_check >= 5
            if health_check_due:
                self.last_server_health_check = now
                if self.server_is_ready():
                    self.server_health_failures = 0
                else:
                    self.server_health_failures += 1

            health_stalled = self.server_health_failures >= 3
            if process_stopped or health_stalled:
                exit_code = (
                    self.server_process.returncode
                    if process_stopped and self.server_process is not None
                    else "health-check-failed"
                )
                logging.error(
                    "PC Sync Server 不可用，状态 %s；正在自动重启",
                    exit_code,
                )
                if self.server_process is not None and self.server_process.poll() is None:
                    self.stop_server()
                self.server_process = None
                if self.server_log_handle is not None:
                    self.server_log_handle.close()
                    self.server_log_handle = None
                self.start_server()
                self.server_health_failures = 0
                self.last_server_health_check = time.monotonic()
                logging.info("PC Sync Server 已自动重启")
                if self.tray is not None:
                    self.tray.notify("Life Link", "同步服务异常后已自动恢复。")
        except Exception as error:
            # Never let one failed recovery cancel the recurring monitor.
            logging.exception("PC Sync Server 自动恢复失败")
            if self.tray is not None:
                self.tray.notify(
                    "Life Link 服务暂不可用",
                    f"自动恢复失败，将继续重试：{error}",
                )
        finally:
            if not self.quitting:
                self.root.after(1_000, self.monitor_server)

    def run(self) -> int:
        if not self.acquire_single_instance():
            # A second launch is also a repair request. The existing tray
            # process may still be alive while its HTTP child has disappeared.
            if not self.server_is_ready():
                try:
                    logging.warning("检测到已有桌面实例，但同步服务不可用；正在修复")
                    self.start_server()
                except Exception:
                    logging.exception("第二次启动未能修复同步服务")
            self.open_dashboard()
            self.root.destroy()
            return 0
        try:
            self.start_server()
            self.status_window = UsageStatusWindow(
                self.root, notify_break=self.show_sedentary_notification,
                open_dashboard=self.open_dashboard,
            )
            self.tray = WindowsTrayIcon(
                self.root, self.open_dashboard, self.open_status,
                self.toggle_login_startup, self.exit_application,
            )
            self.record_custom_event(
                "application.started",
                "Life Link 已启动",
                "PC 同步服务、状态窗口与系统托盘已就绪。",
                {
                    "platform": platform.system() or "Windows",
                    "platform_release": platform.release(),
                    "machine": platform.machine(),
                },
            )
        except Exception as error:
            messagebox.showerror("Life Link 启动失败", str(error))
            self.stop_server()
            if self.mutex_handle and sys.platform == "win32":
                ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
            self.root.destroy()
            return 1
        self.root.after(1_000, self.monitor_server)
        if not os.environ.get("LIFE_RADIO_NO_BROWSER"):
            self.root.after(100, self.open_dashboard)
        # Let the browser launch first, then surface the status window so it is
        # visible even when the persisted topmost option is disabled.
        if not os.environ.get("LIFE_RADIO_BACKGROUND_START"):
            self.root.after(400, self.open_status)
        self.root.mainloop()
        return 0


def main() -> int:
    if sys.platform != "win32":
        print("Life Link 桌面托盘目前仅支持 Windows。", file=sys.stderr)
        return 1
    configure_logging()
    try:
        pc_windows_startup.ensure_default_enabled()
    except OSError:
        logging.exception("pc login startup entry refresh failed")
    logging.info("Life Link 桌面程序启动")
    return LifeRadioDesktopApp().run()


if __name__ == "__main__":
    raise SystemExit(main())
