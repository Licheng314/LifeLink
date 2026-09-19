import hashlib
import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from central.config import CentralConfig
from central.http import create_server


TOKEN_A = "photo-device-a-token-0123456789-ABCDEFGHI"
TOKEN_B = "photo-device-b-token-0123456789-ABCDEFGHI"


class PhotoStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = CentralConfig(database_path=Path(self.temp.name) / "central.sqlite3", token_bindings={TOKEN_A: "android-a", TOKEN_B: "android-b"})
        self.server = create_server(config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    @staticmethod
    def png(): return b"\x89PNG\r\n\x1a\n" + b"test-image"

    def metadata(self, body):
        return {"captured_at":"2026-09-18T02:15:30Z", "time_source":"captured", "mime_type":"image/png", "width":10, "height":20, "byte_size":len(body), "sha256":hashlib.sha256(body).hexdigest()}

    def request(self, method, path, *, token=None, body=None, headers=None):
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_http_routes_require_device_auth_and_preserve_source_ownership(self):
        status, _ = self.request("GET", "/v1/photos")
        self.assertEqual(status, 401)

        body = self.png(); photo_id = str(uuid.uuid4()); sync_id = str(uuid.uuid4())
        metadata = self.metadata(body)
        headers = {
            "Content-Type": metadata["mime_type"],
            "X-Photo-Sync-Id": sync_id,
            "X-Photo-Captured-At": metadata["captured_at"],
            "X-Photo-Time-Source": metadata["time_source"],
            "X-Photo-Mime-Type": metadata["mime_type"],
            "X-Photo-Width": str(metadata["width"]),
            "X-Photo-Height": str(metadata["height"]),
            "X-Photo-Byte-Size": str(metadata["byte_size"]),
            "X-Photo-Sha256": metadata["sha256"],
        }
        status, uploaded = self.request("POST", f"/v1/photos/{photo_id}/content", token=TOKEN_A, body=body, headers=headers)
        self.assertEqual(status, 201)
        self.assertEqual(uploaded["photo"]["source_device_id"], "android-a")

        status, other_device = self.request("GET", "/v1/photos", token=TOKEN_B)
        self.assertEqual(status, 200)
        self.assertEqual(other_device["photos"], [])

        status, deleted = self.request("POST", f"/v1/photos/{photo_id}/delete", token=TOKEN_A, headers={"X-Photo-Sync-Id": sync_id})
        self.assertEqual(status, 200)
        self.assertEqual(deleted["photo"]["status"], "deleted")

    def test_upload_delete_cross_device_isolation_and_sync_event_idempotency(self):
        photo_id, sync_id = str(uuid.uuid4()), str(uuid.uuid4()); body = self.png()
        stored, changed = self.server.photos.upload("android-a", photo_id, self.metadata(body), body, sync_id)
        self.assertTrue(changed); self.assertEqual(stored["status"], "active")
        duplicate, changed = self.server.photos.upload("android-a", photo_id, self.metadata(body), body, sync_id)
        self.assertFalse(changed); self.assertEqual(duplicate["sha256"], stored["sha256"])
        self.assertIsNone(self.server.photos.delete("android-b", photo_id, str(uuid.uuid4()))[0])
        first = self.server.photos.complete("android-a", sync_id)
        second = self.server.photos.complete("android-a", sync_id)
        self.assertEqual(first, second)
        self.assertEqual(first["added_count"], 1)
        with self.server.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM timeline_events WHERE event_key='photo.sync_confirmed'").fetchone()[0], 1)
        deleted, changed = self.server.photos.delete("android-a", photo_id, str(uuid.uuid4()))
        self.assertTrue(changed); self.assertEqual(deleted["photo_id"], photo_id)
        self.assertEqual(deleted["status"], "deleted")
        self.assertIsNotNone(deleted["deleted_at"])
        page = self.server.photos.list(source_device_id="android-a", cursor=None, limit=30)
        self.assertEqual(page["photos"][0]["status"], "deleted")

    def test_rejects_disguised_or_oversized_content_and_paginates(self):
        sync = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, "signature"):
            body = b"not an image"
            self.server.photos.upload("android-a", str(uuid.uuid4()), self.metadata(body), body, sync)
        for _ in range(2):
            body = self.png(); self.server.photos.upload("android-a", str(uuid.uuid4()), self.metadata(body), body, sync)
        page = self.server.photos.list(source_device_id="android-a", cursor=None, limit=1)
        self.assertEqual(len(page["photos"]), 1); self.assertIsNotNone(page["next_cursor"])
        next_page = self.server.photos.list(source_device_id="android-a", cursor=page["next_cursor"], limit=1)
        self.assertEqual(len(next_page["photos"]), 1)
        with self.assertRaisesRegex(ValueError, "maximum"):
            self.server.photos.upload("android-a", str(uuid.uuid4()), {**self.metadata(self.png()), "byte_size": 20 * 1024 * 1024 + 1}, self.png(), sync)

    def test_requires_utc_capture_time_and_restores_missing_content_on_retry(self):
        body = self.png(); photo_id = str(uuid.uuid4()); sync_id = str(uuid.uuid4())
        non_utc = {**self.metadata(body), "captured_at": "2026-09-18T10:15:30+08:00"}
        with self.assertRaisesRegex(ValueError, "UTC"):
            self.server.photos.upload("android-a", photo_id, non_utc, body, sync_id)

        stored, changed = self.server.photos.upload("android-a", photo_id, self.metadata(body), body, sync_id)
        self.assertTrue(changed)
        with self.server.store._connection() as connection:
            name = connection.execute(
                "SELECT file_name FROM photos WHERE source_device_id=? AND photo_id=?",
                ("android-a", photo_id),
            ).fetchone()[0]
        path = self.server.photos.root / name
        path.unlink()
        duplicate, changed = self.server.photos.upload("android-a", photo_id, self.metadata(body), body, sync_id)
        self.assertFalse(changed)
        self.assertEqual(duplicate["sha256"], stored["sha256"])
        self.assertEqual(path.read_bytes(), body)


if __name__ == "__main__":
    unittest.main()
