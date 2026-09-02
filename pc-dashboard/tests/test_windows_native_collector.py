from datetime import datetime, timedelta, timezone
import unittest

from windows_native_collector import ForegroundWindow, NativeSample, WindowsNativeCollector


UTC = timezone.utc
T0 = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)


def window(name="code.exe", title="Work", hwnd=1):
    return ForegroundWindow(hwnd, name, title)


class WindowsNativeCollectorTests(unittest.TestCase):
    def collector(self):
        return WindowsNativeCollector()

    def test_application_switch_closes_one_interval_not_ticks(self):
        collector = WindowsNativeCollector(max_sample_gap_seconds=120)
        self.assertEqual([], collector.observe(NativeSample(window(), 0), T0))
        events = collector.observe(NativeSample(window("chrome.exe", "Life Link", 2), 0), T0 + timedelta(seconds=30))
        self.assertEqual(1, len(events))
        self.assertEqual("app.foreground", events[0]["event_type"])
        self.assertEqual(30, events[0]["duration_seconds"])
        self.assertEqual("code.exe", events[0]["payload"]["app"]["process_name"])
        self.assertEqual(
            {"kind": "desktop", "collector": "windows_native", "reliability": "observed"},
            events[0]["source"],
        )
        self.assertNotIn("hwnd", events[0]["payload"])
        self.assertNotIn("title", events[0]["payload"])

    def test_three_minute_afk_boundary_is_exact(self):
        collector = self.collector()
        collector.observe(NativeSample(window(), 0), T0)
        events = collector.observe(NativeSample(window(), 180), T0 + timedelta(seconds=180))
        states = [event for event in events if event["event_type"] == "device.input_state"]
        self.assertEqual(1, len(states))
        self.assertEqual("active", states[0]["payload"]["status"])
        self.assertEqual(180, states[0]["payload"]["idle_threshold_seconds"])
        self.assertEqual(180, states[0]["duration_seconds"])

    def test_input_recovery_closes_afk(self):
        collector = self.collector()
        collector.observe(NativeSample(window(), 0), T0)
        collector.observe(NativeSample(window(), 190), T0 + timedelta(seconds=190))
        events = collector.observe(NativeSample(window(), 0), T0 + timedelta(seconds=250))
        afk = next(event for event in events if event["event_type"] == "device.input_state")
        self.assertEqual("afk", afk["payload"]["status"])
        self.assertEqual(70, afk["duration_seconds"])

    def test_lock_and_failure_close_foreground_and_make_state_explicit(self):
        collector = self.collector()
        collector.observe(NativeSample(window(), 0), T0)
        locked = collector.observe(NativeSample(locked=True), T0 + timedelta(seconds=10))
        self.assertEqual(["app.foreground", "device.input_state"], [event["event_type"] for event in locked])
        unavailable = collector.observe(NativeSample(available=False), T0 + timedelta(seconds=20))
        self.assertEqual("locked", unavailable[0]["payload"]["status"])
        self.assertIsNone(collector.checkpoint()["input"])

    def test_long_gap_does_not_invent_sleep_usage(self):
        collector = self.collector()
        collector.observe(NativeSample(window(), 0), T0)
        events = collector.observe(NativeSample(window(), 0), T0 + timedelta(minutes=10))
        self.assertEqual(2, len(events))
        self.assertEqual(0, events[0]["duration_seconds"])
        self.assertEqual(0, events[1]["duration_seconds"])
        # The new foreground session starts after wake, not before the gap.
        final = collector.flush(T0 + timedelta(minutes=10, seconds=20))
        self.assertEqual(20, final[0]["duration_seconds"])

    def test_checkpoint_restore_preserves_event_id(self):
        original = self.collector()
        original.observe(NativeSample(window(), 0), T0)
        restored = WindowsNativeCollector.from_checkpoint(original.checkpoint(), max_sample_gap_seconds=120)
        first = original.flush(T0 + timedelta(seconds=20))[0]
        second = restored.flush(T0 + timedelta(seconds=20))[0]
        self.assertEqual(first["event_id"], second["event_id"])

    def test_snapshots_keep_id_and_extend_revision_without_closing(self):
        collector = self.collector()
        collector.observe(NativeSample(window(), 0), T0)
        first = next(event for event in collector.snapshot(T0 + timedelta(seconds=10)) if event["event_type"] == "app.foreground")
        second = next(event for event in collector.snapshot(T0 + timedelta(seconds=25)) if event["event_type"] == "app.foreground")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(10, first["duration_seconds"])
        self.assertEqual(25, second["duration_seconds"])
        self.assertEqual(first["revision"] + 1, second["revision"])
        closed = next(event for event in collector.flush(T0 + timedelta(seconds=30)) if event["event_type"] == "app.foreground")
        self.assertEqual(second["event_id"], closed["event_id"])
        self.assertEqual(second["revision"] + 1, closed["revision"])

    def test_process_path_is_reduced_to_basename(self):
        self.assertEqual("secret.exe", window(r"C:\\private\\secret.exe").process_name)


if __name__ == "__main__":
    unittest.main()
