import unittest

from central.report_text import generate_evening_report, generate_morning_report, generate_periodic_report


class ReportTextTests(unittest.TestCase):
    def test_morning_includes_estimated_sleep_and_top_five_usage(self):
        report = generate_morning_report({
            "business_date": "2026-08-15",
            "sleep": {
                "estimated_start": "2026-08-14T16:46:00Z",
                "estimated_end": "2026-08-15T00:12:00Z",
                "rest_seconds": 26880,
            },
            "yesterday": {
                "usage_seconds": 3600,
                "top_items": [{"name": str(i), "seconds": i * 60} for i in range(7)],
            },
            "yesterday_steps": 1200,
        })
        self.assertIn("睡眠参考区间：00:46–08:12", report)
        self.assertIn("约 7 小时 28 分钟", report)
        self.assertIn("用量较多的 5 个应用或网站", report)
        self.assertNotIn("  6. ", report)

    def test_wish_prompt_only_reminds_after_the_business_day_has_passed(self):
        wish = {
            "text": "期望发布 Life Link",
            "status": "active",
            "duration_days": 3,
            "ends_on": "2026-08-16",
            "wish_days": [
                {"business_date": "2026-08-14", "evaluation": None},
                {"business_date": "2026-08-15", "evaluation": None},
                {"business_date": "2026-08-16", "evaluation": None},
            ],
        }
        report = generate_morning_report({
            "business_date": "2026-08-15",
            "wishes": [wish],
        })

        self.assertIn("待填写：08-14（需要提醒用户填写结果）", report)
        self.assertIn("待填写：08-15（今天的进度。不需要提醒）", report)
        self.assertIn("尚未到达：08-16（不计入进度，不需要提醒）", report)
        self.assertIn("当前业务日尚未填写只表示今天的进度，不要提醒", report)

    def test_evening_uses_top_five_and_selects_then_orders_intervals(self):
        report = generate_evening_report({"business_date": "2026-08-15", "usage_seconds": 3600, "top_items": [{"name": str(i), "seconds": i * 60} for i in range(7)], "location_activity": [{"start": f"2026-08-15T0{i}:00:00Z", "end": f"2026-08-15T0{i}:10:00Z", "place": str(i), "distance_m": i * 1000} for i in range(7)]})
        self.assertIn("用量较多的 5 个应用或网站", report)
        self.assertNotIn("  6. ", report)
        self.assertLess(report.index("稳定停留在2"), report.index("稳定停留在6"))
        self.assertNotIn("稳定停留在0", report)

    def test_periodic_is_strict_half_open_and_labels_new_usage(self):
        report = generate_periodic_report({"business_date": "2026-08-15", "from": "2026-08-15T14:00:00Z", "to": "2026-08-15T16:00:00Z", "usage_seconds": 2760, "events": [{"occurred_at": "2026-08-15T13:59:00Z", "title": "旧事件"}, {"occurred_at": "2026-08-15T15:59:00Z", "title": "新事件"}, {"occurred_at": "2026-08-15T16:00:00Z", "title": "边界事件"}]})
        self.assertIn("（Life Link 业务日）", report)
        self.assertIn("所有设备新增使用 46 分钟", report)
        self.assertIn("新事件", report)
        self.assertNotIn("旧事件", report)
        self.assertNotIn("边界事件", report)

    def test_reports_do_not_repeat_stable_ai_explanation(self):
        report = generate_evening_report({"business_date": "2026-08-15"})
        self.assertNotIn("AI 理解说明", report)
        self.assertNotIn("AFK", report)

    def test_important_events_prefer_wish_for_same_fact(self):
        report = generate_evening_report({"business_date": "2026-08-15", "events": [{"occurred_at": "2026-08-15T12:00:00Z", "title": "设备使用", "fact_key": "usage-120", "importance": "normal"}, {"occurred_at": "2026-08-15T12:01:00Z", "title": "心愿提醒·设备使用", "fact_key": "usage-120", "importance": "high", "wish_id": "w"}]})
        self.assertIn("心愿提醒·设备使用", report)
        self.assertNotIn("- 12:00 设备使用", report)


if __name__ == "__main__":
    unittest.main()
