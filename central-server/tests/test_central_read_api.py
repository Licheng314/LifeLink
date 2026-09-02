import http.client
import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from central.config import CentralConfig
from central.http import create_server


DEVICE_TOKEN = "desktop-device-token-0123456789-ABCDEFGH"
PHONE_TOKEN = "android-device-token-0123456789-ABCDEFGHI"
READ_TOKEN = "central-read-token-0123456789-ABCDEFGHIJK"
OTHER_READ_TOKEN = "other-central-read-token-0123456789-ABCDEFG"


class CentralReadApiTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = CentralConfig(
            database_path=Path(self.temp_dir.name) / "central.sqlite3",
            host="127.0.0.1",
            port=0,
            token_bindings={
                DEVICE_TOKEN: "desktop-a",
                PHONE_TOKEN: "android-b",
            },
            read_token=READ_TOKEN,
        )
        self.server = create_server(self.config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, method, path, *, token=None, payload=None, batch_id=None,
                extra_headers=None):
        headers = {}
        body = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if batch_id:
            headers["Idempotency-Key"] = batch_id
        if extra_headers:
            headers.update(extra_headers)
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        self.last_headers = dict(response.getheaders())
        content = response.read().decode("utf-8")
        connection.close()
        if response.status in {204, 304}:
            return response.status, {}
        return response.status, json.loads(content)

    def test_timeline_etag_avoids_repeating_an_unchanged_private_body(self):
        start = "2026-08-24T16:00:00Z"
        end = "2026-08-25T16:00:00Z"
        path = self.read_path("/v1/timeline-events", start, end)
        status, body = self.request("GET", path, token=DEVICE_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])
        etag = self.last_headers.get("ETag")
        self.assertTrue(etag and etag.startswith('"') and etag.endswith('"'))

        status, body = self.request(
            "GET", path, token=DEVICE_TOKEN,
            extra_headers={"If-None-Match": etag},
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, {})
        self.assertEqual(self.last_headers.get("ETag"), etag)

    def request_text(self, method, path, *, token=None, payload=None, batch_id=None):
        headers = {}
        body = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if batch_id:
            headers["Idempotency-Key"] = batch_id
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        self.last_headers = dict(response.getheaders())
        content = response.read().decode("utf-8", errors="replace")
        connection.close()
        return response.status, content

    def test_calendar_days_allows_read_and_registered_device_tokens_only(self):
        path = "/v1/calendar-days?from=2026-08-01&to=2026-08-07"
        status, body = self.request("GET", path)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "missing_token")

        for token in (READ_TOKEN, DEVICE_TOKEN):
            with self.subTest(token=token):
                status, body = self.request("GET", path, token=token)
                self.assertEqual(status, 200)
                self.assertEqual(len(body["days"]), 7)
                self.assertEqual(body["days"][0]["business_date"], "2026-08-01")
                self.assertEqual(body["days"][-1]["business_date"], "2026-08-07")

        status, body = self.request(
            "GET", "/v1/calendar-days?from=2026-08-01&to=2026-09-12", token=READ_TOKEN
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_calendar_range")

    @staticmethod
    def event(
        event_type,
        occurred_at,
        *,
        duration=0,
        platform="desktop",
        collector="activitywatch",
        payload=None,
    ):
        return {
            "event_id": str(uuid.uuid4()),
            "occurred_at": occurred_at,
            "event_type": event_type,
            "source": {
                "kind": platform,
                "collector": collector,
                "reliability": "observed",
            },
            "duration_seconds": duration,
            "payload": payload or {},
        }

    @staticmethod
    def batch(device_id, platform, events):
        batch_id = str(uuid.uuid4())
        return {
            "schema_version": "v1",
            "batch_id": batch_id,
            "device": {
                "device_id": device_id,
                "platform": platform,
                "display_name": "Desktop A" if platform == "desktop" else "Phone B",
            },
            "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "events": events,
        }

    def upload(self, device_id, platform, events, token):
        payload = self.batch(device_id, platform, events)
        status, body = self.request(
            "POST",
            "/v1/events/batches",
            token=token,
            payload=payload,
            batch_id=payload["batch_id"],
        )
        self.assertEqual(status, 200, body)
        return body

    @staticmethod
    def read_path(endpoint, start, end, *, local_device_id=None):
        params = {"from": start, "to": end}
        if local_device_id is not None:
            params["local_device_id"] = local_device_id
        return endpoint + "?" + urlencode(params)

    def test_read_token_is_independent_and_range_errors_do_not_leak_secrets(self):
        path = self.read_path(
            "/v1/read/devices",
            "2026-07-30T14:00:00Z",
            "2026-07-30T15:00:00Z",
        )
        for token in (None, OTHER_READ_TOKEN):
            status, body = self.request("GET", path, token=token)
            self.assertEqual(status, 401)
            rendered = json.dumps(body)
            self.assertNotIn(READ_TOKEN, rendered)
            self.assertNotIn(str(token), rendered)

        status, body = self.request("GET", path, token=DEVICE_TOKEN)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "read_access_forbidden")
        self.assertNotIn(DEVICE_TOKEN, json.dumps(body))

        status, body = self.request("GET", "/v1/read/devices", token=READ_TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_range")
        self.assertNotIn(READ_TOKEN, json.dumps(body))

    def test_devices_return_stable_identity_range_counts_and_categories(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        occurred_at = now.isoformat().replace("+00:00", "Z")
        self.upload(
            "desktop-a",
            "desktop",
            [
                self.event(
                    "app.foreground",
                    occurred_at,
                    duration=30,
                    payload={"app": {"package_name": "editor.exe"}},
                ),
                self.event(
                    "custom.event",
                    occurred_at,
                    collector="life_radio_app",
                    payload={"event_key": "test", "title": "Test"},
                ),
            ],
            DEVICE_TOKEN,
        )
        start = (now - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
        end = (now + timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
        status, body = self.request(
            "GET",
            self.read_path(
                "/v1/read/devices",
                start,
                end,
                local_device_id="desktop-a",
            ),
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.last_headers.get("Cache-Control"), "no-store")
        self.assertEqual(body["window"], {"from": start, "to": end})
        self.assertTrue(body["generated_at"].endswith("Z"))
        self.assertEqual(body["online_window_seconds"], 600)
        desktop = next(item for item in body["devices"] if item["device_id"] == "desktop-a")
        self.assertEqual(desktop["device_key"], "desktop-a")
        self.assertTrue(desktop["is_local"])
        self.assertEqual(desktop["status"], "connected")
        self.assertEqual(desktop["last_received_at"], desktop["last_seen_at"])
        self.assertEqual(desktop["platform"], "desktop")
        self.assertEqual(desktop["display_name"], "Desktop A")
        self.assertEqual(desktop["event_count"], 2)
        self.assertEqual(desktop["batch_count"], 1)
        self.assertEqual(
            desktop["categories"],
            {"app.foreground": 1, "custom.event": 1},
        )
        self.assertEqual(
            desktop["window"],
            {
                "event_count": 2,
                "batch_count": 1,
                "categories": {"app.foreground": 1, "custom.event": 1},
            },
        )
        self.assertEqual(desktop["today"], desktop["window"])
        self.assertTrue(desktop["last_seen_at"].endswith("Z"))

        status, unknown_local = self.request(
            "GET",
            self.read_path(
                "/v1/read/devices",
                start,
                end,
                local_device_id="unknown-device",
            ),
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertTrue(all(not item["is_local"] for item in unknown_local["devices"]))

    def test_usage_clips_boundaries_excludes_other_types_and_caps_online(self):
        start = "2026-07-30T14:00:00Z"
        end = "2026-07-30T15:00:00Z"
        desktop_events = [
            self.event(
                "app.foreground",
                "2026-07-30T13:59:50Z",
                duration=20,
                payload={
                    "app": {"package_name": "chrome.exe", "display_name": "Google Chrome"},
                    "activitywatch": {
                        "kind": "window",
                        "bucket_id": "aw-watcher-window_desktop",
                        "event_id": 1,
                        "data": {"app": "chrome.exe"},
                    },
                },
            ),
            self.event(
                "app.foreground",
                "2026-07-30T13:59:30Z",
                duration=90,
                payload={
                    "app": {"package_name": "afk"},
                    "activitywatch": {
                        "kind": "afk",
                        "bucket_id": "aw-watcher-afk_desktop",
                        "event_id": 2,
                        "data": {"status": "not-afk"},
                    },
                },
            ),
            self.event(
                "app.foreground",
                "2026-07-30T14:00:05Z",
                duration=0,
                collector="browser_extension",
                payload={
                    "app": {"package_name": "chrome.exe"},
                    "activitywatch": {
                        "kind": "web",
                        "bucket_id": "aw-watcher-web-chrome_desktop",
                        "event_id": 3,
                        "data": {"url": "https://www.bilibili.com/video/1"},
                    },
                },
            ),
            self.event(
                "location.stay",
                "2026-07-30T14:10:00Z",
                duration=3500,
                collector="fused_location",
                payload={"kind": "stay", "latitude": 31.2, "longitude": 121.4},
            ),
            self.event(
                "custom.event",
                "2026-07-30T14:20:00Z",
                duration=3000,
                collector="life_radio_app",
                payload={"event_key": "noise", "title": "Noise"},
            ),
        ]
        self.upload("desktop-a", "desktop", desktop_events, DEVICE_TOKEN)
        self.upload(
            "android-b",
            "android",
            [
                self.event(
                    "app.foreground",
                    "2026-07-30T13:59:30Z",
                    duration=4000,
                    platform="android",
                    collector="usage_stats",
                    payload={
                        "app": {
                            "package_name": "com.example.reader",
                            "display_name": "Reader",
                        }
                    },
                )
            ],
            PHONE_TOKEN,
        )

        status, body = self.request(
            "GET",
            self.read_path(
                "/v1/read/usage",
                start,
                end,
                local_device_id="android-b",
            ),
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["window"], {"from": start, "to": end})
        self.assertTrue(body["generated_at"].endswith("Z"))
        by_id = {item["device_id"]: item for item in body["devices"]}
        desktop = by_id["desktop-a"]
        self.assertEqual(desktop["device_key"], "desktop-a")
        self.assertFalse(desktop["is_local"])
        self.assertEqual(desktop["events"], 3)
        self.assertEqual(desktop["window_events"], 1)
        self.assertEqual(desktop["web_events"], 1)
        self.assertEqual(desktop["afk_seconds"], 60)
        self.assertEqual(desktop["apps"], {"Google Chrome": 10})
        self.assertEqual(desktop["hourly"], {"22": 10})
        self.assertEqual(desktop["hourly_online"], {"22": 10})
        self.assertEqual(desktop["sites"], {"bilibili.com": 5})

        phone = by_id["android-b"]
        self.assertTrue(phone["is_local"])
        self.assertEqual(phone["apps"], {"Reader": 3600})
        self.assertEqual(phone["hourly_online"], {"22": 3600})
        self.assertLessEqual(max(phone["hourly_online"].values()), 3600)
        self.assertNotIn("location", json.dumps(body).lower())
        self.assertEqual(body["all"]["apps"], {"Google Chrome": 10, "Reader": 3600})

    def test_desktop_usage_removes_explicit_afk_and_reattributes_sites(self):
        start = "2026-07-30T14:00:00Z"
        end = "2026-07-30T15:00:00Z"
        self.upload("desktop-a", "desktop", [
            self.event(
                "app.foreground", start, duration=600,
                payload={
                    "app": {"package_name": "chrome.exe", "display_name": "Google Chrome"},
                    "activitywatch": {"kind": "window", "bucket_id": "aw-watcher-window", "data": {"app": "chrome.exe"}},
                },
            ),
            self.event(
                "app.foreground", start, duration=0, collector="browser_extension",
                payload={
                    "app": {"package_name": "chrome.exe"},
                    "activitywatch": {"kind": "web", "bucket_id": "aw-watcher-web-chrome", "data": {"url": "https://www.bilibili.com/video/1"}},
                },
            ),
            self.event(
                "app.foreground", "2026-07-30T14:04:00Z", duration=180,
                payload={
                    "app": {"package_name": "afk"},
                    "activitywatch": {"kind": "afk", "bucket_id": "aw-watcher-afk", "data": {"status": "afk"}},
                },
            ),
        ], DEVICE_TOKEN)
        status, body = self.request(
            "GET", self.read_path("/v1/read/usage", start, end), token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        desktop = next(item for item in body["devices"] if item["device_id"] == "desktop-a")
        self.assertEqual(desktop["apps"], {"Google Chrome": 420})
        self.assertEqual(desktop["hourly"], {"22": 420})
        self.assertEqual(desktop["hourly_online"], desktop["hourly"])
        self.assertEqual(desktop["sites"], {"bilibili.com": 420})
        self.assertIn("设备今日使用时长：7 分", body["ai_summary"])
        self.assertNotIn("在线时长", body["ai_summary"])

    def test_native_desktop_usage_and_domain_facts_replace_aw_server_data(self):
        start = "2026-07-30T14:00:00Z"
        end = "2026-07-30T15:00:00Z"
        self.upload("desktop-a", "desktop", [
            self.event(
                "app.foreground", start, duration=600, collector="windows_native",
                payload={"app": {"display_name": "msedge.exe", "package_name": "msedge.exe", "process_name": "msedge.exe"}},
            ),
            self.event(
                "device.input_state", start, duration=600, collector="windows_native",
                payload={"status": "active", "idle_threshold_seconds": 180},
            ),
            self.event(
                "device.input_state", "2026-07-30T14:04:00Z", duration=180,
                collector="windows_native",
                payload={"status": "afk", "idle_threshold_seconds": 180},
            ),
            self.event(
                "web.foreground", start, duration=600, collector="browser_extension",
                payload={"domain": "example.com", "browser_app": {"display_name": "Microsoft Edge", "process_name": "msedge.exe"}},
            ),
        ], DEVICE_TOKEN)
        status, body = self.request(
            "GET", self.read_path("/v1/read/usage", start, end), token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        desktop = next(item for item in body["devices"] if item["device_id"] == "desktop-a")
        self.assertEqual(desktop["apps"], {"msedge.exe": 420})
        self.assertEqual(desktop["sites"], {"example.com": 420})
        self.assertEqual(desktop["afk_seconds"], 180)

    def test_full_afk_is_zero_mobile_is_unchanged_and_hourly_is_capped(self):
        start = "2026-07-30T14:00:00Z"
        end = "2026-07-30T15:00:00Z"
        self.upload("desktop-a", "desktop", [
            self.event(
                "app.foreground", start, duration=3600,
                payload={"app": {"display_name": "Editor"}, "activitywatch": {"kind": "window", "data": {"app": "Editor"}}},
            ),
            self.event(
                "app.foreground", start, duration=3600,
                payload={"app": {"display_name": "Terminal"}, "activitywatch": {"kind": "window", "data": {"app": "Terminal"}}},
            ),
            self.event(
                "app.foreground", start, duration=3600,
                payload={"app": {"display_name": "AFK"}, "activitywatch": {"kind": "afk", "data": {"status": "afk"}}},
            ),
        ], DEVICE_TOKEN)
        self.upload("android-b", "android", [
            self.event(
                "app.foreground", start, duration=3600, platform="android", collector="usage_stats",
                payload={"app": {"display_name": "Reader"}},
            ),
            self.event(
                "app.foreground", start, duration=3600, platform="android",
                payload={"app": {"display_name": "AFK"}, "activitywatch": {"kind": "afk", "data": {"status": "afk"}}},
            ),
        ], PHONE_TOKEN)
        status, body = self.request(
            "GET", self.read_path("/v1/read/usage", start, end), token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        by_id = {item["device_id"]: item for item in body["devices"]}
        self.assertEqual(by_id["desktop-a"]["hourly"], {})
        self.assertEqual(by_id["desktop-a"]["apps"], {})
        self.assertEqual(by_id["android-b"]["hourly"], {"22": 3600})
        self.assertEqual(by_id["android-b"]["apps"], {"Reader": 3600})
        self.assertLessEqual(max(by_id["android-b"]["hourly"].values()), 3600)

    def test_usage_afk_clipping_respects_query_boundaries(self):
        start = "2026-07-30T14:00:00Z"
        end = "2026-07-30T15:00:00Z"
        self.upload("desktop-a", "desktop", [
            self.event(
                "app.foreground", "2026-07-30T13:55:00Z", duration=1200,
                payload={"app": {"display_name": "Editor"}, "activitywatch": {"kind": "window", "data": {"app": "Editor"}}},
            ),
            self.event(
                "app.foreground", "2026-07-30T13:58:00Z", duration=240,
                payload={"app": {"display_name": "AFK"}, "activitywatch": {"kind": "afk", "data": {"status": "afk"}}},
            ),
        ], DEVICE_TOKEN)
        status, body = self.request(
            "GET", self.read_path("/v1/read/usage", start, end), token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        desktop = next(item for item in body["devices"] if item["device_id"] == "desktop-a")
        self.assertEqual(desktop["apps"], {"Editor": 780})
        self.assertEqual(desktop["hourly"], {"22": 780})


    def test_locations_requires_read_token_and_returns_observations_segments(self):
        # No token and device tokens must not read.
        status, _ = self.request(
            "GET",
            self.read_path(
                "/v1/read/locations",
                "2026-07-30T14:00:00Z",
                "2026-07-30T15:00:00Z",
            ),
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            "GET",
            self.read_path(
                "/v1/read/locations",
                "2026-07-30T14:00:00Z",
                "2026-07-30T15:00:00Z",
            ),
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 403)

        self.upload(
            "android-b",
            "android",
            [
                self.event(
                    "location.observation",
                    "2026-07-30T14:01:00Z",
                    platform="android",
                    collector="fused_location",
                    payload={
                        "kind": "observation",
                        "latitude": 29.49,
                        "longitude": 106.63,
                        "accuracy_m": 30.0,
                        "observed_at": "2026-07-30T14:01:00Z",
                        "place": {"display_label": "测试地点A"},
                    },
                ),
                self.event(
                    "location.observation",
                    "2026-07-30T14:05:00Z",
                    platform="android",
                    collector="fused_location",
                    payload={
                        "kind": "observation",
                        "latitude": 29.4901,
                        "longitude": 106.6301,
                        "accuracy_m": 28.0,
                        "observed_at": "2026-07-30T14:05:00Z",
                        "place": {"display_label": "测试地点A"},
                    },
                ),
                self.event(
                    "location.stay",
                    "2026-07-30T14:10:00Z",
                    duration=1200,
                    platform="android",
                    collector="fused_location",
                    payload={
                        "kind": "stay",
                        "latitude": 29.50,
                        "longitude": 106.64,
                        "accuracy_m": 20.0,
                        "is_active": False,
                    },
                ),
            ],
            PHONE_TOKEN,
        )

        status, body = self.request(
            "GET",
            self.read_path(
                "/v1/read/locations",
                "2026-07-30T14:00:00Z",
                "2026-07-30T15:00:00Z",
            ),
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.last_headers.get("Cache-Control"), "no-store")
        self.assertEqual(len(body["observations"]), 2)
        self.assertEqual(body["observations"][0]["device_name"], "Phone B")
        self.assertEqual(body["observations"][0]["label"], "测试地点A")
        # Two close observations produce one derived segment, plus the stored stay.
        segment_kinds = sorted(item["kind"] for item in body["segments"])
        self.assertIn("stay", segment_kinds)
        self.assertTrue(any(item.get("derived_on_central") for item in body["segments"]))
        self.assertEqual(len(body["devices"]), 1)
        self.assertEqual(body["devices"][0]["device_id"], "android-b")
        self.assertIn("位置轨迹", body["ai_summary"])

    def test_usage_ai_summary_returns_markdown(self):
        self.upload("desktop-a", "desktop", [
            self.event(
                "app.foreground",
                "2026-07-18T10:00:00Z",
                duration=600,
                payload={
                    "app": {"display_name": "Visual Studio Code", "package_name": "code"},
                    "activitywatch": {"kind": "window", "bucket_id": "aw-watcher-window", "data": {"app": "Code"}},
                },
            ),
        ], DEVICE_TOKEN)
        status, body = self.request_text(
            "GET",
            self.read_path("/v1/read/ai/usage.md", "2026-07-18T00:00:00Z", "2026-07-19T00:00:00Z"),
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(body, str)
        self.assertIn("Visual Studio Code", body)

    def test_usage_ai_summary_requires_read_token(self):
        status, body = self.request_text(
            "GET",
            self.read_path("/v1/read/ai/usage.md", "2026-07-18T00:00:00Z", "2026-07-19T00:00:00Z"),
        )
        self.assertEqual(status, 401)

    def test_usage_ai_summary_rejects_upload_token(self):
        status, body = self.request_text(
            "GET",
            self.read_path("/v1/read/ai/usage.md", "2026-07-18T00:00:00Z", "2026-07-19T00:00:00Z"),
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 403)

    def test_location_ai_summary_returns_markdown(self):
        self.upload("android-b", "android", [
            self.event(
                "location.observation",
                "2026-07-18T14:00:00Z",
                duration=0,
                platform="android",
                payload={
                    "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 15,
                    "observed_at": "2026-07-18T14:00:00Z",
                    "place": {"display_label": "上海市黄浦区"},
                },
            ),
        ], PHONE_TOKEN)
        status, body = self.request_text(
            "GET",
            self.read_path("/v1/read/ai/location.md", "2026-07-18T00:00:00Z", "2026-07-19T00:00:00Z"),
            token=READ_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(body, str)
        self.assertIn("上海", body)

    # --- Blacklist rules central CRUD and matching ---

    def test_blacklist_rules_seed(self):
        status, body = self.request("GET", "/v1/settings/blacklist-rules", token=READ_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("rules", body)
        self.assertEqual(len(body["rules"]), 7)

    def test_blacklist_rules_empty_after_delete_all(self):
        for r in self.server.store.list_blacklist_rules():
            self.server.store.delete_blacklist_rule(r["rule_id"])
        status, body = self.request("GET", "/v1/settings/blacklist-rules", token=READ_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"], [])

    def test_blacklist_rules_crud_and_match(self):
        # Create
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={"rule_type": "domain", "pattern": "Example.COM", "label": "Example"})
        self.assertEqual(status, 201)
        rule_id = body["rule_id"]
        # Normalised matching
        self.assertEqual(self.server.store._is_enabled_domain("WWW.Example.COM."), True)
        self.assertEqual(self.server.store._is_enabled_domain("sub.example.com"), True)
        self.assertEqual(self.server.store._is_enabled_domain("notexample.com"), False)
        self.assertEqual(self.server.store._is_enabled_domain("bilibili.com.example.org"), False)
        # Update
        status, body = self.request("PATCH", f"/v1/settings/blacklist-rules/{rule_id}",
            token=DEVICE_TOKEN, payload={"enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["enabled"], False)
        self.assertEqual(self.server.store._is_enabled_domain("example.com"), False)
        # Re-enable
        status, _ = self.request("PATCH", f"/v1/settings/blacklist-rules/{rule_id}",
            token=DEVICE_TOKEN, payload={"enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.server.store._is_enabled_domain("example.com"), True)
        # Delete
        status, _ = self.request("DELETE", f"/v1/settings/blacklist-rules/{rule_id}",
            token=DEVICE_TOKEN)
        self.assertEqual(status, 204)
        self.assertEqual(self.server.store._is_enabled_domain("example.com"), False)

    def test_blacklist_rules_anonymous_rejected(self):
        status, _ = self.request("GET", "/v1/settings/blacklist-rules")
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/v1/settings/blacklist-rules",
            payload={"rule_type": "app", "pattern": "x", "label": "x"})
        self.assertEqual(status, 401)

    def test_blacklist_rules_update_via_post_transport_alias(self):
        # HTTPS tunnels (e.g. peanuthull) reject PATCH; the central service
        # exposes a POST alias for rule updates, mirroring device rename.
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={"rule_type": "app", "pattern": "AliasApp", "label": "before"})
        self.assertEqual(status, 201)
        rule_id = body["rule_id"]
        status, body = self.request("POST", f"/v1/settings/blacklist-rules/{rule_id}",
            token=DEVICE_TOKEN, payload={"label": "after"})
        self.assertEqual(status, 200)
        self.assertEqual(body["label"], "after")

        for invalid_payload in ({}, {"unknown": True}, {"label": "x" * 101}):
            status, _ = self.request(
                "POST", f"/v1/settings/blacklist-rules/{rule_id}",
                token=DEVICE_TOKEN, payload=invalid_payload,
            )
            self.assertEqual(status, 400)
        self.assertEqual(self.server.store._get_rule(rule_id)["label"], "after")

    def test_blacklist_rule_routes_reject_extra_path_segments(self):
        status, body = self.request(
            "POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN,
            payload={"rule_type": "app", "pattern": "StrictPathApp", "label": "before"},
        )
        self.assertEqual(status, 201)
        rule_id = body["rule_id"]
        malformed = f"/v1/settings/blacklist-rules/unrelated/{rule_id}"
        for method, payload in (("POST", {"label": "wrong"}), ("PATCH", {"label": "wrong"}), ("DELETE", None)):
            status, _ = self.request(method, malformed, token=DEVICE_TOKEN, payload=payload)
            self.assertEqual(status, 404)
        self.assertEqual(self.server.store._get_rule(rule_id)["label"], "before")

    def test_blacklist_rules_rejects_duplicate(self):
        status, _ = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={"rule_type": "app", "pattern": "steam", "label": "dup"})
        self.assertEqual(status, 400)

    def test_blacklist_rules_app_normalised_match(self):
        # Minecraft is seeded as 'Minecraft', normalised to 'minecraft'
        store = self.server.store
        self.assertEqual(store._is_enabled_app("Minecraft"), True)
        self.assertEqual(store._is_enabled_app("Minecraft Launcher"), True)
        self.assertEqual(store._is_enabled_app("minecraft"), True)
        # Substring match: 'minecraft' is inside 'notminecraft' — correct per app matching rules
        self.assertEqual(store._is_enabled_app("NotMinecraft"), True)
        # No overlap at all
        self.assertEqual(store._is_enabled_app("Visual Studio Code"), False)

    def test_blacklist_rules_platform_scope_create_and_list(self):
        # Create an android app rule
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={
                "rule_type": "app", "pattern": "TikTok", "label": "TikTok",
                "platform_scope": "android",
            })
        self.assertEqual(status, 201)
        self.assertEqual(body["platform_scope"], "android")
        rule_id = body["rule_id"]

        # Create a pc app rule
        status, body2 = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={
                "rule_type": "app", "pattern": "Notepad", "label": "Notepad",
                "platform_scope": "pc",
            })
        self.assertEqual(status, 201)
        self.assertEqual(body2["platform_scope"], "pc")

        # List should include both with correct platform_scope
        status, list_body = self.request("GET", "/v1/settings/blacklist-rules", token=READ_TOKEN)
        self.assertEqual(status, 200)
        rules_by_id = {r["rule_id"]: r for r in list_body["rules"]}
        self.assertEqual(rules_by_id[rule_id]["platform_scope"], "android")
        self.assertEqual(rules_by_id[body2["rule_id"]]["platform_scope"], "pc")

    def test_blacklist_rules_platform_scope_defaults(self):
        # App without platform_scope defaults to pc
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={"rule_type": "app", "pattern": "AnyApp", "label": "Any"})
        self.assertEqual(status, 201)
        self.assertEqual(body["platform_scope"], "pc")

        # Domain without platform_scope defaults to web
        status, body2 = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={"rule_type": "domain", "pattern": "any.com", "label": "any"})
        self.assertEqual(status, 201)
        self.assertEqual(body2["platform_scope"], "web")

    def test_blacklist_rules_platform_scope_cross_matching(self):
        store = self.server.store
        # Create a pc-only app rule
        status, _ = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={
                "rule_type": "app", "pattern": "SecretPad", "label": "pc secret",
                "platform_scope": "pc",
            })
        self.assertEqual(status, 201)

        # Should match on pc
        self.assertTrue(store._is_enabled_app("SecretPad", "pc"))
        # Should NOT match on android
        self.assertFalse(store._is_enabled_app("SecretPad", "android"))

        # Create an android-only app rule
        status, _ = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={
                "rule_type": "app", "pattern": "SecretPhone", "label": "phone secret",
                "platform_scope": "android",
            })
        self.assertEqual(status, 201)

        # Should NOT match on pc
        self.assertFalse(store._is_enabled_app("SecretPhone", "pc"))
        # Should match on android
        self.assertTrue(store._is_enabled_app("SecretPhone", "android"))

    def test_blacklist_rules_patch_rejects_platform_scope(self):
        # Create a rule
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={"rule_type": "app", "pattern": "TestApp", "label": "test"})
        self.assertEqual(status, 201)
        rule_id = body["rule_id"]

        # PATCH to change platform_scope should be rejected
        status, body = self.request("PATCH", f"/v1/settings/blacklist-rules/{rule_id}",
            token=DEVICE_TOKEN, payload={"platform_scope": "android"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "immutable_platform_scope")

    def test_blacklist_rules_migration_idempotent(self):
        store = self.server.store
        with store._connection() as conn:
            # Run migration twice — should not fail
            store._migrate_blacklist_platform_scope(conn)
            store._migrate_blacklist_platform_scope(conn)
        # All existing app rules should be pc, domain rules should be web
        rules = store.list_blacklist_rules()
        for rule in rules:
            if rule["rule_type"] == "app":
                self.assertIn(rule["platform_scope"], {"pc", "android"})
            else:
                self.assertEqual(rule["platform_scope"], "web")

    def test_blacklist_rules_invalid_platform_scope_combinations(self):
        # app with web should be rejected
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={
                "rule_type": "app", "pattern": "BadApp", "label": "bad",
                "platform_scope": "web",
            })
        self.assertEqual(status, 400)

        # domain with pc should be rejected
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={
                "rule_type": "domain", "pattern": "bad.com", "label": "bad",
                "platform_scope": "pc",
            })
        self.assertEqual(status, 400)

        # domain with android should be rejected
        status, body = self.request("POST", "/v1/settings/blacklist-rules",
            token=DEVICE_TOKEN, payload={
                "rule_type": "domain", "pattern": "bad2.com", "label": "bad",
                "platform_scope": "android",
            })
        self.assertEqual(status, 400)

    def test_blacklist_rules_enabled_patterns_for_scope(self):
        store = self.server.store
        # Remove seeds to avoid interference
        for r in store.list_blacklist_rules():
            store.delete_blacklist_rule(r["rule_id"])

        # Create pc rule
        store.create_blacklist_rule("app", "PcGame", "pc game", platform_scope="pc", enabled=True)
        # Create android rule
        store.create_blacklist_rule("app", "MobileGame", "mobile game", platform_scope="android", enabled=True)

        pc_patterns = store.enabled_patterns_for_scope("app", "pc")
        android_patterns = store.enabled_patterns_for_scope("app", "android")
        self.assertIn("pcgame", pc_patterns)
        self.assertNotIn("mobilegame", pc_patterns)
        self.assertIn("mobilegame", android_patterns)
        self.assertNotIn("pcgame", android_patterns)


class CentralReadConfigTestCase(unittest.TestCase):
    def test_external_and_environment_read_tokens_are_strong_and_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "central.json"
            config_path.write_text(
                json.dumps(
                    {
                        "token_bindings": {DEVICE_TOKEN: "desktop-a"},
                        "read_token": READ_TOKEN,
                    }
                ),
                encoding="utf-8",
            )
            external = CentralConfig.from_environment(
                {"LIFE_RADIO_CENTRAL_CONFIG": str(config_path)}
            )
            self.assertEqual(external.read_token, READ_TOKEN)
            overridden = CentralConfig.from_environment(
                {
                    "LIFE_RADIO_CENTRAL_CONFIG": str(config_path),
                    "LIFE_RADIO_CENTRAL_READ_TOKEN": OTHER_READ_TOKEN,
                }
            )
            self.assertEqual(overridden.read_token, OTHER_READ_TOKEN)
            self.assertNotIn(OTHER_READ_TOKEN, repr(overridden))

        with self.assertRaisesRegex(ValueError, "weak read token"):
            CentralConfig(token_bindings={DEVICE_TOKEN: "desktop-a"}, read_token="x" * 32)
        with self.assertRaisesRegex(ValueError, "distinct"):
            CentralConfig(token_bindings={DEVICE_TOKEN: "desktop-a"}, read_token=DEVICE_TOKEN)


if __name__ == "__main__":
    unittest.main()
