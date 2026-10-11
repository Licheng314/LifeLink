import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pomodoro import PomodoroTimer, load_timer, save_timer


class PomodoroTests(unittest.TestCase):
    def test_focus_rest_and_one_completion_event(self):
        timer = PomodoroTimer(focus_seconds=5, rest_seconds=2)
        timer.start("完成接口测试")
        self.assertEqual(timer.advance(4), [])
        self.assertEqual(timer.display_seconds, 1)
        self.assertEqual(timer.advance(1), ["rest"])
        self.assertEqual(timer.remaining, 2)
        event = timer.pending_events[0]
        self.assertEqual(event["metadata"]["goal"], "完成接口测试")
        self.assertEqual(event["event_id"], timer.session_id)
        self.assertEqual(timer.advance(2), ["complete"])
        self.assertEqual(timer.advance(2), [])
        self.assertEqual(len(timer.pending_events), 1)

    def test_paused_time_does_not_count_and_resume_is_explicit(self):
        timer = PomodoroTimer()
        timer.start("")
        timer.advance(5)
        self.assertEqual(timer.advance(1, blocked=True), ["paused"])
        timer.advance(5, blocked=False)
        self.assertEqual(timer.remaining, 1495)
        timer.resume()
        timer.advance(1)
        self.assertEqual(timer.remaining, 1494)

    def test_sleep_gap_pauses_without_completing_work(self):
        timer = PomodoroTimer()
        timer.start("写作")
        self.assertEqual(timer.advance(3600), ["paused"])
        self.assertEqual(timer.phase, "focus")
        self.assertEqual(timer.remaining, 1500)
        self.assertFalse(timer.pending_events)

    def test_phase_boundary_uses_leftover_seconds_for_rest(self):
        timer = PomodoroTimer(focus_seconds=3, rest_seconds=5)
        timer.start("")
        self.assertEqual(timer.advance(4), ["rest"])
        self.assertEqual(timer.remaining, 4)

    def test_stop_is_not_completion_and_keeps_goal_for_next_round(self):
        timer = PomodoroTimer()
        timer.start("写初稿")
        timer.advance(4)
        timer.stop()
        self.assertEqual(timer.goal, "写初稿")
        self.assertFalse(timer.pending_events)
        timer.start(timer.goal)
        self.assertEqual(timer.display_seconds, 1500)

    def test_active_session_cannot_be_silently_replaced(self):
        timer = PomodoroTimer()
        timer.start("任务一")
        with self.assertRaises(ValueError):
            timer.start("任务二")
        self.assertEqual(timer.goal, "任务一")

    def test_restart_restores_paused_focus_and_durable_pending_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pomodoro.json"
            timer = PomodoroTimer()
            timer.start("上一轮目标", datetime(2026, 10, 8, 1, tzinfo=timezone.utc))
            timer.advance(5)
            save_timer(path, timer)
            restored = load_timer(path)
            self.assertTrue(restored.paused)
            self.assertEqual(restored.remaining, 1495)
            self.assertEqual(restored.session_id, timer.session_id)
            restored.resume()
            restored.remaining = 1
            restored.advance(1)
            save_timer(path, restored)
            resting = load_timer(path)
            self.assertEqual(resting.phase, "rest")
            self.assertTrue(resting.paused)
            self.assertEqual(resting.pending_events, restored.pending_events)

    def test_invalid_state_never_creates_a_completed_session(self):
        timer = PomodoroTimer.restore({"phase": "focus", "remaining": float("nan"), "session_id": "bad"})
        self.assertEqual(timer.phase, "idle")
        self.assertFalse(timer.pending_events)

    def test_restart_completed_round_is_ready_and_keeps_goal_and_pending_delivery(self):
        timer = PomodoroTimer(focus_seconds=1, rest_seconds=1)
        timer.start("保留上一轮目标")
        timer.advance(2)
        self.assertEqual(timer.phase, "complete")
        restored = PomodoroTimer.restore(timer.snapshot())
        self.assertEqual(restored.phase, "idle")
        self.assertEqual(restored.goal, timer.goal)
        self.assertEqual(restored.pending_events, timer.pending_events)
        self.assertFalse(restored.active)
        restored.start(restored.goal)
        self.assertEqual(restored.display_seconds, 1500)

    def test_countdown_rounds_up_fractional_seconds(self):
        timer = PomodoroTimer()
        timer.start("目标\n第二行")
        timer.advance(.1)
        self.assertEqual(timer.display_seconds, 1500)
        self.assertEqual(timer.goal, "目标 第二行")


if __name__ == "__main__":
    unittest.main()
