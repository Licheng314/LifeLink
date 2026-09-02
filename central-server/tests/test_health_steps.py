import unittest

from central.health_steps import derive_steps_by_device


def observation(device, at, value, session="3f2504e0-4f89-41d3-9a0c-0305e82c3301"):
    return {"event_type": "health.steps_observation", "device_id": device,
            "occurred_at": at, "payload": {"counter_value": value, "counter_session_id": session}}


class HealthStepsTest(unittest.TestCase):
    def test_same_session_deltas_use_shanghai_later_observation_date(self):
        result = derive_steps_by_device([observation("phone", "2026-08-11T15:55:00Z", 10), observation("phone", "2026-08-11T16:05:00Z", 17)], "2026-08-12")
        self.assertEqual(7, result[0].steps)
        self.assertEqual("available", result[0].status)
        self.assertEqual(7, result[0].hourly_steps[0])
        self.assertEqual(24, len(result[0].hourly_steps))
        self.assertEqual(result[0].steps, sum(result[0].hourly_steps))

    def test_same_hour_and_cross_hour_deltas_belong_to_later_observation_hour(self):
        result = derive_steps_by_device([
            observation("phone", "2026-08-12T00:05:00Z", 10),  # 08:05
            observation("phone", "2026-08-12T00:45:00Z", 17),  # 08:45: +7
            observation("phone", "2026-08-12T03:10:00Z", 30),  # 11:10: +13
        ], "2026-08-12")[0]
        self.assertEqual(20, result.steps)
        self.assertEqual(7, result.hourly_steps[8])
        self.assertEqual(13, result.hourly_steps[11])
        self.assertEqual(result.steps, sum(result.hourly_steps))

    def test_multi_hour_and_midnight_deltas_are_not_split_or_lost(self):
        result = derive_steps_by_device([
            observation("phone", "2026-08-11T14:00:00Z", 100),  # 22:00 previous day
            observation("phone", "2026-08-11T17:30:00Z", 130),  # 01:30 target day: +30
            observation("phone", "2026-08-12T10:00:00Z", 150),  # 18:00 target day: +20
        ], "2026-08-12")[0]
        self.assertEqual(50, result.steps)
        self.assertEqual(30, result.hourly_steps[1])
        self.assertEqual(20, result.hourly_steps[18])
        self.assertEqual(result.steps, sum(result.hourly_steps))


    def test_session_reset_is_not_counted_as_a_delta(self):
        result = derive_steps_by_device([observation("phone", "2026-08-12T01:00:00Z", 100, "3f2504e0-4f89-41d3-9a0c-0305e82c3301"), observation("phone", "2026-08-12T02:00:00Z", 3, "e83d7de0-4f89-41d3-9a0c-0305e82c3301")], "2026-08-12")
        self.assertEqual("insufficient_samples", result[0].status)
        self.assertIsNone(result[0].steps)
        self.assertEqual((0,) * 24, result[0].hourly_steps)


    def test_negative_delta_is_ignored_and_warned(self):
        result = derive_steps_by_device([observation("phone", "2026-08-12T01:00:00Z", 10), observation("phone", "2026-08-12T02:00:00Z", 8)], "2026-08-12")
        self.assertIsNone(result[0].steps)
        self.assertIn("negative_counter_delta", result[0].warnings)
        self.assertEqual((0,) * 24, result[0].hourly_steps)


    def test_multiple_devices_are_reported_not_summed(self):
        events = [observation("a", "2026-08-12T01:00:00Z", 1), observation("a", "2026-08-12T02:00:00Z", 4), observation("b", "2026-08-12T01:00:00Z", 10), observation("b", "2026-08-12T02:00:00Z", 30)]
        self.assertEqual([("a", 3), ("b", 20)], [(item.device_id, item.steps) for item in derive_steps_by_device(events, "2026-08-12")])

    def test_interleaved_sessions_break_the_adjacent_baseline(self):
        session_a = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        session_b = "e83d7de0-4f89-41d3-9a0c-0305e82c3301"
        result = derive_steps_by_device([
            observation("phone", "2026-08-12T01:00:00Z", 10, session_a),
            observation("phone", "2026-08-12T02:00:00Z", 100, session_b),
            observation("phone", "2026-08-12T03:00:00Z", 17, session_a),
        ], "2026-08-12")[0]
        self.assertIsNone(result.steps)
        self.assertEqual((0,) * 24, result.hourly_steps)

    def test_invalid_observation_breaks_the_adjacent_baseline(self):
        invalid = observation("phone", "2026-08-12T02:00:00Z", 12)
        invalid["payload"] = {"counter_value": "invalid"}
        result = derive_steps_by_device([
            observation("phone", "2026-08-12T01:00:00Z", 10),
            invalid,
            observation("phone", "2026-08-12T03:00:00Z", 17),
        ], "2026-08-12")[0]
        self.assertIsNone(result.steps)
        self.assertIn("invalid_observation", result.warnings)
        self.assertEqual((0,) * 24, result.hourly_steps)
