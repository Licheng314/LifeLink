import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeviceManagementContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openapi = (ROOT / "life-radio-api-v1.yaml").read_text(encoding="utf-8")
        cls.delivery = (ROOT / "central-delivery-v1.md").read_text(encoding="utf-8")

    def test_v19_declares_management_paths_and_post_compatibility(self):
        self.assertIn("version: 1.15.5", self.openapi)
        for marker in (
            "/v1/devices:",
            "/v1/devices/{device_id}:",
            "/v1/devices/{device_id}/delete:",
            "operationId: renameDevice",
            "operationId: renameDeviceViaPost",
            "operationId: retireDevice",
            "operationId: retireDeviceViaPost",
        ):
            self.assertIn(marker, self.openapi)

    def test_alias_and_retirement_shapes_are_strict(self):
        for marker in (
            "ManagedDevice:", "ManagedDeviceListResponse:", "DeviceNamePatch:",
            "required: [device_id, platform, display_name, reported_name, custom_name, is_current, first_seen_at, last_seen_at]",
            "additionalProperties: false",
            "device_display_name:",
            "cannot_delete_current_device",
            "credential_source_not_mutable",
        ):
            self.assertIn(marker, self.openapi)

    def test_delivery_semantics_keep_identity_and_facts(self):
        for marker in (
            "custom_name",
            "retired_at",
            "不得删除原始事件",
            "重新领取邀请",
        ):
            self.assertIn(marker, self.delivery)


if __name__ == "__main__":
    unittest.main()
