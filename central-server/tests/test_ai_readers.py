import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from unittest import mock

from central.ai_readers import CLAIM_SCHEMA, _arguments_contain_path_segments, is_loopback_address
from central.config import CentralConfig
from central.domain import canonical_json, utc_timestamp
from central.http import create_server


DEVICE_TOKEN = "registered-device-token-0123456789-ABCDEFGHIJK"
READ_TOKEN = "legacy-central-read-token-0123456789-ABCDEFGHI"


class AIReaderTests(unittest.TestCase):
    def test_hosted_application_match_uses_path_segments_not_keywords(self):
        self.assertTrue(_arguments_contain_path_segments([
            "node.exe", r"C:\Users\test\npm\node_modules\openclaw\dist\index.js",
        ], ["node_modules", "openclaw"]))
        self.assertTrue(_arguments_contain_path_segments([
            "node.exe", "D:/portable/node_modules/OpenClaw/dist/cli.js",
        ], ["node_modules", "openclaw"]))
        self.assertFalse(_arguments_contain_path_segments([
            "node.exe", r"D:\scripts\openclaw-check.js",
        ], ["node_modules", "openclaw"]))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "central.sqlite3"
        self.config = CentralConfig(
            database_path=self.database,
            host="127.0.0.1",
            port=0,
            token_bindings={DEVICE_TOKEN: "bootstrap-device"},
            read_token=READ_TOKEN,
        )
        self.server = create_server(self.config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, method, path, *, token=None, body=None):
        headers = {}
        encoded = None
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, payload, raw

    def create_and_claim(
        self, *, reader_type="codex", instance_id="agent-main",
        display_name="My Reader", process_identity=None, process_binding=None,
    ):
        pairing = self.server.store.ai_readers.create_pairing(
            claim_url=(
                f"http://127.0.0.1:{self.server.server_port}"
                "/v1/ai-readers/pairings/claim"
            )
        )
        pairing_text = json.loads(pairing.text)
        status, profile, _ = self.request(
            "POST",
            "/v1/ai-readers/pairings/claim",
            token=pairing_text["pairing_token"],
            body={
                "schema_version": CLAIM_SCHEMA,
                "pairing_id": pairing_text["pairing_id"],
                "reader": {
                    "type": reader_type,
                    "instance_id": instance_id,
                    "display_name": display_name,
                    **({"process_identity": process_identity} if process_identity else {}),
                    **({"process_binding": process_binding} if process_binding else {}),
                },
            },
        )
        self.assertEqual(status, 200, profile)
        return pairing_text, profile

    def test_process_status_uses_the_binding_saved_by_pairing(self):
        _, profile = self.create_and_claim(instance_id="process-reader")
        status, payload, _ = self.request(
            "GET",
            f"/v1/ai-readers/{profile['reader_id']}/process-status",
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["process_running"])
        self.assertIsNone(payload["process_display_name"])

        binding = {
            "strategy": "hosted-argument",
            "display_name": "OpenClaw",
            "process_name": "node.exe",
            "argument_path_segments": ["node_modules", "openclaw"],
        }
        _, profile = self.create_and_claim(
            instance_id="process-reader-2", process_binding=binding
        )
        with mock.patch(
            "central.ai_readers.detect_process_binding", return_value=True
        ) as checker:
            status, payload, _ = self.request(
                "GET",
                f"/v1/ai-readers/{profile['reader_id']}/process-status",
                token=DEVICE_TOKEN,
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["process_running"])
        self.assertEqual(payload["process_display_name"], "OpenClaw")
        checker.assert_called_once_with(binding)

    def test_process_status_requires_registered_device_and_hides_unknown_reader(self):
        _, profile = self.create_and_claim(instance_id="protected-process-reader")
        status, payload, _ = self.request(
            "GET",
            f"/v1/ai-readers/{profile['reader_id']}/process-status",
        )
        self.assertEqual(status, 401, payload)
        status, payload, _ = self.request(
            "GET",
            "/v1/ai-readers/00000000-0000-0000-0000-000000000000/process-status",
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 404, payload)

    def insert_timeline(
        self,
        *,
        occurred_at,
        created_at,
        event_key="custom.test",
        importance="normal",
        title="Test event",
        detail="detail",
        evidence=None,
        wish_id=None,
    ):
        event_id = str(uuid.uuid4())
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO timeline_events(
                    timeline_event_id, occurred_at, created_at, event_key,
                    category, importance, title, detail, source_kind,
                    source_device_id, wish_id, trigger_id, subject_json,
                    evidence_json, statistics_window_json, delivery_json,
                    dedupe_key
                ) VALUES (?, ?, ?, ?, 'system', ?, ?, ?,
                          'central', NULL, ?, NULL, '{}', ?, NULL, NULL, ?)
                """,
                (
                    event_id,
                    occurred_at,
                    created_at,
                    event_key,
                    importance,
                    title,
                    detail,
                    wish_id,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    f"test:{event_id}",
                ),
            )
            connection.commit()
        return event_id

    def test_update_flag_tracks_unread_high_priority_events_without_advancing_cursor(self):
        _, profile = self.create_and_claim(instance_id="update-reader")
        status, first, _ = self.request(
            "GET", "/v1/read/ai/context?view=compact", token=profile["access_token"]
        )
        self.assertEqual(status, 200, first)
        now = datetime.now(timezone.utc)
        self.insert_timeline(
            occurred_at=utc_timestamp(now), created_at=utc_timestamp(now + timedelta(seconds=1)),
            event_key="wish.scheduled_reminder", importance="high", title="Reminder",
        )
        query = urlencode({"cursor": first["next_cursor"]})
        status, pending, _ = self.request(
            "GET", f"/v1/read/ai/updates?{query}", token=profile["access_token"]
        )
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending, {"update_mcp": True})
        status, served, _ = self.request(
            "GET", f"/v1/read/ai/context?view=compact&{query}", token=profile["access_token"]
        )
        self.assertEqual(status, 200, served)
        status, cleared, _ = self.request(
            "GET", f"/v1/read/ai/updates?{urlencode({'cursor': served['next_cursor']})}",
            token=profile["access_token"],
        )
        self.assertEqual(status, 200, cleared)
        self.assertEqual(cleared, {"update_mcp": False})

    def test_update_flag_includes_normal_priority_wish_events(self):
        _, profile = self.create_and_claim(instance_id="wish-update-reader")
        status, first, _ = self.request(
            "GET", "/v1/read/ai/context?view=compact", token=profile["access_token"]
        )
        self.assertEqual(status, 200, first)
        now = datetime.now(timezone.utc)
        self.insert_timeline(
            occurred_at=utc_timestamp(now), created_at=utc_timestamp(now + timedelta(seconds=1)),
            event_key="wish.related_event", importance="normal", wish_id=str(uuid.uuid4()),
        )
        status, pending, _ = self.request(
            "GET", f"/v1/read/ai/updates?{urlencode({'cursor': first['next_cursor']})}",
            token=profile["access_token"],
        )
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending, {"update_mcp": True})

    def test_pairing_is_short_lived_claims_once_and_stores_only_hashes(self):
        pairing, profile = self.create_and_claim()

        self.assertEqual(
            set(pairing),
            {
                "schema_version",
                "central_instance_id",
                "central_display_name",
                "claim_url",
                "pairing_id",
                "pairing_token",
                "expires_at",
                "instructions",
                "claim_request_body_template",
            },
        )
        self.assertEqual(pairing["schema_version"], "life-radio-ai-reader-pairing-v1")
        self.assertEqual(
            pairing["claim_request_body_template"]["pairing_id"],
            pairing["pairing_id"],
        )
        self.assertEqual(
            pairing["claim_request_body_template"]["schema_version"], CLAIM_SCHEMA
        )
        self.assertIn("central_instance_id", pairing["instructions"][0])
        self.assertEqual(pairing["central_display_name"], "Life Link Central")
        self.assertIn("Life Link", pairing["instructions"][0])
        self.assertTrue(pairing["central_instance_id"].startswith("central-"))
        self.assertEqual(
            pairing["claim_url"],
            f"http://127.0.0.1:{self.server.server_port}/v1/ai-readers/pairings/claim",
        )
        self.assertEqual(
            set(profile), {"access_token", "reader_id", "expires_at", "context_url"}
        )
        self.assertTrue(profile["context_url"].endswith("/v1/read/ai/context"))
        expires = datetime.fromisoformat(profile["expires_at"][:-1] + "+00:00")
        pairing_expires = datetime.fromisoformat(
            pairing["expires_at"][:-1] + "+00:00"
        )
        with closing(sqlite3.connect(self.database)) as connection:
            pairing_created_at, paired_at = connection.execute(
                "SELECT created_at FROM ai_reader_pairings WHERE pairing_id = ?",
                (pairing["pairing_id"],),
            ).fetchone()[0], connection.execute(
                "SELECT paired_at FROM ai_readers WHERE reader_id = ?",
                (profile["reader_id"],),
            ).fetchone()[0]
        issued = datetime.fromisoformat(paired_at[:-1] + "+00:00")
        self.assertEqual(expires - issued, timedelta(days=90))
        pairing_created = datetime.fromisoformat(
            pairing_created_at[:-1] + "+00:00"
        )
        self.assertEqual(pairing_expires - pairing_created, timedelta(hours=24))

        status, retry, _ = self.request(
            "POST",
            "/v1/ai-readers/pairings/claim",
            token=pairing["pairing_token"],
            body={
                "schema_version": CLAIM_SCHEMA,
                "pairing_id": pairing["pairing_id"],
                "reader": {
                    "type": "codex",
                    "instance_id": "agent-main",
                    "display_name": "My Reader",
                },
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(retry["error"], "ai_reader_pairing_already_claimed")

        with closing(sqlite3.connect(self.database)) as connection:
            stored = "|".join(
                str(value)
                for table in (
                    "ai_reader_pairings",
                    "ai_readers",
                    "ai_reader_cursors",
                    "ai_reader_access_logs",
                )
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
                for value in row
                if value is not None
            )
            reader_hash = connection.execute(
                "SELECT token_hash FROM ai_readers WHERE reader_id = ?",
                (profile["reader_id"],),
            ).fetchone()[0]
        self.assertNotIn(pairing["pairing_token"], stored)
        self.assertNotIn(profile["access_token"], stored)
        self.assertEqual(
            reader_hash, hashlib.sha256(profile["access_token"].encode()).hexdigest()
        )
        self.assertTrue(is_loopback_address("127.0.0.1"))
        self.assertTrue(is_loopback_address("::1"))
        self.assertFalse(is_loopback_address("192.0.2.1"))

        status, rejected, _ = self.request(
            "GET", "/v1/read/ai/context", token=READ_TOKEN
        )
        self.assertEqual(status, 401)
        self.assertEqual(rejected["error"], "invalid_ai_reader_token")
        status, rejected, _ = self.request(
            "GET", "/v1/read/devices", token=profile["access_token"]
        )
        self.assertEqual(status, 401)
        self.assertEqual(rejected["error"], "invalid_read_token")

    def test_context_uses_opaque_created_order_cursor_and_audits_served_metadata(self):
        now = datetime.now(timezone.utc)
        initial_id = self.insert_timeline(
            occurred_at=utc_timestamp(now),
            created_at=utc_timestamp(now - timedelta(minutes=2)),
            event_key="report.morning",
            importance="high",
        )
        _, profile = self.create_and_claim()

        status, first, first_raw = self.request(
            "GET", "/v1/read/ai/context?view=full", token=profile["access_token"]
        )
        self.assertEqual(status, 200, first)
        self.assertEqual(
            set(first), {"background", "events", "understanding", "next_cursor", "importance_counts"}
        )
        self.assertLess(list(first).index("understanding"), list(first).index("background"))
        self.assertEqual(
            [event["timeline_event_id"] for event in first["events"]],
            [initial_id],
        )
        self.assertEqual(first["importance_counts"], {"high": 1, "normal": 0, "low": 0})
        self.assertIn("background_summary", first["background"])
        self.assertFalse(first["understanding"]["unchanged"])
        cursor = first["next_cursor"]

        with closing(sqlite3.connect(self.database)) as connection:
            stored_cursor = connection.execute(
                "SELECT cursor_hash, position_created_at, timeline_event_id FROM ai_reader_cursors"
            ).fetchone()
        self.assertNotEqual(stored_cursor[0], cursor)
        self.assertEqual(stored_cursor[0], hashlib.sha256(cursor.encode()).hexdigest())
        self.assertEqual(stored_cursor[2], initial_id)

        backfilled_id = self.insert_timeline(
            occurred_at=utc_timestamp(now - timedelta(minutes=1)),
            created_at=utc_timestamp(now - timedelta(minutes=1)),
            importance="low",
        )
        query = urlencode(
            {
                "cursor": cursor,
                "understanding_version": first["understanding"]["version"],
                "view": "full",
            }
        )
        status, second, second_raw = self.request(
            "GET", f"/v1/read/ai/context?{query}", token=profile["access_token"]
        )
        self.assertEqual(status, 200, second)
        self.assertEqual(second["events"], [])
        self.assertEqual(second["importance_counts"], {"high": 0, "normal": 0, "low": 0})
        self.assertEqual(
            second["understanding"],
            {
                "version": first["understanding"]["version"],
                "unchanged": True,
            },
        )

        status, logs, _ = self.request(
            "GET",
            f"/v1/ai-readers/{profile['reader_id']}/access-logs",
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 200, logs)
        self.assertEqual(len(logs["logs"]), 2)
        latest = logs["logs"][0]
        self.assertEqual(
            set(latest),
            {
                "access_log_id", "request_id", "requested_at", "completed_at",
                "result", "cursor_epoch", "business_date", "requested_position",
                "served_position", "served_event_ids", "served_report_ids",
                "importance_counts", "background_generated_at",
                "understanding_version", "response_hash", "response_bytes",
                "duration_ms",
            },
        )
        self.assertEqual(latest["result"], "served")
        self.assertEqual(latest["cursor_epoch"], 1)
        self.assertEqual(latest["served_event_ids"], [])
        self.assertEqual(latest["served_report_ids"], [])
        self.assertEqual(latest["response_hash"], hashlib.sha256(second_raw).hexdigest())
        self.assertEqual(latest["response_bytes"], len(second_raw))
        older = logs["logs"][1]
        self.assertEqual(older["served_report_ids"], [initial_id])
        self.assertEqual(older["response_hash"], hashlib.sha256(first_raw).hexdigest())
        serialized_logs = canonical_json(logs)
        self.assertNotIn(profile["access_token"], serialized_logs)
        self.assertNotIn(cursor, serialized_logs)

        status, timeline, _ = self.request(
            "GET",
            "/v1/timeline-events?" + urlencode({
                "from": utc_timestamp(now - timedelta(hours=1)),
                "to": utc_timestamp(now + timedelta(hours=1)),
            }),
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 200, timeline)
        by_id = {event["timeline_event_id"]: event for event in timeline["events"]}
        self.assertEqual(by_id[initial_id]["ai_reader"]["state"], "served")
        self.assertEqual(by_id[backfilled_id]["ai_reader"]["state"], "not_applicable")
        audit = [event for event in timeline["events"] if event["event_key"].startswith("system.ai_reader_")]
        self.assertTrue(audit)
        self.assertTrue(all(event["importance"] == "low" for event in audit))
        self.assertTrue(all(event["ai_reader"]["state"] == "not_applicable" for event in audit))

        status, cleared, _ = self.request(
            "POST",
            f"/v1/ai-readers/{profile['reader_id']}/clear-reading-progress",
            token=DEVICE_TOKEN,
            body={},
        )
        self.assertEqual(status, 200, cleared)
        self.assertEqual(cleared["reader"]["cursor_epoch"], 2)
        status, superseded, _ = self.request(
            "GET", f"/v1/read/ai/context?cursor={second['next_cursor']}",
            token=profile["access_token"],
        )
        self.assertEqual(status, 409, superseded)
        self.assertEqual(superseded["error"], "cursor_superseded")
        status, reread, _ = self.request(
            "GET", "/v1/read/ai/context?view=full", token=profile["access_token"]
        )
        self.assertEqual(status, 200, reread)
        self.assertEqual(
            {event["timeline_event_id"] for event in reread["events"]},
            {initial_id},
        )
        self.assertFalse(any(event["event_key"].startswith("system.ai_reader_") for event in reread["events"]))

    def test_context_preview_follows_latest_cursor_without_side_effects(self):
        now = datetime.now(timezone.utc)
        first_id = self.insert_timeline(
            occurred_at=utc_timestamp(now),
            created_at=utc_timestamp(now - timedelta(minutes=2)),
            title="First",
        )
        self.insert_timeline(
            occurred_at=utc_timestamp(now),
            created_at=utc_timestamp(now - timedelta(minutes=1)),
            importance="low",
            title="Hidden low event",
        )
        _, profile = self.create_and_claim(instance_id="preview-reader")

        status, preview, _ = self.request(
            "GET",
            f"/v1/ai-readers/{profile['reader_id']}/context-preview?view=compact",
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview["preview_only"])
        self.assertFalse(preview["side_effects"])
        self.assertEqual([item["text"] for item in preview["context"]["events"]], ["First：detail"])
        self.assertEqual(preview["context"]["next_cursor"], "<正式读取时由中央签发>")

        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_reader_cursors").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_reader_access_logs").fetchone()[0], 0)

        status, served, _ = self.request(
            "GET", "/v1/read/ai/context", token=profile["access_token"]
        )
        self.assertEqual(status, 200, served)
        self.assertEqual([item["text"] for item in served["events"]], ["First：detail"])
        second_id = self.insert_timeline(
            occurred_at=utc_timestamp(now + timedelta(seconds=1)),
            created_at=utc_timestamp(now + timedelta(seconds=1)),
            title="Second",
        )
        with closing(sqlite3.connect(self.database)) as connection:
            reader_state_before = connection.execute(
                """
                SELECT cursor_epoch, last_requested_at,
                       last_requested_cursor_created_at,
                       last_requested_timeline_event_id,
                       last_served_at, last_served_cursor_created_at,
                       last_served_timeline_event_id
                FROM ai_readers WHERE reader_id = ?
                """,
                (profile["reader_id"],),
            ).fetchone()
            audit_count_before = connection.execute(
                "SELECT COUNT(*) FROM timeline_events WHERE event_key LIKE 'system.ai_reader_%'"
            ).fetchone()[0]

        status, preview, _ = self.request(
            "GET",
            f"/v1/ai-readers/{profile['reader_id']}/context-preview",
            token=DEVICE_TOKEN,
        )
        self.assertEqual(status, 200, preview)
        self.assertEqual([item["text"] for item in preview["context"]["events"]], ["Second：detail"])
        self.assertFalse(preview["context"]["understanding"]["unchanged"])
        self.assertTrue(preview["context"]["understanding"]["items"])
        self.assertLess(
            list(preview["context"]).index("understanding"),
            list(preview["context"]).index("background"),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_reader_cursors").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_reader_access_logs").fetchone()[0], 1)
            served_ids = json.loads(connection.execute(
                "SELECT served_event_ids_json FROM ai_reader_access_logs"
            ).fetchone()[0])
            reader_state_after = connection.execute(
                """
                SELECT cursor_epoch, last_requested_at,
                       last_requested_cursor_created_at,
                       last_requested_timeline_event_id,
                       last_served_at, last_served_cursor_created_at,
                       last_served_timeline_event_id
                FROM ai_readers WHERE reader_id = ?
                """,
                (profile["reader_id"],),
            ).fetchone()
            audit_count_after = connection.execute(
                "SELECT COUNT(*) FROM timeline_events WHERE event_key LIKE 'system.ai_reader_%'"
            ).fetchone()[0]
        self.assertEqual(served_ids, [first_id])
        self.assertEqual(reader_state_after, reader_state_before)
        self.assertEqual(audit_count_after, audit_count_before)
        self.assertNotEqual(second_id, first_id)

        status, forbidden, _ = self.request(
            "GET",
            f"/v1/ai-readers/{profile['reader_id']}/context-preview",
            token=profile["access_token"],
        )
        self.assertEqual(status, 401, forbidden)

    def test_registered_device_can_generate_pairing_text(self):
        status, payload, _ = self.request(
            "POST", "/v1/ai-readers/pairings", token=DEVICE_TOKEN, body={}
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(set(payload), {"pairing_text", "expires_at", "central_instance_id"})
        pairing = json.loads(payload["pairing_text"])
        self.assertEqual(pairing["central_instance_id"], payload["central_instance_id"])
        self.assertIn("next_cursor", pairing["instructions"][2])
        self.assertIn("understanding.version", pairing["instructions"][2])
        self.assertIn("立即", pairing["instructions"][2])
        self.assertIn("UTF-8", pairing["instructions"][2])
        self.assertIn("process_binding", pairing["instructions"][1])
        process_template = pairing["claim_request_body_template"]["reader"]["process_binding"]
        self.assertEqual(process_template["strategy"], "hosted-argument")
        self.assertIn("REPLACE", process_template["process_name"])

        _, profile = self.create_and_claim(
            instance_id="settings-reader", display_name="塔洛"
        )
        status, settings, _ = self.request(
            "GET", "/v1/settings/shared", token=DEVICE_TOKEN
        )
        self.assertEqual(status, 200, settings)
        self.assertEqual(settings["ai_display_name"], "塔洛")
        status, rejected, _ = self.request(
            "POST", "/v1/settings/shared", token=DEVICE_TOKEN,
            body={"ai_display_name": "手动名称"},
        )
        self.assertEqual(status, 400, rejected)
        self.assertEqual(rejected["error"], "invalid_settings_patch")
        self.assertEqual(profile["reader_id"], self.server.store.ai_readers.primary_reader()["reader_id"])

    def test_repair_without_process_binding_preserves_existing_binding(self):
        binding = {
            "strategy": "hosted-argument",
            "display_name": "OpenClaw",
            "process_name": "node.exe",
            "argument_path_segments": ["node_modules", "openclaw"],
        }
        _, first = self.create_and_claim(
            instance_id="preserve-process-reader", process_binding=binding
        )
        _, second = self.create_and_claim(instance_id="preserve-process-reader")
        self.assertEqual(first["reader_id"], second["reader_id"])
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT process_binding_json "
                "FROM ai_readers WHERE reader_id = ?",
                (first["reader_id"],),
            ).fetchone()[0]
        self.assertEqual(json.loads(row), binding)

    def test_new_reader_claim_atomically_replaces_the_connected_ai(self):
        _, first_profile = self.create_and_claim(
            instance_id="reader-one", display_name="First AI"
        )
        status, first_context, _ = self.request(
            "GET", "/v1/read/ai/context", token=first_profile["access_token"]
        )
        self.assertEqual(status, 200, first_context)

        _, second_profile = self.create_and_claim(
            instance_id="reader-two", display_name="Second AI"
        )
        status, rejected, _ = self.request(
            "GET", "/v1/read/ai/context", token=first_profile["access_token"]
        )
        self.assertEqual(status, 401, rejected)
        self.assertEqual(rejected["error"], "invalid_ai_reader_token")

        status, second_context, _ = self.request(
            "GET", "/v1/read/ai/context", token=second_profile["access_token"]
        )
        self.assertEqual(status, 200, second_context)
        status, readers, _ = self.request(
            "GET", "/v1/ai-readers", token=DEVICE_TOKEN
        )
        self.assertEqual(status, 200, readers)
        active = [item for item in readers["readers"] if item["status"] == "active"]
        self.assertEqual([item["reader_id"] for item in active], [second_profile["reader_id"]])
        first = next(
            item for item in readers["readers"]
            if item["reader_id"] == first_profile["reader_id"]
        )
        self.assertEqual(first["status"], "revoked")
        self.assertEqual(first["cursor_epoch"], 2)
        self.assertEqual(
            self.server.store.ai_readers.primary_reader()["reader_id"],
            second_profile["reader_id"],
        )

    def test_cursorless_read_accepts_an_explicit_business_date(self):
        now = datetime.now(timezone.utc)
        historical = (now - timedelta(days=3)).date().isoformat()
        event_id = self.insert_timeline(
            occurred_at=f"{historical}T12:00:00Z",
            created_at=utc_timestamp(now - timedelta(minutes=1)),
        )
        _, profile = self.create_and_claim(instance_id="historical-reader")

        status, payload, _ = self.request(
            "GET",
            f"/v1/read/ai/context?business_date={historical}&view=full",
            token=profile["access_token"],
        )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["background"]["business_date"], historical)
        self.assertEqual([item["timeline_event_id"] for item in payload["events"]], [event_id])
        status, invalid, _ = self.request(
            "GET",
            "/v1/read/ai/context?business_date=2026-8-1",
            token=profile["access_token"],
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"], "invalid_ai_context_request")

    def test_compact_view_uses_local_time_and_omits_event_ids_and_keys(self):
        now = datetime.now(timezone.utc)
        business_date = self.server.store.event_background()["business_date"]
        local_zone = timezone(timedelta(hours=8))
        ordinary_at = datetime.fromisoformat(
            f"{business_date}T12:38:13+08:00"
        ).astimezone(timezone.utc)
        report_at = datetime.fromisoformat(
            f"{business_date}T12:40:00+08:00"
        ).astimezone(timezone.utc)
        ordinary_id = self.insert_timeline(
            occurred_at=utc_timestamp(ordinary_at),
            created_at=utc_timestamp(now - timedelta(minutes=2)),
            title="心愿已完成",
            detail="完成 2/3 天。",
        )
        report_id = self.insert_timeline(
            occurred_at=utc_timestamp(report_at),
            created_at=utc_timestamp(now - timedelta(minutes=1)),
            event_key="report.periodic",
            importance="high",
            title="定时总结",
            detail="等待接入。",
            evidence={"body": "【定时总结】\n只保留报告正文。"},
        )
        _, profile = self.create_and_claim(instance_id="compact-reader")

        status, compact, _ = self.request(
            "GET", "/v1/read/ai/context", token=profile["access_token"]
        )

        self.assertEqual(status, 200, compact)
        self.assertEqual(
            set(compact),
            {
                "business_date", "timezone", "generated_at",
                "generated_at_label", "background", "current", "events",
                "understanding", "next_cursor",
            },
        )
        self.assertEqual(compact["timezone"], "Asia/Shanghai")
        self.assertLess(
            list(compact).index("understanding"), list(compact).index("background")
        )
        self.assertTrue(compact["generated_at"].endswith("+08:00"))
        self.assertEqual(len(compact["events"]), 2)
        self.assertEqual(
            set(compact["events"][0]), {"at", "importance", "text"}
        )
        self.assertEqual(
            compact["events"][0]["at"],
            ordinary_at.astimezone(local_zone).isoformat(),
        )
        self.assertEqual(compact["events"][0]["text"], "心愿已完成：完成 2/3 天。")
        self.assertEqual(
            compact["events"][1]["text"], "【定时总结】\n只保留报告正文。"
        )
        serialized = canonical_json(compact)
        self.assertNotIn(ordinary_id, serialized)
        self.assertNotIn(report_id, serialized)
        self.assertNotIn("report.periodic", serialized)

        status, incremental, _ = self.request(
            "GET",
            "/v1/read/ai/context?"
            + urlencode(
                {
                    "view": "compact",
                    "cursor": compact["next_cursor"],
                    "understanding_version": compact["understanding"]["version"],
                }
            ),
            token=profile["access_token"],
        )
        self.assertEqual(status, 200, incremental)
        self.assertEqual(incremental["events"], [])
        self.assertEqual(
            incremental["understanding"],
            {
                "version": compact["understanding"]["version"],
                "unchanged": True,
            },
        )
        status, invalid, _ = self.request(
            "GET", "/v1/read/ai/context?view=short", token=profile["access_token"]
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"], "invalid_ai_context_request")

    def test_microsecond_cursor_order_and_expiry_errors_are_explicit(self):
        now = datetime.now(timezone.utc).replace(microsecond=100000)
        first_id = self.insert_timeline(
            occurred_at=utc_timestamp(now),
            created_at=utc_timestamp(now),
        )
        pairing, profile = self.create_and_claim(instance_id="expiry-reader")
        status, first, _ = self.request(
            "GET", "/v1/read/ai/context?view=full", token=profile["access_token"]
        )
        self.assertEqual(status, 200, first)
        self.assertIn(first_id, [item["timeline_event_id"] for item in first["events"]])

        second_id = self.insert_timeline(
            occurred_at=utc_timestamp(now),
            created_at=utc_timestamp(now + timedelta(microseconds=100000)),
        )
        status, second, _ = self.request(
            "GET",
            f"/v1/read/ai/context?{urlencode({'cursor': first['next_cursor'], 'view': 'full'})}",
            token=profile["access_token"],
        )
        self.assertEqual(status, 200, second)
        self.assertEqual(
            [item["timeline_event_id"] for item in second["events"]], [second_id]
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE ai_reader_cursors SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE cursor_hash = ?",
                (hashlib.sha256(second["next_cursor"].encode()).hexdigest(),),
            )
            connection.commit()
        status, expired_cursor, _ = self.request(
            "GET",
            f"/v1/read/ai/context?{urlencode({'cursor': second['next_cursor']})}",
            token=profile["access_token"],
        )
        self.assertEqual(status, 410)
        self.assertEqual(expired_cursor["error"], "cursor_expired")

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE ai_readers SET token_expires_at = '2000-01-01T00:00:00Z' "
                "WHERE reader_id = ?",
                (profile["reader_id"],),
            )
            connection.commit()
        status, expired_token, _ = self.request(
            "GET", "/v1/read/ai/context", token=profile["access_token"]
        )
        self.assertEqual(status, 401)
        self.assertEqual(expired_token["error"], "ai_reader_token_expired")

        unclaimed = self.server.store.ai_readers.create_pairing(
            claim_url=(
                f"http://127.0.0.1:{self.server.server_port}"
                "/v1/ai-readers/pairings/claim"
            )
        )
        pairing_text = json.loads(unclaimed.text)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE ai_reader_pairings SET expires_at = '2000-01-01T00:00:00Z' "
                "WHERE pairing_id = ?",
                (pairing_text["pairing_id"],),
            )
            connection.commit()
        status, expired_pairing, _ = self.request(
            "POST",
            "/v1/ai-readers/pairings/claim",
            token=pairing_text["pairing_token"],
            body={
                "schema_version": CLAIM_SCHEMA,
                "pairing_id": pairing_text["pairing_id"],
                "reader": {
                    "type": "codex",
                    "instance_id": "expired-pairing",
                    "display_name": "Expired Pairing",
                },
            },
        )
        self.assertEqual(status, 410)
        self.assertEqual(expired_pairing["error"], "ai_reader_pairing_expired")


if __name__ == "__main__":
    unittest.main()


