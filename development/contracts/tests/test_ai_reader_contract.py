from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


CONTRACTS_DIR = Path(__file__).resolve().parents[1]
OPENAPI_PATH = CONTRACTS_DIR / "life-radio-api-v1.yaml"
SEMANTICS_PATH = CONTRACTS_DIR / "ai-reader-passive-read-v1.md"
CLAIM_SCHEMA_PATH = CONTRACTS_DIR / "ai-reader-pairing-claim-v1.schema.json"
CLAIM_FIXTURE_PATH = CONTRACTS_DIR / "fixtures" / "ai-reader-pairing-claim-v1.json"


def yaml_block(text: str, marker: str, indent: int) -> str:
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


class AIReaderContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = OPENAPI_PATH.read_text(encoding="utf-8")
        cls.semantics = SEMANTICS_PATH.read_text(encoding="utf-8")
        cls.claim_schema = json.loads(CLAIM_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.claim_fixture = json.loads(CLAIM_FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_version_and_pairing_claim_are_strict(self) -> None:
        self.assertIn("version: 1.15.5", self.openapi)
        self.assertEqual(self.claim_schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(self.claim_schema["additionalProperties"])
        self.assertEqual(
            self.claim_schema["required"],
            ["schema_version", "pairing_id", "reader"],
        )
        self.assertEqual(
            self.claim_schema["properties"]["schema_version"]["const"],
            "life-radio-ai-reader-pairing-claim-v1",
        )
        reader = self.claim_schema["$defs"]["reader_identity"]
        self.assertFalse(reader["additionalProperties"])
        self.assertEqual(reader["required"], ["type", "instance_id", "display_name"])
        self.assertEqual(set(self.claim_fixture), set(self.claim_schema["required"]))
        self.assertEqual(set(self.claim_fixture["reader"]), set(reader["required"]))

    def test_process_binding_is_stable_and_does_not_require_pid_or_full_path(self) -> None:
        binding = self.claim_schema["$defs"]["hosted_argument_binding"]
        self.assertEqual(
            binding["required"],
            ["strategy", "display_name", "process_name", "argument_path_segments"],
        )
        self.assertNotIn("pid", json.dumps(binding))
        self.assertNotIn("executable_path", json.dumps(binding))
        self.assertIn("`process_binding`", self.semantics)
        self.assertIn("node_modules/openclaw", self.semantics)

    def test_pairing_fixture_contains_no_secret_or_pairing_text(self) -> None:
        fixture_text = CLAIM_FIXTURE_PATH.read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(
                r"(?i)(authorization|pairing_token|access_token|bearer|password|secret|pairing_text)",
                fixture_text,
            )
        )
        self.assertEqual(
            self.claim_fixture["schema_version"],
            "life-radio-ai-reader-pairing-claim-v1",
        )
        self.assertRegex(self.claim_fixture["pairing_id"], r"^[0-9a-f-]{36}$")

    def test_claim_uses_only_short_lived_pairing_auth_and_one_time_response(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/ai-readers/pairings/claim:", 2)
        response = yaml_block(self.openapi, "AIReaderPairingClaimResponse:", 4)
        self.assertIn("- aiReaderPairingAuth: []", endpoint)
        for forbidden in ("- aiReaderAuth: []", "- registeredDeviceAuth: []", "- bearerAuth: []"):
            self.assertNotIn(forbidden, endpoint)
        self.assertIn("#/components/schemas/AIReaderPairingClaim", endpoint)
        self.assertIn("returned exactly once", endpoint)
        self.assertIn("required: [access_token, reader_id, expires_at, context_url]", response)
        self.assertIn("additionalProperties: false", response)
        self.assertIn("writeOnly: true", response)

    def test_context_is_ai_reader_only_and_reuses_existing_models(self) -> None:
        endpoint = yaml_block(self.openapi, "/v1/read/ai/context:", 2)
        response = yaml_block(self.openapi, "AIReaderContextResponse:", 4)
        understanding = yaml_block(self.openapi, "AIReaderUnderstandingResult:", 4)
        self.assertIn("- aiReaderAuth: []", endpoint)
        self.assertNotIn("- readBearerAuth: []", endpoint)
        self.assertNotIn("- registeredDeviceAuth: []", endpoint)
        for parameter in (
            "AIReaderBusinessDate", "AIReaderCursor",
            "AIReaderUnderstandingVersion", "AIReaderView",
        ):
            self.assertIn(f"#/components/parameters/{parameter}", endpoint)
        for status in ("'200':", "'400':", "'401':", "'409':"):
            self.assertIn(status, endpoint)
        self.assertIn("invalid_cursor", endpoint)
        self.assertIn("cursor_superseded", endpoint)
        self.assertIn("raw GPS", endpoint)
        self.assertIn("required: [understanding, background, events, importance_counts, next_cursor]", response)
        self.assertIn("#/components/schemas/AIReaderBackground", response)
        self.assertIn("#/components/schemas/TimelineEvent", response)
        self.assertIn("required: [version, unchanged]", understanding)
        self.assertIn("#/components/schemas/AIUnderstandingGuide", understanding)
        compact = yaml_block(self.openapi, "AIReaderCompactContextResponse:", 4)
        compact_event = yaml_block(self.openapi, "AIReaderCompactEvent:", 4)
        view = yaml_block(self.openapi, "AIReaderView:", 4)
        self.assertIn("default: compact", view)
        self.assertIn("const: Asia/Shanghai", compact)
        self.assertIn("required: [at, importance, text]", compact_event)
        self.assertNotIn("timeline_event_id", compact_event)
        self.assertNotIn("event_key", compact_event)

    def test_management_is_registered_device_only_with_single_clear_operation(self) -> None:
        paths = (
            "/v1/ai-readers/pairings:",
            "/v1/ai-readers:",
            "/v1/ai-readers/{reader_id}/access-logs:",
            "/v1/ai-readers/{reader_id}/context-preview:",
            "/v1/ai-readers/{reader_id}/clear-reading-progress:",
            "/v1/ai-readers/{reader_id}:",
        )
        for path in paths:
            with self.subTest(path=path):
                endpoint = yaml_block(self.openapi, path, 2)
                self.assertIn("- registeredDeviceAuth: []", endpoint)
                self.assertNotIn("- aiReaderAuth: []", endpoint)
                self.assertNotIn("- readBearerAuth: []", endpoint)
        clear = yaml_block(self.openapi, "/v1/ai-readers/{reader_id}/clear-reading-progress:", 2)
        self.assertIn("cursor_epoch", clear)
        self.assertIn("current-business-day events", clear)
        self.assertNotIn("replay_today", self.openapi)
        self.assertNotIn("skip_to_now", self.openapi)
        preview = yaml_block(self.openapi, "/v1/ai-readers/{reader_id}/context-preview:", 2)
        self.assertIn("latest cursor issued", preview)
        self.assertIn("does not issue a usable cursor", preview)
        self.assertIn("#/components/schemas/AIReaderContextPreviewResponse", preview)

    def test_access_logs_mean_served_and_exclude_sensitive_material(self) -> None:
        access_log = yaml_block(self.openapi, "AIReaderAccessLog:", 4)
        self.assertIn("served means central prepared and returned", access_log)
        self.assertIn("never mean AI read or understood", access_log)
        self.assertIn("response_hash", access_log)
        for forbidden_field in (
            "access_token:",
            "authorization:",
            "pairing_text:",
            "latitude:",
            "longitude:",
        ):
            self.assertNotIn(forbidden_field, access_log.lower())

    def test_semantics_freeze_passive_scope_cursor_and_report_body(self) -> None:
        for statement in (
            "`400 invalid_cursor`",
            "`409 cursor_superseded`",
            "`401`",
            "access log 中的 `served`",
            "不表示 AI 已阅读、理解或向用户展示",
            "`event.evidence.body`",
            "原始 GPS",
            "游标同时绑定业务日",
            "不会提供上一业务日尚未读取的事件",
            "所有 `importance=low` 的时间线事件都不提供给 AI",
            "不会签发可用游标",
            "任意时刻最多一个 reader 为 active",
            "`understanding`，再给出 `background`",
            "首版不定义主动更新信号、Webhook、会话注入、AI 写入或回复",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.semantics)
        for excluded_path in ("/hooks/wake", "/hooks/agent", "/webhooks"):
            self.assertNotIn(excluded_path, self.openapi)

    def test_semantics_document_links_resolve(self) -> None:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]+)?\)", self.semantics):
            with self.subTest(target=target):
                self.assertTrue((SEMANTICS_PATH.parent / target).exists(), target)


if __name__ == "__main__":
    unittest.main()
