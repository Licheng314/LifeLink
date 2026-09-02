import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request

import central_endpoint


class CentralEndpointTests(unittest.TestCase):
    def test_normalize_requires_https_origin(self):
        self.assertEqual(
            central_endpoint.normalize_base_url(" https://Example.Test/ "),
            "https://example.test",
        )
        for invalid in (
            "http://example.test",
            "https://example.test/path",
            "https://example.test?token=secret",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                central_endpoint.normalize_base_url(invalid)

    def test_probe_rejects_dashboard_mapping_with_actionable_message(self):
        def fake_fetch(request: Request, timeout: float):
            return {
                "status": "ok",
                "api_version": "v1",
                "device": {"device_id": "desktop-test"},
            }

        with self.assertRaisesRegex(
            central_endpoint.EndpointError, "match the central configuration",
        ):
            central_endpoint.probe_endpoint(
                "https://example.test", fetch_json=fake_fetch,
            )

    def test_probe_checks_central_role_and_authenticated_read(self):
        requests = []

        def fake_fetch(request: Request, timeout: float):
            requests.append(request)
            if request.full_url.endswith("/v1/health"):
                return {"status": "ok", "role": "central"}
            self.assertEqual(request.get_header("Authorization"), "Bearer read-secret")
            return {"devices": [{"device_id": "desktop-test"}]}

        report = central_endpoint.probe_endpoint(
            "https://example.test",
            "read-secret",
            fetch_json=fake_fetch,
        )

        self.assertEqual(report["role"], "central")
        self.assertTrue(report["authenticated_read"])
        self.assertEqual(report["device_count"], 1)
        self.assertEqual(len(requests), 2)

    def test_save_and_load_contains_no_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public_endpoint.json"
            central_endpoint.save_endpoint(
                path, "peanuthull", "https://example.vicp.fun",
            )
            payload = central_endpoint.load_endpoint(path)
            raw = path.read_text(encoding="utf-8")

        self.assertEqual(payload["provider"], "peanuthull")
        self.assertEqual(payload["base_url"], "https://example.vicp.fun")
        self.assertNotIn("token", raw.lower())

    def test_legacy_endpoint_is_merged_into_central_config_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            legacy = root / "public_endpoint.json"
            config.write_text(json.dumps({
                "host": "127.0.0.1",
                "token_bindings": {"secret-device-token": "desktop-a"},
            }), encoding="utf-8")
            legacy.write_text(json.dumps({
                "version": 1,
                "provider": "peanuthull",
                "base_url": "https://example.vicp.fun",
                "verified_at": "2026-08-01T00:00:00Z",
            }), encoding="utf-8")

            with (
                mock.patch.object(central_endpoint, "default_config_path", return_value=config),
                mock.patch.object(central_endpoint, "legacy_endpoint_path", return_value=legacy),
            ):
                endpoint = central_endpoint.load_endpoint(config)

            merged = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(endpoint["base_url"], "https://example.vicp.fun")
            self.assertEqual(
                merged["token_bindings"], {"secret-device-token": "desktop-a"},
            )
            self.assertEqual(
                merged["public_endpoint"]["base_url"], "https://example.vicp.fun",
            )
            self.assertFalse(legacy.exists())


if __name__ == "__main__":
    unittest.main()
