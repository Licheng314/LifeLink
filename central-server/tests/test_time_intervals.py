import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from central.storage import CentralStore


class DailyTimeIntervalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.temp.name) / "central.sqlite3", {})

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **changes):
        value = {
            "name": "专注", "start_local_time": "09:00", "end_local_time": "10:00",
            "color": "#2563EB", "tag": None, "foreground_display": True,
        }
        value.update(changes)
        return self.store.create_time_interval(value)

    def clear_compatibility_intervals(self):
        for item in self.store.list_time_intervals():
            self.store.delete_time_interval(item["interval_id"])

    def test_migrates_sleep_anchor_and_personal_time_once(self):
        first = self.store.list_time_intervals()
        self.assertEqual([(item["name"], item["compatibility_source"]) for item in first], [
            ("个人时光", "personal_time"), ("睡眠时间", "sleep"),
        ])
        reopened = CentralStore(Path(self.temp.name) / "central.sqlite3", {})
        self.assertEqual(len(reopened.list_time_intervals()), 2)

    def test_rejects_overlap_including_a_business_day_crossing_interval(self):
        self.clear_compatibility_intervals()
        self.create(name="夜间", start_local_time="22:00", end_local_time="02:00")
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            self.create(name="凌晨", start_local_time="01:00", end_local_time="03:00")
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            self.create(name="夜晚", start_local_time="21:00", end_local_time="23:00")

    def test_zero_duration_anchor_does_not_occupy_the_day(self):
        self.clear_compatibility_intervals()
        anchor = self.create(name="休息", start_local_time="22:00", end_local_time="22:00", tag="alert")
        interval = self.create(name="夜间", start_local_time="21:00", end_local_time="23:00")
        self.assertTrue(anchor["is_time_anchor"])
        self.assertFalse(interval["is_time_anchor"])

    def test_current_and_next_state_use_the_central_local_clock(self):
        self.clear_compatibility_intervals()
        interval = self.create(name="阅读", start_local_time="22:00", end_local_time="01:00")
        active = self.store.time_interval_state(now=datetime(2026, 9, 7, 15, 30, tzinfo=timezone.utc))
        self.assertEqual(active["current"]["interval_id"], interval["interval_id"])
        self.assertEqual(active["current_remaining_seconds"], 90 * 60)
        upcoming = self.store.time_interval_state(now=datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc))
        self.assertIsNone(upcoming["current"])
        self.assertEqual(upcoming["next"]["interval_id"], interval["interval_id"])

    def test_update_rechecks_overlap_and_delete_is_idempotent_at_the_api_boundary(self):
        self.clear_compatibility_intervals()
        first = self.create(name="上午", start_local_time="09:00", end_local_time="10:00")
        second = self.create(name="下午", start_local_time="14:00", end_local_time="15:00")
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            self.store.update_time_interval(second["interval_id"], {"start_local_time": "09:30"})
        self.assertTrue(self.store.delete_time_interval(first["interval_id"]))
        self.assertFalse(self.store.delete_time_interval(first["interval_id"]))

    def test_sleep_compatibility_interval_can_be_expanded_and_keeps_its_reminder_start(self):
        sleep = next(item for item in self.store.list_time_intervals() if item["compatibility_source"] == "sleep")
        updated = self.store.update_time_interval(sleep["interval_id"], {"start_local_time": "23:00", "end_local_time": "07:10"})
        self.assertFalse(updated["is_time_anchor"])
        self.assertEqual(updated["end_local_time"], "07:10")
        self.assertEqual(self.store.get_shared_settings()["sleep_local_time"], "23:00")
