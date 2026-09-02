import http.client
import json
import socket
import sys
import unittest
import uuid
from urllib.parse import quote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aw_web_compat


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


class AWWebCompatTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.receiver = aw_web_compat.AWWebCompatReceiver(self.events.append, port=0)
        started = self.receiver.start()
        self.assertEqual(started["status"], "started")
        self.port = started["port"]

    def tearDown(self):
        self.receiver.stop()

    def request(self, method, path, payload=None, origin=ORIGIN, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        headers = {"Origin": origin}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(payload).encode()
        headers.update(extra_headers or {})
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), json.loads(body) if body else None

    def create_bucket(self, bucket="aw-watcher-web-chrome"):
        encoded = quote(bucket, safe="")
        return self.request("POST", f"/api/0/buckets/{encoded}", {"type": "web.tab.current", "client": "ignored"})

    def test_heartbeat_merges_domain_and_emits_stable_identifier(self):
        self.assertEqual(self.create_bucket()[0], 200)
        payload = {"timestamp": "2026-08-27T12:00:00Z", "duration": 0, "data": {"url": "https://www.Example.COM/a?q=secret#section", "title": "private"}}
        self.assertEqual(self.request("POST", "/api/0/buckets/aw-watcher-web-chrome/heartbeat?pulsetime=20", payload)[0], 200)
        payload["timestamp"] = "2026-08-27T12:00:15Z"
        self.assertEqual(self.request("POST", "/api/0/buckets/aw-watcher-web-chrome/heartbeat?pulsetime=20", payload)[0], 200)
        self.assertEqual(len(self.events), 2)
        first, merged = self.events
        self.assertEqual(merged["event_id"], first["event_id"])
        self.assertEqual(merged["revision"], 1)
        self.assertEqual(merged["duration_seconds"], 35.0)
        self.assertIsInstance(merged["duration_seconds"], int)
        uuid.UUID(merged["event_id"])
        self.assertEqual(merged["event_type"], "web.foreground")
        self.assertEqual(merged["source"], {"kind": "desktop", "collector": "browser_extension", "reliability": "observed"})
        self.assertEqual(merged["payload"], {
            "domain": "example.com",
            "browser_app": {"display_name": "Google Chrome", "process_name": "chrome.exe"},
        })
        self.assertNotIn("url", json.dumps(merged))
        self.assertNotIn("title", json.dumps(merged))
        self.assertNotIn("bucket_id", json.dumps(merged))

    def test_unknown_browser_bucket_omits_browser_identity(self):
        self.assertEqual(self.create_bucket("aw-watcher-web-custom")[0], 200)
        payload = {"timestamp": "2026-08-27T12:00:00Z", "duration": 0, "data": {"url": "https://example.com"}}
        self.assertEqual(self.request("POST", "/api/0/buckets/aw-watcher-web-custom/heartbeat", payload)[0], 200)
        self.assertEqual(self.events[0]["payload"], {"domain": "example.com"})

    def test_bucket_accepts_unicode_device_name_used_by_official_extension(self):
        bucket = "aw-watcher-web-chrome_联想小A"
        self.assertEqual(self.create_bucket(bucket)[0], 200)
        payload = {"timestamp": "2026-08-27T15:00:00Z", "duration": 10, "data": {"url": "https://www.bilibili.com/video/1"}}
        encoded = quote(bucket, safe="")
        self.assertEqual(self.request("POST", f"/api/0/buckets/{encoded}/heartbeat?pulsetime=20", payload)[0], 200)
        self.assertEqual(self.events[-1]["event_type"], "web.foreground")
        self.assertEqual(self.events[-1]["payload"]["domain"], "bilibili.com")
        self.assertEqual(self.events[-1]["payload"]["browser_app"]["process_name"], "chrome.exe")
        self.assertEqual(self.request("GET", f"/api/0/buckets/{encoded}")[0], 200)

    def test_info_bucket_read_and_cors_preflight(self):
        status, headers, info = self.request("GET", "/api/0/info")
        self.assertEqual((status, info["hostname"]), (200, "life-link"))
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(self.create_bucket()[0], 200)
        bucket = self.request("GET", "/api/0/buckets/aw-watcher-web-chrome")[2]
        self.assertEqual(bucket["type"], "web.tab.current")
        self.assertEqual(bucket["hostname"], "life-link")
        self.assertEqual(bucket["data"], {})
        self.assertIn("aw-watcher-web-chrome", self.request("GET", "/api/0/buckets/")[2])
        status, headers, _ = self.request("OPTIONS", "/api/0/info", extra_headers={"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)

    def test_rejects_untrusted_cors_bucket_and_url(self):
        self.assertEqual(self.request("GET", "/api/0/info", origin="http://evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/0/buckets/other", {"type": "web.tab.current"})[0], 400)
        self.assertEqual(self.create_bucket("aw-watcher-web-firefox")[0], 200)
        filtered = {"timestamp": "2026-08-27T12:00:00Z", "duration": 0, "data": {"url": "file:///private/file"}}
        status, _, response = self.request("POST", "/api/0/buckets/aw-watcher-web-firefox/heartbeat", filtered)
        self.assertEqual(status, 200)
        self.assertEqual(response["timestamp"], filtered["timestamp"])
        self.assertEqual(self.events, [])

    def test_incognito_is_accepted_without_an_event(self):
        self.assertEqual(self.create_bucket()[0], 200)
        payload = {"timestamp": "2026-08-27T12:00:00Z", "duration": 0, "data": {"url": "https://example.com", "incognito": True}}
        self.assertEqual(self.request("POST", "/api/0/buckets/aw-watcher-web-chrome/heartbeat", payload)[0], 200)
        self.assertEqual(self.events, [])

    def test_port_conflict_is_structured_and_stop_releases_listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        try:
            blocked = aw_web_compat.AWWebCompatReceiver(lambda event: None, port=sock.getsockname()[1])
            self.assertEqual(blocked.start()["status"], "port_in_use")
        finally:
            sock.close()
        port = self.port
        self.assertEqual(self.receiver.stop()["status"], "stopped")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)
