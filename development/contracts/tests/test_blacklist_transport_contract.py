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


class BlacklistTransportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openapi = OPENAPI_PATH.read_text(encoding="utf-8")

    def test_update_alias_and_patch_use_the_same_closed_body(self):
        endpoint = yaml_block(self.openapi, "/v1/settings/blacklist-rules/{rule_id}:", 2)
        self.assertIn("operationId: updateBlacklistRuleViaPost", endpoint)
        self.assertIn("operationId: updateBlacklistRule", endpoint)
        self.assertEqual(endpoint.count("additionalProperties: false"), 2)
        self.assertEqual(endpoint.count("minProperties: 1"), 2)
        self.assertEqual(endpoint.count("maxLength: 100"), 2)


if __name__ == "__main__":
    unittest.main()
