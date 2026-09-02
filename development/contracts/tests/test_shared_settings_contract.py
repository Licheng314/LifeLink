from __future__ import annotations

import unittest
from pathlib import Path


OPENAPI_PATH = Path(__file__).resolve().parents[1] / "life-radio-api-v1.yaml"


def yaml_block(text: str, marker: str, indent: int) -> str:
    lines = text.splitlines()
    start = lines.index(" " * indent + marker)
    selected = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        selected.append(line)
    return "\n".join(selected)


class SharedSettingsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openapi = OPENAPI_PATH.read_text(encoding="utf-8")

    def test_shared_settings_endpoint_has_read_and_registered_device_write_boundaries(self):
        endpoint = yaml_block(self.openapi, "/v1/settings/shared:", 2)
        self.assertIn("operationId: getSharedSettings", endpoint)
        self.assertIn("operationId: updateSharedSettings", endpoint)
        self.assertIn("operationId: updateSharedSettingsViaPost", endpoint)
        self.assertIn("会阻断 PATCH 的 HTTPS 映射", endpoint)
        self.assertIn("- readBearerAuth: []", endpoint)
        self.assertIn("- registeredDeviceAuth: []", endpoint)
        self.assertIn("任一已注册设备可修改", self.openapi)
        self.assertNotIn("dashboard scope 设备", endpoint)
        self.assertNotIn("'403':", endpoint)

    def test_contract_version_records_the_backward_compatible_addition(self):
        self.assertIn("version: 1.15.5", self.openapi)

    def test_public_response_and_patch_schema_are_closed_and_typed(self):
        response = yaml_block(self.openapi, "SharedSettings:", 4)
        patch = yaml_block(self.openapi, "SharedSettingsPatch:", 4)
        self.assertIn("additionalProperties: false", response)
        self.assertIn("required: [timezone, day_start_hour, primary_health_device_id, sleep_local_time, ai_display_name, morning_report, evening_report, periodic_summary, settings_version, updated_at]", response)
        self.assertIn("const: Asia/Shanghai", response)
        self.assertIn("minimum: 1", response)
        self.assertIn("additionalProperties: false", patch)
        self.assertIn("minProperties: 1", patch)
        self.assertIn("primary_health_device_id:", patch)
        self.assertIn("type: integer", patch)
        self.assertIn("minimum: 0", patch)
        self.assertIn("maximum: 23", patch)

    def test_v113_scheduler_settings_are_strict_and_use_fixed_intervals(self):
        response = yaml_block(self.openapi, "SharedSettings:", 4)
        patch = yaml_block(self.openapi, "SharedSettingsPatch:", 4)
        morning = yaml_block(self.openapi, "MorningReportSchedule:", 4)
        periodic = yaml_block(self.openapi, "PeriodicSummarySchedule:", 4)
        for block in (response, patch):
            self.assertIn("sleep_local_time", block)
            self.assertIn("morning_report", block)
            self.assertIn("evening_report", block)
            self.assertIn("periodic_summary", block)
        self.assertIn("ai_display_name", response)
        self.assertNotIn("ai_display_name", patch)
        self.assertIn("Read-only projection", response)
        self.assertIn("enum: [after_first_usage, fixed_time]", morning)
        self.assertIn("enum: [30, 60, 120, 180, 240]", periodic)
        self.assertIn("default: false", morning)
        self.assertIn("default: false", periodic)
        self.assertIn("任一共享设置实际变化时递增", response)
        self.assertIn("完全相同的重写保持 settings_version 和 updated_at 不变", response)


if __name__ == "__main__":
    unittest.main()
