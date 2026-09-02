from __future__ import annotations

import re
import unittest
from pathlib import Path


CONTRACTS_DIR = Path(__file__).resolve().parents[1]
OPENAPI_PATH = CONTRACTS_DIR / "life-radio-api-v1.yaml"
SEMANTICS_PATH = CONTRACTS_DIR / "central-delivery-v1.md"


def yaml_block(text: str, marker: str, indent: int) -> str:
    """Return one indentation-delimited YAML mapping block without a parser."""
    lines = text.splitlines()
    try:
        start = lines.index(" " * indent + marker)
    except ValueError as error:
        raise AssertionError(f"missing YAML marker: {marker}") from error
    selected = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        selected.append(line)
    return "\n".join(selected)


class CentralReadMvpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = OPENAPI_PATH.read_text(encoding="utf-8")
        cls.semantics = SEMANTICS_PATH.read_text(encoding="utf-8")

    def test_read_operations_use_only_the_independent_read_scheme(self) -> None:
        for path in ("/v1/read/devices:", "/v1/read/usage:"):
            with self.subTest(path=path):
                block = yaml_block(self.openapi, path, 2)
                self.assertIn("- readBearerAuth: []", block)
                self.assertNotIn("- bearerAuth: []", block)
                self.assertIn("#/components/parameters/ReadFrom", block)
                self.assertIn("#/components/parameters/ReadTo", block)
                self.assertIn("#/components/parameters/LocalDeviceId", block)
                self.assertIn("'401':", block)
                self.assertIn("'403':", block)

        upload = yaml_block(self.openapi, "/v1/events/batches:", 2)
        self.assertIn("- bearerAuth: []", upload)
        self.assertNotIn("readBearerAuth", upload)

    def test_window_parameters_are_required_utc_and_to_is_exclusive(self) -> None:
        read_from = yaml_block(self.openapi, "ReadFrom:", 4)
        read_to = yaml_block(self.openapi, "ReadTo:", 4)
        local = yaml_block(self.openapi, "LocalDeviceId:", 4)

        for block in (read_from, read_to):
            self.assertIn("in: query", block)
            self.assertIn("required: true", block)
            self.assertIn("format: date-time", block)
            self.assertIn("UTC", block)
            self.assertIn("Z", block)
        self.assertIn("排他", read_to)
        self.assertIn("晚于 from", read_to)
        self.assertIn("required: false", local)
        self.assertIn("仅用于计算 is_local", local)
        self.assertIn("不筛选", local)

    def test_device_response_can_back_the_existing_device_cards(self) -> None:
        response = yaml_block(self.openapi, "ReadDevicesResponse:", 4)
        device = yaml_block(self.openapi, "ReadDeviceSummary:", 4)
        stats = yaml_block(self.openapi, "DeviceWindowStats:", 4)

        self.assertIn("required: [window, generated_at, online_window_seconds, devices]", response)
        for field in (
            "device_key:", "device_id:", "display_name:", "platform:",
            "is_local:", "status:", "last_seen_at:", "last_received_at:",
            "window:", "today:",
        ):
            self.assertIn(field, device)
        self.assertIn("不代表设备此刻在线或可访问", device)
        self.assertIn("required: [event_count, batch_count, categories]", stats)

    def test_usage_response_contains_every_existing_dashboard_aggregate(self) -> None:
        response = yaml_block(self.openapi, "ReadUsageResponse:", 4)
        aggregate = yaml_block(self.openapi, "UsageAggregate:", 4)
        device = yaml_block(self.openapi, "ReadUsageDevice:", 4)

        self.assertIn("required: [window, generated_at, timezone, day_start_hour, devices, all]", response)
        for field in (
            "events:", "window_events:", "web_events:", "afk_seconds:",
            "apps:", "hourly:", "hourly_apps:", "hourly_online:",
            "sites:", "hourly_sites:",
        ):
            self.assertIn(field, aggregate)
        self.assertIn("deprecated: true", aggregate)
        self.assertIn("值与 hourly 相同", aggregate)
        for field in (
            "device_key:", "device_id:", "display_name:", "platform:",
            "is_local:",
        ):
            self.assertIn(field, device)

    def test_all_local_component_references_have_targets(self) -> None:
        references = re.findall(
            r"\$ref: '#/components/(schemas|parameters)/([A-Za-z0-9]+)'",
            self.openapi,
        )
        self.assertTrue(references)
        for section, name in references:
            with self.subTest(section=section, name=name):
                self.assertRegex(
                    self.openapi,
                    rf"(?m)^    {re.escape(name)}:$" if section == "schemas"
                    else rf"(?m)^    {re.escape(name)}:$",
                )

    def test_semantics_document_records_security_and_freshness_boundaries(self) -> None:
        required_statements = (
            "## 中央只读 MVP",
            "设备上传 Token 不具备任何中央读取权限",
            "`from`：必填、包含式窗口起点",
            "`to`：必填、排他式窗口终点",
            "持续事件只要与 `[from,to)` 相交就可参与统计",
            "只表示最后同步时间，不证明设备当前联网",
            "只聚合中央库中 `event_type=app.foreground`",
            "网站标记不是独立可累加的真实用时",
            "剪去明确 `status=afk` 的重叠部分",
            "MVP 只覆盖设备和用量读取",
        )
        for statement in required_statements:
            with self.subTest(statement=statement):
                self.assertIn(statement, self.semantics)


if __name__ == "__main__":
    unittest.main()
