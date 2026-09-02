import json
import unittest
from pathlib import Path


CONTRACTS_DIR = Path(__file__).resolve().parents[1]
OPENAPI = (CONTRACTS_DIR / "life-radio-api-v1.yaml").read_text(encoding="utf-8")
FIXTURE = json.loads((CONTRACTS_DIR / "fixtures" / "sync-batch-v1.json").read_text(encoding="utf-8"))


def yaml_block(marker: str, indent: int = 4) -> str:
    lines = OPENAPI.splitlines()
    start = lines.index(" " * indent + marker)
    selected = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        selected.append(line)
    return "\n".join(selected)


class NativeDesktopFactsContractTests(unittest.TestCase):
    def test_new_event_types_and_native_collector_are_additive(self):
        context_event = yaml_block("ContextEvent:")
        source = yaml_block("EventSource:")
        self.assertIn("app.foreground", context_event)
        self.assertIn("web.foreground", context_event)
        self.assertIn("device.input_state", context_event)
        self.assertIn("activitywatch", source)
        self.assertIn("browser_extension", source)
        self.assertIn("windows_native", source)

    def test_input_state_is_limited_to_non_content_status(self):
        payload = yaml_block("DeviceInputStatePayload:")
        self.assertIn("additionalProperties: false", payload)
        self.assertIn("required: [status]", payload)
        self.assertIn("enum: [active, afk, locked]", payload)
        self.assertIn("idle_threshold_seconds:", payload)
        self.assertNotIn("keystrokes:", payload)

    def test_web_foreground_rejects_privacy_bearing_fields(self):
        payload = yaml_block("WebForegroundPayload:")
        self.assertIn("additionalProperties: false", payload)
        self.assertIn("required: [domain]", payload)
        self.assertIn("browser_app:", payload)
        for prohibited_field in ("url:", "title:", "incognito:", "is_incognito:"):
            self.assertNotIn(prohibited_field, payload)
        self.assertIn("(?!https?://)", payload)
        self.assertIn("(?!.*[/?#@])", payload)

    def test_fixture_has_anonymous_native_and_domain_only_examples(self):
        events = {event["event_type"]: event for event in FIXTURE["events"]}
        native_app = events["app.foreground"]
        self.assertEqual("windows_native", native_app["source"]["collector"])
        self.assertEqual("sample-editor.exe", native_app["payload"]["app"]["process_name"])
        input_state = events["device.input_state"]
        self.assertEqual("afk", input_state["payload"]["status"])
        web = events["web.foreground"]
        self.assertEqual("example.test", web["payload"]["domain"])
        self.assertEqual({"domain", "browser_app"}, set(web["payload"]))
        rendered = json.dumps(web, ensure_ascii=False).lower()
        self.assertNotIn("http://", rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("incognito", rendered)

    def test_legacy_activitywatch_payload_remains_explicitly_compatible(self):
        payload = yaml_block("AppForegroundPayload:")
        self.assertIn("additionalProperties: true", payload)
        self.assertIn("activitywatch:", payload)


if __name__ == "__main__":
    unittest.main()
