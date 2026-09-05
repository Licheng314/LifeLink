import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import desktop_app
import pc_windows_startup

from desktop_app import (
    LifeRadioDesktopApp,
    SedentaryTimer,
    WindowsInputActivityDetector,
    business_day_timeline_url,
    sedentary_status_text,
    TIMELINE_FETCH_FAILED,
    TIMELINE_UNCHANGED,
    visible_timeline_events,
)


class FakeClock:
    def __init__(self, value=0):
        self.value = float(value)

    def __call__(self):
        return self.value


class SedentaryTimerTests(unittest.TestCase):
    def test_tray_uses_the_packaged_client_icon(self):
        self.assertEqual(
            desktop_app.TRAY_ICON_FILE,
            Path(desktop_app.__file__).resolve().parent
            / "assets" / "life-link-client-tray.ico",
        )
        self.assertTrue(desktop_app.TRAY_ICON_FILE.is_file())
        self.assertEqual(desktop_app.TRAY_ICON_FILE.read_bytes()[:4], b"\x00\x00\x01\x00")
        source = Path(desktop_app.__file__).read_text(encoding="utf-8")
        self.assertIn("LoadImageW", source)

    def test_repeats_notifications_at_multiples_while_progress_stays_full(self):
        timer = SedentaryTimer(limit_seconds=10, afk_reset_seconds=5)

        self.assertFalse(timer.advance("active", 9))
        self.assertTrue(timer.advance("active", 1))
        self.assertTrue(timer.reminder_active)
        self.assertEqual(timer.online_progress, 1)

        self.assertTrue(timer.advance("active", 10))
        self.assertEqual(timer.online_progress, 1)
        self.assertEqual(timer.next_notification_multiple, 3)

        timer.acknowledge()
        self.assertEqual(timer.active_seconds, 0)
        self.assertFalse(timer.reminder_active)
        self.assertEqual(timer.next_notification_multiple, 1)

    def test_short_afk_pauses_then_active_time_continues(self):
        timer = SedentaryTimer(limit_seconds=60, afk_reset_seconds=300)
        timer.advance("active", 30)
        timer.advance("afk", 100, observed_afk_seconds=100)

        self.assertEqual(timer.active_seconds, 30)
        self.assertAlmostEqual(timer.afk_progress, 1 / 3)

        timer.advance("active", 10)
        self.assertEqual(timer.active_seconds, 40)
        self.assertEqual(timer.afk_seconds, 0)

    def test_five_minutes_afk_resets_time_and_active_reminder(self):
        timer = SedentaryTimer(limit_seconds=60, afk_reset_seconds=300)
        self.assertTrue(timer.advance("active", 60))

        timer.advance("afk", 1, observed_afk_seconds=300)

        self.assertEqual(timer.active_seconds, 0)
        self.assertFalse(timer.reminder_active)
        self.assertEqual(timer.afk_progress, 1)
        self.assertEqual(timer.next_notification_multiple, 1)

    def test_unknown_state_never_advances_timer(self):
        timer = SedentaryTimer(limit_seconds=60, afk_reset_seconds=300)
        timer.advance("unknown", 30)
        self.assertEqual(timer.active_seconds, 0)

    def test_status_text_switches_to_observed_afk_duration(self):
        timer = SedentaryTimer(limit_seconds=60, afk_reset_seconds=300)
        timer.advance("active", 30)
        self.assertEqual(sedentary_status_text(timer, "active"), "本轮在线 00:30")

        timer.advance("afk", 1, observed_afk_seconds=40)
        self.assertEqual(sedentary_status_text(timer, "afk"), "AFK 时间 00:40")


class WindowsInputActivityDetectorTests(unittest.TestCase):
    def test_three_minute_input_tolerance_then_afk_starts_at_zero(self):
        clock = FakeClock()
        idle = FakeClock()
        detector = WindowsInputActivityDetector(
            tolerance_seconds=180,
            idle_reader=idle,
            monotonic_clock=clock,
        )

        clock.value = idle.value = 179
        self.assertEqual(detector.sample(), ("active", 0))

        clock.value = idle.value = 180
        self.assertEqual(detector.sample(), ("afk", 0))

        clock.value = idle.value = 240
        self.assertEqual(detector.sample(), ("afk", 60))

    def test_process_start_does_not_inherit_existing_windows_idle_time(self):
        clock = FakeClock(1_000)
        idle = FakeClock(600)
        detector = WindowsInputActivityDetector(
            tolerance_seconds=180,
            idle_reader=idle,
            monotonic_clock=clock,
        )

        self.assertEqual(detector.sample(), ("active", 0))
        clock.value += 60
        idle.value += 60
        self.assertEqual(detector.sample(), ("active", 0))

        clock.value += 120
        idle.value += 120
        self.assertEqual(detector.sample(), ("afk", 0))

    def test_keyboard_or_mouse_input_immediately_returns_to_active(self):
        clock = FakeClock()
        idle = FakeClock()
        detector = WindowsInputActivityDetector(
            tolerance_seconds=180,
            idle_reader=idle,
            monotonic_clock=clock,
        )

        clock.value = idle.value = 240
        self.assertEqual(detector.sample(), ("afk", 60))

        clock.value = 241
        idle.value = 0
        self.assertEqual(detector.sample(), ("active", 0))


class DesktopStartupRecoveryTests(unittest.TestCase):
    def test_packaged_client_allows_a_longer_sync_server_start_window(self):
        self.assertGreaterEqual(desktop_app.SYNC_SERVER_START_TIMEOUT_SECONDS, 20)

    def test_login_startup_uses_formal_background_executable(self):
        command = pc_windows_startup.startup_command(
            Path("C:/Life Link/pc-dashboard/LifeLink PC Client.exe"),
            environ={"WINDIR": "C:/Windows"},
        )
        self.assertEqual(
            command,
            '"C:\\Life Link\\pc-dashboard\\LifeLink PC Client.exe"',
        )
        self.assertNotIn("token", command.lower())

    @mock.patch.object(pc_windows_startup, "_windows_blocked", return_value=False)
    @mock.patch.object(pc_windows_startup, "_preference", return_value=True)
    @mock.patch.object(pc_windows_startup, "shortcut_path", return_value=Path("missing.lnk"))
    def test_missing_requested_startup_is_reported_separately(
        self, _shortcut, _preference, _blocked,
    ):
        state = pc_windows_startup.status()
        self.assertEqual(state["state"], "missing")
        self.assertTrue(state["requested_enabled"])
        self.assertFalse(state["enabled"])

    @mock.patch.object(pc_windows_startup, "_windows_blocked", return_value=True)
    @mock.patch.object(pc_windows_startup, "_preference", return_value=True)
    @mock.patch.object(pc_windows_startup, "shortcut_path", return_value=Path("missing.lnk"))
    def test_windows_block_is_visible_even_if_manager_removed_shortcut(
        self, _shortcut, _preference, _blocked,
    ):
        state = pc_windows_startup.status()
        self.assertEqual(state["state"], "blocked")
        self.assertTrue(state["blocked_by_windows"])

    def test_shortcut_creation_cannot_succeed_without_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "LifeLink PC Client.exe"
            target.touch()
            with (
                mock.patch.object(pc_windows_startup, "startup_target", return_value=target),
                mock.patch.object(pc_windows_startup.subprocess, "run"),
            ):
                with self.assertRaisesRegex(RuntimeError, "启动项未生成"):
                    pc_windows_startup._create_shortcut(
                        environ={"APPDATA": directory},
                    )

    @mock.patch.object(pc_windows_startup, "ensure_default_enabled", return_value=True)
    def test_startup_registration_entry_uses_generated_launcher(self, ensure):
        self.assertEqual(pc_windows_startup.main(), 0)
        ensure.assert_called_once_with()

    def test_pc_login_startup_is_managed_by_tray_menu(self):
        source = Path(desktop_app.__file__).read_text(encoding="utf-8")
        self.assertIn("ID_TOGGLE_LOGIN_STARTUP", source)
        self.assertIn("toggle_login_startup", source)
        self.assertIn('"开机启动"', source)
        self.assertIn("ensure_default_enabled()", source)

    @mock.patch.object(pc_windows_startup, "set_enabled", return_value={"enabled": False})
    @mock.patch.object(pc_windows_startup, "status", return_value={"enabled": True})
    def test_tray_toggle_changes_only_the_local_startup_preference(self, status, set_enabled):
        app = LifeRadioDesktopApp.__new__(LifeRadioDesktopApp)
        app.tray = mock.Mock()
        app.toggle_login_startup()
        status.assert_called_once_with()
        set_enabled.assert_called_once_with(False)
        app.tray.notify.assert_called_once_with(
            "Life Link PC 客户端", "已关闭开机启动", warning=False,
        )

    def test_server_log_rotation_keeps_only_the_configured_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "sync_server.log"
            log.write_bytes(b"a" * 20)
            with mock.patch.object(desktop_app, "SERVER_LOG_FILE", log), mock.patch.object(desktop_app, "SERVER_LOG_MAX_BYTES", 8), mock.patch.object(desktop_app, "SERVER_LOG_BACKUP_COUNT", 2):
                desktop_app.rotate_server_log_if_needed()
            self.assertFalse(log.exists())
            self.assertEqual((Path(directory) / "sync_server.log.1").read_bytes(), b"a" * 8)

    def test_second_launch_repairs_missing_server_before_opening_dashboard(self):
        app = LifeRadioDesktopApp.__new__(LifeRadioDesktopApp)
        app.acquire_single_instance = mock.Mock(return_value=False)
        app.server_is_ready = mock.Mock(return_value=False)
        app.start_server = mock.Mock()
        app.open_dashboard = mock.Mock()
        app.root = mock.Mock()

        self.assertEqual(app.run(), 0)

        app.start_server.assert_called_once_with()
        app.open_dashboard.assert_called_once_with()
        app.root.destroy.assert_called_once_with()

    def test_second_launch_does_not_duplicate_a_healthy_server(self):
        app = LifeRadioDesktopApp.__new__(LifeRadioDesktopApp)
        app.acquire_single_instance = mock.Mock(return_value=False)
        app.server_is_ready = mock.Mock(return_value=True)
        app.start_server = mock.Mock()
        app.open_dashboard = mock.Mock()
        app.root = mock.Mock()

        self.assertEqual(app.run(), 0)

        app.start_server.assert_not_called()
        app.open_dashboard.assert_called_once_with()


class DesktopTopmostControlTests(unittest.TestCase):
    def test_header_topmost_control_reuses_persisted_state_and_is_not_draggable(self):
        source = Path(desktop_app.__file__).read_text(encoding="utf-8")
        self.assertIn("self.title_topmost_check = tk.Checkbutton(", source)
        self.assertIn("titlebar, text=\"置顶\", variable=self.topmost_value", source)
        self.assertIn("command=self.apply_topmost", source)
        self.assertIn("self.title_topmost_check,", source)

    def test_status_window_has_dashboard_button_and_tray_left_click_opens_status(self):
        source = Path(desktop_app.__file__).read_text(encoding="utf-8")
        self.assertIn("text=\"打开 Dashboard\", command=self.open_dashboard", source)
        self.assertIn("self.dashboard_button.pack(fill=\"x\", pady=(7, 0))", source)
        self.assertIn("self.ack_slot, self.ack_button, self.dashboard_button", source)
        self.assertIn(
            'if lparam == self.WM_LBUTTONUP:\n                self.command_queue.put("status")',
            source,
        )
        self.assertIn('self.ID_TOGGLE_LOGIN_STARTUP, "开机启动"', source)
        self.assertIn('self.command_queue.put("toggle-login-startup")', source)
        self.assertIn('def toggle_login_startup(self) -> None:', source)


class SharedTimelineTests(unittest.TestCase):
    def test_desktop_uses_the_stable_full_business_day_window(self):
        start = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(
            business_day_timeline_url(start),
            "http://127.0.0.1:8090/api/timeline-events"
            "?from=2026-08-24T16:00:00Z&to=2026-08-25T16:00:00Z",
        )

    def test_desktop_hides_future_and_malformed_events_from_full_day_cache(self):
        now = datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc)
        past = {"timeline_event_id": "past", "occurred_at": "2026-08-25T01:59:00Z"}
        future = {"timeline_event_id": "future", "occurred_at": "2026-08-25T02:01:00Z"}
        malformed = {"timeline_event_id": "bad", "occurred_at": "not-a-time"}
        self.assertEqual(visible_timeline_events([past, future, malformed], now), [past])

    def test_failed_timeline_fetch_signals_retry_without_empty_render(self):
        app = desktop_app.UsageStatusWindow.__new__(desktop_app.UsageStatusWindow)
        app._business_day_start_utc = mock.Mock(
            return_value=datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc),
        )
        app.opener = mock.Mock()
        app.opener.open.side_effect = OSError("offline")
        app.timeline_queue = desktop_app.queue.Queue()

        app._fetch_timeline()

        self.assertIs(app.timeline_queue.get_nowait(), TIMELINE_FETCH_FAILED)

    def test_unchanged_timeline_fetch_does_not_request_a_redraw(self):
        event = {
            "timeline_event_id": "same",
            "occurred_at": "2026-08-24T17:00:00Z",
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = desktop_app.json.dumps(
            {"events": [event]},
        ).encode("utf-8")
        app = desktop_app.UsageStatusWindow.__new__(desktop_app.UsageStatusWindow)
        app._business_day_start_utc = mock.Mock(
            return_value=datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc),
        )
        app.opener = mock.Mock()
        app.opener.open.return_value = response
        app.timeline_queue = desktop_app.queue.Queue()
        app.timeline_snapshot = [event]

        with mock.patch.object(
            desktop_app, "datetime",
            wraps=desktop_app.datetime,
        ) as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
            app._fetch_timeline()

        self.assertIs(app.timeline_queue.get_nowait(), TIMELINE_UNCHANGED)

if __name__ == "__main__":
    unittest.main()
