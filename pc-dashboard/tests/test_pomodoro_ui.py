"""Exercise the actual Tk layout without calling production services."""
import ctypes
import json
import os
import struct
import sys
import tempfile
import tkinter as tk
import unittest
import zlib
from datetime import datetime, timezone
from ctypes import wintypes
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

import desktop_app
from timer_visuals import png, ring_sweep, render_compact, TimerVisual, TimerCard, BODY_CENTER, RING_RADIUS, GOAL_RECT, NEXT_RECT, BUTTON_SPACING, WIDTH, EXPAND_CENTER, BUTTON_RADIUS


def capture_window(window, destination):
    """Capture only our test window via GDI; PNG uses the standard library."""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    u.GetParent.argtypes, u.GetParent.restype = [wintypes.HWND], wintypes.HWND
    u.GetDC.argtypes, u.GetDC.restype = [wintypes.HWND], wintypes.HDC
    g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
    g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g.SelectObject.argtypes, g.SelectObject.restype = [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ
    u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteDC.argtypes = [wintypes.HDC]
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    hwnd = u.GetParent(window.winfo_id())
    width, height = window.winfo_width(), window.winfo_height()
    dc = u.GetDC(hwnd)
    memory = g.CreateCompatibleDC(dc)
    bitmap = g.CreateCompatibleBitmap(dc, width, height)
    previous = g.SelectObject(memory, bitmap)
    try:
        if not u.PrintWindow(hwnd, memory, 2):
            raise OSError("Test window capture failed")
        header = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not g.GetDIBits(memory, bitmap, 0, height, buffer, header, 0):
            raise OSError("Test bitmap read failed")
        raw = buffer.raw
        rgba = bytearray(len(raw))
        rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = raw[2::4], raw[1::4], raw[0::4], bytes([255]) * (width * height)
        rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        destination.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                                + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    finally:
        g.SelectObject(memory, previous)
        g.DeleteObject(bitmap)
        g.DeleteDC(memory)
        u.ReleaseDC(hwnd, dc)


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("LIFE_LINK_TEST_TK_UI") == "1",
                     "Run Tk checks in their own process with LIFE_LINK_TEST_TK_UI=1")
class PomodoroWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patches = [mock.patch.object(desktop_app, "POMODORO_FILE", Path(self.directory.name) / "pomodoro.json"),
                        mock.patch.object(desktop_app, "SETTINGS_FILE", Path(self.directory.name) / "settings.json")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.window = desktop_app.UsageStatusWindow(self.root)
        for attr in ("poll_job", "refresh_job", "sedentary_job", "personal_time_job", "timeline_refresh_job"):
            self.root.after_cancel(getattr(self.window, attr))
            setattr(self.window, attr, None)
        self.window.window.geometry("360x540+60+60")
        self.window.initial_position_applied = True
        self.window.window.deiconify()
        self.root.update()
        self.addCleanup(self.window.destroy)

    def capture(self, name):
        self.root.update()
        folder = os.environ.get("LIFE_LINK_UI_CAPTURE_DIR")
        if folder:
            destination = Path(folder)
            destination.mkdir(parents=True, exist_ok=True)
            if self.window.collapsed:
                frame = self.window.compact_frame
                (destination / f"{name}.png").write_bytes(png(*frame[:3]))
            else:
                capture_window(self.window.window, destination / f"{name}.png")

    def test_real_layout_and_next_round_goal_editing(self):
        w = self.window
        w.render({"current_app": "Code.exe", "today_app_seconds": 13320})
        interval = {"interval_id": "preview", "name": "工作时间", "start_local_time": "09:00",
                    "end_local_time": "18:00", "color": "#a78bfa"}
        w.time_intervals = [interval, {"start_local_time": "22:00", "end_local_time": "06:00", "color": "#38bdf8"}]
        w.time_interval_state = {"current": interval, "current_remaining_seconds": 2700}
        w.time_interval_state_received_at = desktop_app.time.monotonic()
        with mock.patch.object(w, "_refresh_time_interval_state"):
            w.update_personal_time()
        self.root.after_cancel(w.personal_time_job)
        w.personal_time_job = None
        w._render_timeline([{"timeline_event_id": "preview-event", "occurred_at": datetime.now(timezone.utc).isoformat(),
                             "event_key": "pomodoro.completed", "title": "番茄钟完成", "importance": "normal",
                             "detail": "工作计时 25 分钟；目标：完成接口测试"}])
        self.capture("01-sedentary-expanded")
        w.toggle_collapsed()
        self.capture("02-sedentary-compact")
        self.assertTrue(w.compact_window.visible)
        self.assertEqual(w.compact_frame[:2], (WIDTH, 130))
        w.open_pomodoro_setup()
        self.assertFalse(w.collapsed)
        w.pomodoro_goal_text.set("完成照片同步接口")
        self.capture("03-setup")
        self.assertTrue(w.goal_entry.winfo_ismapped())
        self.assertEqual(w.goal_entry.get(), "完成照片同步接口")
        w.start_pomodoro()
        w.pomodoro.advance(1)
        w.render_pomodoro()
        self.capture("04-focus-expanded")
        self.assertTrue(w.goal_row.winfo_ismapped())
        self.assertTrue(w.dashboard_button.winfo_ismapped())
        self.assertLess(w.personal_time_card.winfo_rooty(), w.events_card.winfo_rooty())
        self.assertFalse(hasattr(w, "controls"))
        self.assertEqual(w.events_card.winfo_y() + w.events_card.winfo_height(), w.outer.winfo_height() - 8)
        self.assertGreater(w.goal_row.winfo_y(), w.online_canvas.winfo_y())
        w.toggle_collapsed()
        self.capture("05-focus-compact")
        self.assertFalse(w.titlebar.winfo_ismapped())
        self.assertEqual(w.compact_frame[:2], (WIDTH, 164))
        self.assertEqual(list(w.compact_window.actions), ["expand", "pause", "stop"])
        w.pomodoro.remaining = 1
        w.pomodoro.advance(1)
        w._sync_timer_layout()
        self.capture("06-rest-compact")
        self.assertEqual(w.pomodoro.phase, "rest")
        w.pomodoro.remaining = 1
        w.pomodoro.advance(1)
        w._sync_timer_layout()
        self.capture("07-complete-compact")
        w.open_pomodoro_setup()
        self.assertEqual(w.pomodoro_goal_text.get(), "完成照片同步接口")
        w.toggle_collapsed()
        self.capture("08-ready-compact")
        self.assertFalse(w.compact_visual.complete)
        self.assertTrue(w.compact_visual.ready)
        self.assertEqual(w.compact_visual.clock, "25:00")
        self.assertEqual(w.compact_visual.label, "待开始")
        w.toggle_collapsed()
        w.pomodoro_goal_text.set("写测试")
        w.start_pomodoro()
        self.assertEqual(w.pomodoro.goal, "写测试")
        w.stop_pomodoro()
        self.root.update()
        self.assertTrue(w.goal_entry.winfo_ismapped())
        w.select_timer_mode("sedentary")
        self.root.update_idletasks()
        self.assertFalse(w.goal_row.winfo_ismapped())

    def test_afk_runs_pomodoro_and_sedentary_is_exclusive(self):
        w = self.window
        w.pomodoro_goal_text.set("阅读")
        w.start_pomodoro()
        w.last_timer_tick = 100
        with mock.patch.object(desktop_app.time, "monotonic", return_value=105), \
             mock.patch.object(desktop_app, "windows_session_locked", return_value=False), \
             mock.patch.object(w.input_activity_detector, "sample", return_value=("afk", 250)), \
             mock.patch.object(w, "_flush_pomodoro_events"):
            w.update_sedentary_timer()
        self.assertEqual(w.pomodoro.remaining, 1495)
        self.assertEqual(w.sedentary_timer.active_seconds, 0)
        w.stop_pomodoro()
        w.select_timer_mode("sedentary")
        self.assertFalse(w.sedentary_paused)

    def test_sedentary_is_preserved_and_pauses_across_mode_start_stop_and_return(self):
        w = self.window
        timer = w.sedentary_timer
        timer.active_seconds, timer.afk_seconds = 600, 17
        timer.reminder_active, timer.next_notification_multiple = True, 2
        snapshot = (600, 17, True, 2)
        def state():
            return timer.active_seconds, timer.afk_seconds, timer.reminder_active, timer.next_notification_multiple
        w.select_timer_mode("pomodoro")
        self.assertTrue(w.sedentary_paused)
        self.assertEqual(state(), snapshot)
        w.start_pomodoro()
        w.last_timer_tick = 100
        with mock.patch.object(desktop_app.time, "monotonic", return_value=105), \
             mock.patch.object(desktop_app, "windows_session_locked", return_value=False), \
             mock.patch.object(w.input_activity_detector, "sample", return_value=("afk", 900)), \
             mock.patch.object(w, "_flush_pomodoro_events"):
            w.update_sedentary_timer()
        self.assertEqual(state(), snapshot)
        self.assertEqual(w.pomodoro.remaining, 1495)
        self.root.after_cancel(w.sedentary_job)
        w.sedentary_job = None
        w.stop_pomodoro()
        self.assertEqual(state(), snapshot)
        for _ in range(3):
            w.select_timer_mode("sedentary")
            self.assertFalse(w.sedentary_paused)
            self.assertEqual(state(), snapshot)
            w.select_timer_mode("pomodoro")
        w.select_timer_mode("sedentary")
        w.last_timer_tick = 200
        with mock.patch.object(desktop_app.time, "monotonic", return_value=205), \
             mock.patch.object(w.input_activity_detector, "sample", return_value=("active", 0)), \
             mock.patch.object(w, "_flush_pomodoro_events"):
            w.update_sedentary_timer()
        self.assertEqual(timer.active_seconds, 605)
        w.active_leave()
        self.assertEqual(timer.active_seconds, 0)

    def test_system_pause_does_not_resume_or_overwrite_sedentary_preference(self):
        w = self.window
        w.sedentary_paused = True
        w.start_pomodoro()
        w.pause_pomodoro_for_system()
        self.assertTrue(w.pomodoro.paused)
        w.stop_pomodoro()
        self.assertTrue(w.sedentary_paused)

    def test_completion_stays_pending_until_local_server_confirms_same_id(self):
        w = self.window
        w.start_pomodoro()
        w.pomodoro.remaining = 1
        w.pomodoro.advance(1)
        event_id = w.pomodoro.pending_events[0]["event_id"]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"status": "stored", "event": {"event_id": event_id}}).encode()
        health = mock.MagicMock()
        health.__enter__.return_value.read.return_value = b'{"capabilities":{"custom_event_idempotency":true}}'
        with mock.patch.object(w.opener, "open", side_effect=OSError("offline")), \
             mock.patch.object(desktop_app.threading, "Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=target)), \
             self.assertLogs(level="ERROR"):
            w._flush_pomodoro_events()
        self.assertEqual(len(w.pomodoro.pending_events), 1)
        w.pomodoro_last_submit = 0
        with mock.patch.object(w.opener, "open", side_effect=[health, response]), \
             mock.patch.object(desktop_app.threading, "Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=target)):
            w._flush_pomodoro_events()
        self.assertEqual(len(w.pomodoro.pending_events), 1)
        w._flush_pomodoro_events()
        self.assertFalse(w.pomodoro.pending_events)

    def test_old_local_service_is_not_sent_a_completion_it_would_duplicate(self):
        w = self.window
        w.start_pomodoro()
        w.pomodoro.remaining = 1
        w.pomodoro.advance(1)
        health = mock.MagicMock()
        health.__enter__.return_value.read.return_value = b'{"status":"ok"}'
        with mock.patch.object(w.opener, "open", return_value=health) as opened, \
             mock.patch.object(desktop_app.threading, "Thread", side_effect=lambda target, **kwargs: SimpleNamespace(start=target)), \
             self.assertLogs(level="ERROR"):
            w._flush_pomodoro_events()
        opened.assert_called_once_with(desktop_app.HEALTH_URL, timeout=3)
        self.assertEqual(len(w.pomodoro.pending_events), 1)

    def test_modes_keep_shared_bar_alignment_without_sedentary_goal_gap(self):
        w = self.window
        sed_y = w.online_canvas.winfo_rooty()
        sed_height = w.sedentary_card.winfo_height()
        w.select_timer_mode("pomodoro")
        self.root.update()
        self.assertEqual(w.online_canvas.winfo_rooty(), sed_y)
        self.assertGreater(w.sedentary_card.winfo_height(), sed_height)
        self.assertEqual(w.active_leave_button.winfo_reqheight(), w.pomodoro_start_button.winfo_height())
        w.start_pomodoro()
        self.root.update()
        self.assertEqual(w.online_canvas.winfo_rooty(), sed_y)
        self.assertEqual(w.pomodoro_pause_button.winfo_height(), w.pomodoro_stop_button.winfo_height())
        self.assertLessEqual(abs((w.timer_actions.winfo_y() + w.timer_actions.winfo_height() / 2)
                                 - (w.timer_status.winfo_y() + w.timer_status.winfo_height() / 2)), 1)
        w.select_timer_mode("sedentary")
        self.root.update()
        self.assertTrue(w.pomodoro.paused)
        self.assertEqual(w.sedentary_card.winfo_height(), sed_height)
        w.select_timer_mode("pomodoro")
        self.assertTrue(w.pomodoro.paused)
        self.assertFalse(w.pomodoro_setup)

    def test_expand_anchor_is_fixed_and_compact_actions_work(self):
        w = self.window
        def anchor():
            return (w.collapse_button.winfo_rootx() + w.collapse_button.winfo_width() // 2,
                    w.collapse_button.winfo_rooty() + w.collapse_button.winfo_height() // 2)
        original = anchor()
        for _ in range(3):
            w.toggle_collapsed()
            self.root.update()
            self.assertEqual(w.compact_window.anchor(), original)
            self.assertEqual(list(w.compact_window.actions), ["expand", "leave"])
            w.sedentary_timer.active_seconds = 300
            w._dispatch_compact_action("leave")
            self.assertEqual(w.sedentary_timer.active_seconds, 0)
            w.toggle_collapsed()
            self.root.update()
            self.assertEqual(anchor(), original)
            self.assertEqual(w.collapse_button.text, "▼")
        w.start_pomodoro()
        w.toggle_collapsed()
        self.root.update()
        overlay = w.compact_window
        for action in ("pause", "stop"):
            left, top, right, bottom = overlay.actions[action]
            point = ((top + bottom) // 2 << 16) | ((left + right) // 2)
            overlay._proc(overlay.hwnd, 0x201, 1, point)
            overlay._proc(overlay.hwnd, 0x202, 0, point)
            self.root.after_cancel(overlay.job)
            overlay._pump()
            if action == "pause":
                self.assertTrue(w.pomodoro.paused)
            else:
                self.assertEqual(w.pomodoro.phase, "idle")

    def test_sedentary_compact_always_counts_up_even_when_afk(self):
        w = self.window
        w.sedentary_timer.active_seconds = 150
        w.sedentary_timer.afk_seconds = 40
        w.current_activity_state = "afk"
        w.toggle_collapsed()
        self.assertEqual(w.compact_visual.label, "本轮在线")
        self.assertEqual(w.compact_visual.clock, "02:30")
        self.assertEqual(w.compact_visual.progress, w.sedentary_timer.online_progress)

    def test_clock_color_status_column_and_header_icons_are_consistent(self):
        w = self.window
        status_x = w.timer_status.winfo_rootx()
        self.assertEqual(w.timer_time.cget("foreground"), w.normal_color)
        self.assertEqual(w.collapse_button.button_width, 26)
        headers = [widget for widget in w.titlebar_substrate_widgets if isinstance(widget, desktop_app.TimerButton)]
        self.assertEqual(len(headers), 3)
        self.assertEqual({widget.winfo_height() for widget in headers}, {28})
        self.assertEqual({widget.winfo_rooty() for widget in headers}, {w.collapse_button.winfo_rooty()})
        w.window.geometry("420x540+60+60")
        self.root.update()
        self.assertEqual(w.timer_status.winfo_rootx(), status_x)
        w.select_timer_mode("pomodoro")
        self.root.update()
        self.assertEqual(w.timer_time.cget("foreground"), w.normal_color)
        self.assertEqual(w.timer_status.winfo_rootx(), status_x)
        w.start_pomodoro()
        self.root.update()
        self.assertEqual(w.timer_status.winfo_rootx(), status_x)
        self.assertEqual(w.pomodoro_stop_button.text, "■")
        self.assertEqual(w.sedentary_card.border_canvas.winfo_x(), 0)
        self.assertEqual(w.sedentary_card.border_canvas.winfo_y(), 0)
        self.assertEqual(w.sedentary_card.border_canvas.winfo_width(), w.sedentary_card.winfo_width())

    def test_startup_does_not_reopen_previous_completion_screen(self):
        timer = desktop_app.PomodoroTimer(focus_seconds=1, rest_seconds=1)
        timer.start("上一轮目标")
        timer.advance(2)
        restored = desktop_app.PomodoroTimer.restore(timer.snapshot())
        with mock.patch.object(desktop_app, "load_timer", return_value=restored), \
             mock.patch.object(desktop_app, "load_desktop_settings", return_value={"timer_mode": "pomodoro"}):
            reopened = desktop_app.UsageStatusWindow(self.root)
        self.addCleanup(reopened.destroy)
        for attr in ("poll_job", "refresh_job", "sedentary_job", "personal_time_job", "timeline_refresh_job"):
            self.root.after_cancel(getattr(reopened, attr))
            setattr(reopened, attr, None)
        reopened.window.geometry("360x540+60+60")
        reopened.window.deiconify()
        self.root.update()
        self.assertEqual(reopened._timer_view(), "setup")
        self.assertEqual(reopened.timer_time.cget("text"), "25:00")
        self.assertTrue(reopened.pomodoro_start_button.winfo_ismapped())
        self.assertFalse(reopened.pomodoro_next_button.winfo_ismapped())
        self.assertEqual(reopened.goal_entry.get(), "上一轮目标")
        self.assertEqual(len(reopened.pomodoro.pending_events), 1)

    def test_all_panels_are_rounded_and_only_header_has_topmost(self):
        w = self.window
        cards = [w.value_labels["today_app"].master, w.value_labels["current_app"].master,
                 w.sedentary_card, w.personal_time_card, w.events_card]
        self.assertIs(w.personal_time_card.master, w.outer)
        for card in cards:
            self.assertIsInstance(card, TimerCard)
        def descend(widget):
            yield widget
            for child in widget.winfo_children():
                yield from descend(child)
        checks = [widget for widget in descend(w.window) if isinstance(widget, tk.Checkbutton)]
        self.assertEqual(checks, [w.title_topmost_check])
        for card in cards:
            if card.winfo_ismapped():
                self.assertTrue(card.border_image.transparency_get(0, 0))
                self.assertEqual(card.border_canvas.winfo_x(), 0)
        w.topmost_value.set(True)
        w.apply_topmost()
        self.assertTrue(w.window.attributes("-topmost"))
        self.capture("09-rounded-panels")

    def test_rounded_event_card_keeps_highlight_and_border_on_hover(self):
        w = self.window
        w.timeline_first_load = False
        event = {"timeline_event_id": "rounded-event", "occurred_at": datetime.now(timezone.utc).isoformat(),
                 "title": "圆角事件", "importance": "high", "detail": "检查高亮与恢复"}
        w._render_timeline([event])
        self.root.update()
        card = w.event_cards["rounded-event"]["card"]
        self.assertIsInstance(card, TimerCard)
        self.assertEqual(card.card_color, w.event_new_bg)
        self.assertEqual(card.border_width, 2)
        self.assertTrue(card.border_image.transparency_get(0, 0))
        w._fade_event("rounded-event")
        self.root.update()
        self.assertEqual(card.card_color, w.card_background)
        self.assertEqual(card.border_width, 2)
        self.assertTrue(card.border_image.transparency_get(0, 0))
        self.capture("10-rounded-event")

    def test_initial_height_is_512_and_all_extra_height_goes_to_events(self):
        w = self.window
        w.time_intervals = [{"name": "工作时间", "start_local_time": "09:00", "end_local_time": "18:00"}]
        w._sync_timer_layout()
        for mode in ("sedentary", "pomodoro"):
            w.select_timer_mode(mode)
            w.window.geometry("320x420+60+60")
            self.root.update()
            upper = [w.totals_row, w.sedentary_card]
            before = [(part.winfo_y(), part.winfo_width(), part.winfo_height()) for part in upper]
            events_height = w.events_card.winfo_height()
            viewport_height = w.events_canvas.winfo_height()
            w.initial_position_applied = False
            with mock.patch.object(w.window, "winfo_screenwidth", return_value=1600), \
                 mock.patch.object(w.window, "winfo_screenheight", return_value=1000):
                w.apply_initial_geometry()
            self.root.update()
            self.assertEqual(w.window.winfo_height(), 512)
            self.assertEqual(w.window.winfo_width(), 320)
            self.assertEqual([(part.winfo_y(), part.winfo_width(), part.winfo_height()) for part in upper], before)
            self.assertEqual(w.events_card.winfo_height() - events_height, 92)
            self.assertEqual(w.events_canvas.winfo_height() - viewport_height, 92)
            self.capture(f"11-height512-{mode}")

    def _set_interval(self, current=True):
        interval = {"interval_id": "work", "name": "下午工作", "start_local_time": "13:00",
                    "end_local_time": "18:00", "color": "#ea580c"}
        self.window.time_intervals = [interval]
        self.window.time_interval_state = {"current": interval if current else None, "current_remaining_seconds": 7200}
        self.window.time_interval_state_received_at = desktop_app.time.monotonic()
        self.window._sync_timer_layout()
        self.window._render_personal_time()
        return interval

    def test_tabs_are_independent_and_none_hides_timer_card_reclaiming_space(self):
        w = self.window
        self._set_interval()
        w.start_pomodoro()
        w.pomodoro.advance(3)
        w.sedentary_timer.active_seconds = 600
        self.root.update()
        old_events = w.events_canvas.winfo_height()
        old_card = w.sedentary_card.winfo_height()
        self.assertIs(w.timer_tabs.master, w.outer)
        self.assertIs(w.sedentary_card.master, w.outer)
        self.assertIs(w.personal_time_card.master, w.outer)
        self.assertLess(w.timer_tabs.winfo_rooty(), w.sedentary_card.winfo_rooty())
        self.assertLess(w.sedentary_card.winfo_rooty() + w.sedentary_card.winfo_height(),
                        w.personal_time_card.winfo_rooty())
        tab_y = w.timer_tabs.winfo_rooty()
        self.capture("12-separate-pomodoro")
        w.select_timer_mode("none")
        self.root.update()
        self.assertTrue(w.pomodoro.paused)
        self.assertTrue(w.sedentary_paused)
        self.assertEqual(w.sedentary_timer.active_seconds, 600)
        self.assertEqual(w.pomodoro.remaining, 1497)
        self.assertFalse(w.timer_content.winfo_ismapped())
        self.assertFalse(w.sedentary_card.winfo_ismapped())
        self.assertTrue(w.personal_time_card.winfo_ismapped())
        self.assertTrue(w.none_tab.winfo_ismapped())
        self.assertIs(w.personal_time_card.master, w.outer)
        self.assertEqual(w.timer_tabs.winfo_rooty(), tab_y)
        saved = w.events_canvas.winfo_height() - old_events
        self.assertGreater(saved, 50)
        self.assertEqual(saved, old_card + 5)
        self.capture("13-none-expanded")
        w.last_timer_tick = 100
        with mock.patch.object(desktop_app.time, "monotonic", return_value=105), \
             mock.patch.object(w.input_activity_detector, "sample", return_value=("active", 0)), \
             mock.patch.object(w, "_flush_pomodoro_events"):
            w.update_sedentary_timer()
        self.root.after_cancel(w.sedentary_job)
        w.sedentary_job = None
        self.assertEqual(w.pomodoro.remaining, 1497)
        self.assertEqual(w.sedentary_timer.active_seconds, 600)
        w.select_timer_mode("sedentary")
        self.root.update()
        self.assertTrue(w.sedentary_card.winfo_ismapped())
        self.assertTrue(w.personal_time_card.winfo_ismapped())
        self.assertEqual(w.timer_tabs.winfo_rooty(), tab_y)
        self.capture("16-separate-sedentary")
        self.assertFalse(w.sedentary_paused)
        self.assertEqual(w.sedentary_timer.active_seconds, 600)
        w.select_timer_mode("pomodoro")
        self.assertTrue(w.pomodoro.paused)
        self.assertEqual(w.pomodoro.remaining, 1497)

    def test_none_compact_interval_updates_locally_and_restores_anchor(self):
        w = self.window
        interval = self._set_interval()
        w.select_timer_mode("none")
        self.root.update()
        anchor = (w.collapse_button.winfo_rootx() + w.collapse_button.winfo_width() // 2,
                  w.collapse_button.winfo_rooty() + w.collapse_button.winfo_height() // 2)
        received = w.time_interval_state_received_at
        with mock.patch.object(desktop_app.time, "monotonic", return_value=received):
            w.toggle_collapsed()
        self.assertEqual(w.compact_visual.mode, "interval")
        self.assertEqual(w.compact_visual.label, "下午工作")
        self.assertEqual(w.compact_visual.clock, "2:00:00")
        self.assertEqual(w.compact_visual.color, interval["color"])
        self.assertAlmostEqual(w.compact_visual.progress, 2 / 5)
        self.assertEqual(list(w.compact_window.actions), ["expand"])
        self.assertEqual(w.compact_window.anchor(), anchor)
        with mock.patch.object(desktop_app.time, "monotonic", return_value=received + 17), \
             mock.patch.object(w, "_refresh_time_interval_state") as fetch:
            w.update_personal_time()
        self.assertEqual(w.compact_visual.clock, "1:59:43")
        fetch.assert_called_once()
        self.root.after_cancel(w.personal_time_job)
        w.personal_time_job = None
        self.capture("14-none-compact-interval")
        w.toggle_collapsed()
        self.root.update()
        self.assertFalse(w.timer_content.winfo_ismapped())
        self.assertTrue(w.personal_time_card.winfo_ismapped())
        self.assertEqual((w.collapse_button.winfo_rootx() + w.collapse_button.winfo_width() // 2,
                          w.collapse_button.winfo_rooty() + w.collapse_button.winfo_height() // 2), anchor)

    def test_overview_shows_white_current_clock_with_or_without_configured_intervals(self):
        w = self.window
        self._set_interval(current=False)
        fixed = datetime(2026, 10, 9, 17, 42, 8, tzinfo=desktop_app.SHANGHAI_TZ)
        with mock.patch.object(desktop_app, "datetime", wraps=datetime) as clock:
            clock.now.return_value = fixed
            w.select_timer_mode("none")
            self.root.update()
            self.assertEqual(w.personal_time_label.cget("text"), "时间区间总览 17:42:08")
            self.assertEqual(w.personal_time_label.cget("foreground"), w.normal_color)
            w.toggle_collapsed()
            self.assertEqual(w.compact_visual.label, "时间区间总览")
            self.assertEqual(w.compact_visual.clock, "17:42")
            self.assertEqual(w.compact_visual.color, w.normal_color)
            self.assertEqual(w.compact_visual.progress, 0)
            self.assertEqual(len(w.compact_visual.intervals), 1)
            self.capture("15-none-overview")
            w.time_intervals = []
            w._sync_timer_layout()
            self.assertTrue(w.compact_visual.visible)
            self.assertEqual(w.compact_visual.clock, "17:42")
            self.assertEqual(w.compact_visual.intervals, ())
            self.assertEqual(list(w.compact_window.actions), ["expand"])
            self.assertEqual(w.compact_frame[1], 130)
            self.capture("19-empty-overview-compact")
            clock.now.return_value = fixed.replace(second=9)
            with mock.patch.object(w, "_refresh_time_interval_state"):
                w.update_personal_time()
            self.assertEqual(w.personal_time_label.cget("text"), "时间区间总览 17:42:09")
            self.assertEqual(w.compact_visual.clock, "17:42")
            self.root.after_cancel(w.personal_time_job)
            w.personal_time_job = None
            clock.now.return_value = fixed.replace(minute=43, second=0)
            with mock.patch.object(w, "_refresh_time_interval_state"):
                w.update_personal_time()
            self.assertEqual(w.compact_visual.clock, "17:43")
            self.assertEqual(w.personal_time_label.cget("text"), "时间区间总览 17:43:00")
            self.root.after_cancel(w.personal_time_job)
            w.personal_time_job = None
            w.toggle_collapsed()
            self.root.update()
            self.assertTrue(w.personal_time_card.winfo_ismapped())
            self.assertFalse(w.timer_content.winfo_ismapped())
            self.capture("20-empty-overview-expanded")
            w.select_timer_mode("sedentary")
            self.root.update()
            self.assertTrue(w.personal_time_card.winfo_ismapped())
            self.assertEqual(w.personal_time_label.cget("foreground"), w.normal_color)

    def test_none_mode_persists_without_discarding_active_pomodoro(self):
        w = self.window
        w.start_pomodoro()
        w.pomodoro.advance(4)
        w.select_timer_mode("none")
        settings = json.loads(desktop_app.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(settings["timer_mode"], "none")
        with mock.patch.object(desktop_app, "load_desktop_settings", return_value=settings):
            reopened = desktop_app.UsageStatusWindow(self.root)
        self.addCleanup(reopened.destroy)
        for attr in ("poll_job", "refresh_job", "sedentary_job", "personal_time_job", "timeline_refresh_job"):
            self.root.after_cancel(getattr(reopened, attr))
            setattr(reopened, attr, None)
        self.assertEqual(reopened._timer_view(), "none")
        self.assertTrue(reopened.pomodoro.paused)
        self.assertEqual(reopened.pomodoro.remaining, 1496)

    def test_no_tab_underline_and_same_gap_to_first_card_in_all_modes(self):
        w = self.window
        self._set_interval()
        self.assertFalse(hasattr(w, "timer_divider"))
        gaps = []
        for mode in ("sedentary", "pomodoro", "none"):
            w.select_timer_mode(mode)
            self.root.update()
            first_card = w.personal_time_card if mode == "none" else w.sedentary_card
            gaps.append(first_card.winfo_rooty() - w.timer_tabs.winfo_rooty() - w.timer_tabs.winfo_height())
            self.capture(f"17-tab-spacing-{mode}")
        self.assertEqual(gaps, [4, 4, 4])

    def test_interval_bubble_hides_zero_hour_only_and_keeps_card_format(self):
        w = self.window
        self._set_interval()
        w.select_timer_mode("none")
        received = w.time_interval_state_received_at
        w.time_interval_state["current_remaining_seconds"] = 548
        with mock.patch.object(desktop_app.time, "monotonic", return_value=received):
            w._render_personal_time()
            w.toggle_collapsed()
        self.assertEqual(w.compact_visual.clock, "09:08")
        self.assertIn("0:09:08", w.personal_time_label.cget("text"))
        self.capture("18-interval-under-hour")
        for seconds, expected in ((3599, "59:59"), (3600, "1:00:00"), (3601, "1:00:01"), (0, "00:00")):
            w.time_interval_state["current_remaining_seconds"] = seconds
            with mock.patch.object(desktop_app.time, "monotonic", return_value=received):
                w._draw_compact_timer()
            self.assertEqual(w.compact_visual.clock, expected)


@unittest.skipUnless(sys.platform == "win32", "Windows GDI+ rendering")
class TimerRenderingTests(unittest.TestCase):
    def test_sedentary_ring_starts_at_noon_and_grows_counterclockwise(self):
        self.assertEqual(ring_sweep("sedentary", 0), (-90, 0))
        self.assertEqual(ring_sweep("sedentary", .25), (-90, -90))
        self.assertEqual(ring_sweep("sedentary", .5), (-90, -180))
        frame = render_compact(TimerVisual("sedentary", "本轮在线", "05:00", .25))
        def pixel(x, y):
            return frame[2][(y * frame[0] + x) * 4:(y * frame[0] + x) * 4 + 4]
        x, y = BODY_CENTER
        # Top-left arc is amber; right side is still the slate track.
        self.assertEqual(pixel(x - 39, y - 39), bytes((11, 158, 245, 255)))
        self.assertEqual(pixel(x + RING_RADIUS, y), bytes((85, 65, 51, 255)))
        self.assertEqual(pixel(0, 0)[3], 0)
        self.assertTrue(any(0 < alpha < 255 for alpha in frame[2][3::4]))

    def test_actions_double_radius_are_compact_aligned_and_not_clipped(self):
        for mode, height, names in (("sedentary", 130, ["expand", "leave"]),
                                    ("pomodoro", 164, ["expand", "pause", "stop"])):
            frame = render_compact(TimerVisual(mode, "本轮在线" if mode == "sedentary" else "专注", "00:00", 0))
            self.assertEqual(frame[:2], (WIDTH, height))
            self.assertEqual(list(frame[3]), names)
            centers = []
            for left, top, right, bottom in frame[3].values():
                self.assertEqual(BUTTON_RADIUS, 16)
                self.assertEqual(right - left, 32)
                self.assertEqual(bottom - top, 32)
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(right, frame[0])
                self.assertLessEqual(bottom, frame[1])
                centers.append(((left + right) / 2, (top + bottom) / 2))
            self.assertEqual({point[0] for point in centers}, {EXPAND_CENTER[0]})
            self.assertEqual([p[1] for p in centers], [EXPAND_CENTER[1] + i * BUTTON_SPACING for i in range(len(names))])
            self.assertEqual(BUTTON_SPACING - BUTTON_RADIUS * 2, 4)
            if mode == "pomodoro":
                self.assertEqual(centers[1][1], BODY_CENTER[1])

    def test_compact_goal_and_next_button_align_with_orb_not_side_controls(self):
        self.assertEqual(GOAL_RECT[0], BODY_CENTER[0] - 61)
        self.assertEqual(GOAL_RECT[2], BODY_CENTER[0] + 61)
        self.assertEqual(NEXT_RECT[0::2], GOAL_RECT[0::2])
        frame = render_compact(TimerVisual("pomodoro", "已完成", "00:00", 0, "工作目标", complete=True))
        self.assertEqual(frame[3]["next"], NEXT_RECT)
        x, y = 140, 145
        self.assertEqual(frame[2][(y * frame[0] + x) * 4 + 3], 0)


if __name__ == "__main__":
    unittest.main()
