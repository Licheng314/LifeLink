import unittest
from pathlib import Path


OPENAPI = (Path(__file__).resolve().parents[1] / "life-radio-api-v1.yaml").read_text(encoding="utf-8")


class ActivityStateContractTests(unittest.TestCase):
    def test_location_read_exposes_business_window_activity_projection(self):
        for marker in (
            "/v1/read/locations:", "ActivityStateProjection:", "ActivityInterval:",
            "enum: [stationary, walking, running, transport]", "primary_health_device_id:",
            "distance_source:", "is_current:", "address:", "latitude:", "longitude:",
        ):
            self.assertIn(marker, OPENAPI)

    def test_activity_is_read_only_and_uses_existing_read_boundary(self):
        block = OPENAPI.split("  /v1/read/locations:", 1)[1].split("  /v1/devices:", 1)[0]
        self.assertIn("- readBearerAuth: []", block)
        self.assertNotIn("post:", block)
        self.assertNotIn("patch:", block)


if __name__ == "__main__":
    unittest.main()
