from __future__ import annotations

import base64
import json
import re
import unittest
from pathlib import Path


CONTRACTS_DIR = Path(__file__).resolve().parents[1]
OPENAPI_PATH = CONTRACTS_DIR / "life-radio-api-v1.yaml"
SEMANTICS_PATH = CONTRACTS_DIR / "one-line-invitation-v1.md"
INVITATION_SCHEMA_PATH = CONTRACTS_DIR / "one-line-invitation-payload-v1.schema.json"
CLAIM_SCHEMA_PATH = CONTRACTS_DIR / "enrollment-claim-v1.schema.json"
PROFILE_SCHEMA_PATH = CONTRACTS_DIR / "client-profile-v1.schema.json"
CLAIM_FIXTURE_PATH = CONTRACTS_DIR / "fixtures" / "enrollment-claim-v1.json"
ANDROID_CLAIM_FIXTURE_PATH = CONTRACTS_DIR / "fixtures" / "enrollment-claim-android-v1.json"


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


class OnlineEnrollmentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = OPENAPI_PATH.read_text(encoding="utf-8")
        cls.semantics = SEMANTICS_PATH.read_text(encoding="utf-8")
        cls.invitation_schema = json.loads(INVITATION_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.claim_schema = json.loads(CLAIM_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.profile_schema = json.loads(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.claim_fixture = json.loads(CLAIM_FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.android_claim_fixture = json.loads(
            ANDROID_CLAIM_FIXTURE_PATH.read_text(encoding="utf-8")
        )

    def test_invitation_payload_is_frozen_and_secret_bearing(self) -> None:
        self.assertFalse(self.invitation_schema["additionalProperties"])
        self.assertEqual(
            self.invitation_schema["required"],
            ["v", "invitation_id", "central_base_url", "invitation_token", "scope", "expires_at"],
        )
        properties = self.invitation_schema["properties"]
        self.assertEqual(properties["v"]["const"], 1)
        self.assertEqual(properties["scope"]["enum"], ["upload", "dashboard"])
        self.assertTrue(properties["central_base_url"]["pattern"].startswith("^https://"))
        self.assertGreaterEqual(properties["invitation_token"]["minLength"], 32)
        self.assertEqual(properties["expires_at"]["pattern"], "Z$")

    def test_lr1_code_is_unpadded_base64url_compact_json(self) -> None:
        payload = {
            "v": 1,
            "invitation_id": "22222222-2222-4222-8222-222222222222",
            "central_base_url": "https://central.example.test",
            "invitation_token": "t" * 32,
            "scope": "upload",
            "expires_at": "2026-08-02T03:00:00Z",
        }
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(compact).decode("ascii").rstrip("=")
        invitation = "LR1." + encoded
        self.assertRegex(invitation, r"^LR1\.[A-Za-z0-9_-]+$")
        self.assertNotIn("=", invitation)
        padded = encoded + "=" * (-len(encoded) % 4)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(padded)), payload)

    def test_claim_body_and_fixture_use_no_secret_fields(self) -> None:
        self.assertFalse(self.claim_schema["additionalProperties"])
        self.assertEqual(
            self.claim_schema["required"],
            ["schema_version", "invitation_id", "device"],
        )
        self.assertEqual(
            self.claim_schema["properties"]["schema_version"]["const"],
            "life-radio-enrollment-claim-v1",
        )
        self.assertEqual(self.claim_fixture["schema_version"], "life-radio-enrollment-claim-v1")
        self.assertEqual(self.claim_fixture["device"]["platform"], "desktop")
        self.assertEqual(self.android_claim_fixture["device"]["platform"], "android")
        self.assertRegex(
            self.android_claim_fixture["device"]["device_id"],
            r"^android-install-[0-9a-f-]{36}$",
        )
        fixture_text = CLAIM_FIXTURE_PATH.read_text(encoding="utf-8") + (
            ANDROID_CLAIM_FIXTURE_PATH.read_text(encoding="utf-8")
        )
        self.assertIsNone(re.search(r"(?i)(invitation_token|upload_token|read_token|password|secret)", fixture_text))

    def test_claim_schema_ties_each_platform_to_its_installation_id_format(self) -> None:
        device = self.claim_schema["$defs"]["enrollment_device"]
        self.assertEqual(len(device["oneOf"]), 2)
        desktop = self.claim_schema["$defs"]["desktop_device"]
        android = self.claim_schema["$defs"]["android_device"]
        self.assertEqual(desktop["properties"]["platform"]["const"], "desktop")
        self.assertEqual(android["properties"]["platform"]["const"], "android")
        self.assertTrue(
            android["properties"]["device_id"]["pattern"].startswith(
                "^android-install-"
            )
        )

    def test_claim_endpoint_uses_only_invitation_bearer_and_profile_response(self) -> None:
        claim = yaml_block(self.openapi, "/v1/enrollments/claim:", 2)
        self.assertIn("- invitationBearerAuth: []", claim)
        self.assertNotIn("- bearerAuth: []", claim)
        self.assertNotIn("- readBearerAuth: []", claim)
        self.assertIn("#/components/schemas/EnrollmentClaim", claim)
        self.assertIn("#/components/schemas/LifeRadioClientProfile", claim)
        for status in ("'200':", "'400':", "'401':", "'409':", "'410':", "'429':", "'503':"):
            self.assertIn(status, claim)
        self.assertIn("invitation_already_claimed", claim)
        self.assertIn("invitation_expired", claim)

    def test_profile_is_permanent_device_bound_and_read_token_is_optional(self) -> None:
        self.assertEqual(
            self.profile_schema["properties"]["schema_version"]["const"],
            "life-radio-client-profile-v1",
        )
        self.assertIn("upload_token", self.profile_schema["required"])
        self.assertNotIn("read_token", self.profile_schema["required"])
        component = yaml_block(self.openapi, "LifeRadioClientProfile:", 4)
        self.assertIn("const: life-radio-client-profile-v1", component)
        self.assertIn("upload_token:", component)
        self.assertIn("read_token:", component)
        self.assertIn("upload scope 必须省略", component)

    def test_document_freezes_idempotency_and_secret_boundaries(self) -> None:
        required = (
            "默认有效期是 24 小时",
            "不同 `device_id` 再使用同一邀请必须返回 `409 invitation_already_claimed`",
            "同一稳定 `device_id` 的重试必须返回首次签发的同一份长期配置",
            "一旦过期，无论是首次领取、已领取同设备的幂等重试，还是不同设备复用，都返回 `410 invitation_expired`",
            "`invitation_token` 只以不可逆密码学散列保存",
            "不得放入 URL 路径、查询参数、fragment",
            "不得把邀请码、`invitation_token`、`upload_token` 或 `read_token` 写入 `localStorage`",
            "不再保留离线双文件签发路径",
        )
        for statement in required:
            with self.subTest(statement=statement):
                self.assertIn(statement, self.semantics)

    def test_document_links_resolve(self) -> None:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]+)?\)", self.semantics):
            with self.subTest(target=target):
                self.assertTrue((SEMANTICS_PATH.parent / target).exists(), target)


if __name__ == "__main__":
    unittest.main()
