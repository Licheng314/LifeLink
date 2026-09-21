import json
import unittest
from pathlib import Path


CONTRACTS = Path(__file__).resolve().parents[1]


class PhotoSyncContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openapi = (CONTRACTS / "life-radio-api-v1.yaml").read_text(encoding="utf-8")
        cls.fixture = json.loads((CONTRACTS / "fixtures" / "photo-sync-v1.json").read_text(encoding="utf-8"))

    def test_contract_exposes_source_scoped_binary_sync_and_compat_delete(self):
        for marker in (
            "version: 1.17.1",
            "/v1/photos:",
            "/v1/photos/{photo_id}/content:",
            "/v1/photos/{photo_id}:",
            "/v1/photos/{photo_id}/delete:",
            "/v1/photos/sync-complete:",
            "X-Photo-Sync-Id",
            "X-Photo-Captured-At",
            "X-Photo-Source-Label",
            "current_business_date_added_count",
            "AI 不获得图片二进制或 URL",
        ):
            self.assertIn(marker, self.openapi)

    def test_fixture_is_non_secret_metadata_only(self):
        serialized = json.dumps(self.fixture, ensure_ascii=False)
        self.assertEqual(self.fixture["sync_id"], self.fixture["upload"]["headers"]["X-Photo-Sync-Id"])
        self.assertNotIn('"authorization"', serialized.lower())
        self.assertNotIn('"device_token"', serialized.lower())
        self.assertNotIn("https://", serialized.lower())
        self.assertNotIn("data:image", serialized.lower())
        self.assertEqual(self.fixture["upload"]["headers"]["X-Photo-Source-Label"], "%E6%88%AA%E5%9B%BE")
        self.assertEqual(self.fixture["complete_response"]["added_count"], 1)


if __name__ == "__main__":
    unittest.main()
