"""Unit tests for the non-destructive Tailscale endpoint setup helper."""

from __future__ import annotations

import unittest
from unittest import mock

import configure_tailscale_endpoint as setup


class TailscaleEndpointSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.status = {
            "BackendState": "Running",
            "Self": {"DNSName": "central.tail-example.ts.net."},
        }

    def test_detects_https_candidate_without_querying_or_changing_serve_rules(self) -> None:
        with mock.patch.object(
            setup, "_run_tailscale", return_value=__import__("json").dumps(self.status),
        ) as run:
            endpoint = setup.detect_https_endpoint()
        self.assertEqual(endpoint, "https://central.tail-example.ts.net:8443")
        run.assert_called_once_with("status", "--json")

    def test_configures_empty_dedicated_port_and_saves_verified_endpoint(self) -> None:
        calls: list[tuple[str, ...]] = []
        def run(*arguments: str) -> str:
            calls.append(arguments)
            if arguments == ("status", "--json"):
                return __import__("json").dumps(self.status)
            if arguments == ("serve", "status", "--json"):
                return "{}"
            return ""
        with (
            mock.patch.object(setup, "_check_local_central"),
            mock.patch.object(setup, "_run_tailscale", side_effect=run),
            mock.patch.object(setup, "probe_endpoint"),
            mock.patch.object(setup, "read_token", return_value="read-token"),
            mock.patch.object(setup, "save_endpoint") as save,
        ):
            endpoint = setup.configure()
        self.assertEqual(endpoint, "https://central.tail-example.ts.net:8443")
        self.assertIn(("serve", "--bg", "--https=8443", "http://127.0.0.1:8091"), calls)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[1:], ("tailscale", endpoint))

    def test_refuses_to_overwrite_another_service_on_dedicated_port(self) -> None:
        def run(*arguments: str) -> str:
            if arguments == ("status", "--json"):
                return __import__("json").dumps(self.status)
            if arguments == ("serve", "status", "--json"):
                return __import__("json").dumps({
                    "Web": {
                        "central.tail-example.ts.net:8443": {
                            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
                        }
                    }
                })
            self.fail(f"unexpected command: {arguments}")
        with (
            mock.patch.object(setup, "_check_local_central"),
            mock.patch.object(setup, "_run_tailscale", side_effect=run),
        ):
            with self.assertRaisesRegex(setup.TailscaleSetupError, "未做任何修改"):
                setup.configure()

    def test_existing_matching_route_is_reused_without_rewriting(self) -> None:
        def run(*arguments: str) -> str:
            if arguments == ("status", "--json"):
                return __import__("json").dumps(self.status)
            if arguments == ("serve", "status", "--json"):
                return __import__("json").dumps({
                    "Web": {
                        "central.tail-example.ts.net:8443": {
                            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8091"}}
                        }
                    }
                })
            self.fail(f"unexpected command: {arguments}")
        with (
            mock.patch.object(setup, "_check_local_central"),
            mock.patch.object(setup, "_run_tailscale", side_effect=run),
            mock.patch.object(setup, "probe_endpoint"),
            mock.patch.object(setup, "read_token", return_value="read-token"),
            mock.patch.object(setup, "save_endpoint"),
        ):
            self.assertEqual(setup.configure(), "https://central.tail-example.ts.net:8443")

    def test_custom_central_port_is_used_for_the_serve_target(self) -> None:
        calls: list[tuple[str, ...]] = []
        def run(*arguments: str) -> str:
            calls.append(arguments)
            if arguments == ("status", "--json"):
                return __import__("json").dumps(self.status)
            if arguments == ("serve", "status", "--json"):
                return "{}"
            return ""
        with (
            mock.patch.object(setup, "_check_local_central"),
            mock.patch.object(setup, "_run_tailscale", side_effect=run),
            mock.patch.object(setup, "probe_endpoint"),
            mock.patch.object(setup, "read_token", return_value="read-token"),
            mock.patch.object(setup, "save_endpoint"),
        ):
            setup.configure(central_port=8094)
        self.assertIn(
            ("serve", "--bg", "--https=8443", "http://127.0.0.1:8094"), calls,
        )

    def test_known_previous_lifelink_target_can_be_updated(self) -> None:
        calls: list[tuple[str, ...]] = []
        def run(*arguments: str) -> str:
            calls.append(arguments)
            if arguments == ("status", "--json"):
                return __import__("json").dumps(self.status)
            if arguments == ("serve", "status", "--json"):
                return __import__("json").dumps({
                    "Web": {
                        "central.tail-example.ts.net:8443": {
                            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8091"}}
                        }
                    }
                })
            return ""
        with (
            mock.patch.object(setup, "_check_local_central"),
            mock.patch.object(setup, "_run_tailscale", side_effect=run),
            mock.patch.object(setup, "probe_endpoint"),
            mock.patch.object(setup, "read_token", return_value="read-token"),
            mock.patch.object(setup, "save_endpoint"),
        ):
            setup.configure(central_port=8094, previous_central_port=8091)
        self.assertIn(
            ("serve", "--bg", "--https=8443", "http://127.0.0.1:8094"), calls,
        )
