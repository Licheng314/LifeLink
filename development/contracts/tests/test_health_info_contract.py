import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HealthInfoContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = (ROOT / "life-radio-api-v1.yaml").read_text(encoding="utf-8")
        cls.fixture = json.loads((ROOT / "fixtures" / "health-info-v1.json").read_text(encoding="utf-8"))

    def test_health_info_has_distinct_authenticated_path(self):
        self.assertIn("  /v1/health:\n", self.contract)
        self.assertIn("  /v1/health-info:\n", self.contract)
        self.assertIn("operationId: getHealthInfo", self.contract)
        self.assertIn("- readBearerAuth: []", self.contract)
        self.assertIn("- registeredDeviceAuth: []", self.contract)

    def test_steps_event_and_collector_are_declared(self):
        self.assertIn("health.steps_observation", self.contract)
        self.assertIn("step_counter", self.contract)
        self.assertIn("StepsObservationPayload:", self.contract)
        self.assertIn("finalized_at:", self.contract)
        self.assertIn("interval_seconds:", self.contract)
        self.assertIn("last_activity_devices:", self.contract)
        self.assertIn("first_activity_devices:", self.contract)
        self.assertIn("last_activity_apps:", self.contract)
        self.assertIn("first_activity_apps:", self.contract)
        self.assertIn("HealthActivityReference:", self.contract)

    def test_fixture_uses_stable_counter_session(self):
        event = self.fixture["steps_observation"]
        self.assertEqual(event["event_type"], "health.steps_observation")
        self.assertEqual(event["source"]["collector"], "step_counter")
        self.assertEqual(event["payload"]["sensor_type"], "android.step_counter")
        self.assertGreaterEqual(event["payload"]["counter_value"], 0)

    def test_response_keeps_devices_separate(self):
        response = self.fixture["health_info_response"]
        self.assertEqual(response["timezone"], "Asia/Shanghai")
        self.assertIn(response["sleep"]["status"], {"estimating", "final", "insufficient_data"})
        self.assertIsInstance(response["steps"]["devices"], list)
        self.assertNotIn("total_steps", response["steps"])
        self.assertEqual(response["sleep"]["finalized_at"], response["sleep"]["estimated_end"])
        self.assertEqual(response["sleep"]["interval_seconds"], 26100)
        self.assertEqual(response["sleep"]["last_activity_apps"][0]["app_name"], "Obsidian.exe")
        self.assertEqual(response["sleep"]["first_activity_apps"][0]["platform"], "android")
        device = response["steps"]["devices"][0]
        self.assertEqual(24, len(device["hourly_steps"]))
        self.assertTrue(all(isinstance(value, int) and value >= 0 for value in device["hourly_steps"]))
        self.assertEqual(device["steps"], sum(device["hourly_steps"]))

    def test_contract_requires_fixed_hourly_steps(self):
        self.assertIn("hourly_steps:", self.contract)
        self.assertIn("minItems: 24", self.contract)
        self.assertIn("maxItems: 24", self.contract)


if __name__ == "__main__":
    unittest.main()
