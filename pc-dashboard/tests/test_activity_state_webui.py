import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ActivityStateWebUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "web" / "scripts" / "location.js").read_text(encoding="utf-8")

    def test_empty_dual_source_result_hides_chart_but_keeps_explicit_list_state(self):
        self.assertIn("activity-state-chart-panel", self.source)
        self.assertIn("activity-state-list-panel", self.source)
        self.assertIn("chartPanel.hidden = !hasActivity", self.source)
        self.assertIn("listPanel.hidden = false", self.source)
        self.assertIn("没有活动状态记录", self.source)

    def test_empty_result_explains_missing_steps_or_location(self):
        self.assertIn("步数或定位证据不足，暂不生成活动状态", self.source)
        self.assertIn("activity.current.end_at", self.source)


if __name__ == "__main__":
    unittest.main()
