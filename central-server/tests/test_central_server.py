import copy
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import central_server
from central.config import CentralConfig
from central.http import MissingDeviceTokenError, create_server


DESKTOP_TOKEN = "desktop-test-token-0123456789-ABCDEFGH"
PHONE_TOKEN = "phone-test-token-0123456789-ABCDEFGHIJK"
WRONG_TOKEN = "wrong-test-token-0123456789-ABCDEFGHIJK"


class CentralServerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "central.sqlite3"
        self.config = CentralConfig(
            database_path=self.database_path,
            host="127.0.0.1",
            port=0,
            token_bindings={
                DESKTOP_TOKEN: "desktop-install-a",
                PHONE_TOKEN: "android-install-b",
            },
        )
        self.server = create_server(self.config, ("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, method, path, payload=None, *, token=None, idempotency_key=None):
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=5,
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        return response.status, json.loads(response_body.decode("utf-8"))

    @staticmethod
    def aw_event(event_id=None, *, revision=1, duration=10):
        return {
            "event_id": event_id or str(uuid.uuid4()),
            "occurred_at": "2026-07-30T14:10:00Z",
            "event_type": "app.foreground",
            "source": {
                "kind": "desktop",
                "collector": "activitywatch",
                "reliability": "observed",
            },
            "duration_seconds": duration,
            "revision": revision,
            "payload": {
                "app": {
                    "package_name": "chrome.exe",
                    "display_name": "Google Chrome",
                },
                "activitywatch": {
                    "kind": "window",
                    "bucket_id": "aw-watcher-window_desktop",
                    "event_id": 42,
                },
            },
        }

    @staticmethod
    def location_observation(event_id=None, *, revision=0, latitude=31.23):
        return {
            "event_id": event_id or str(uuid.uuid4()),
            "occurred_at": "2026-07-30T14:10:00Z",
            "event_type": "location.observation",
            "source": {
                "kind": "android",
                "collector": "fused_location",
                "reliability": "observed",
            },
            "revision": revision,
            "payload": {
                "kind": "observation",
                "latitude": latitude,
                "longitude": 121.47,
                "accuracy_m": 20.0,
            },
        }

    @staticmethod
    def location_segment(event_type, event_id=None, *, revision=1, duration=900):
        return {
            "event_id": event_id or str(uuid.uuid4()),
            "occurred_at": "2026-07-30T14:10:00Z",
            "event_type": event_type,
            "source": {
                "kind": "android",
                "collector": "fused_location",
                "reliability": "observed",
            },
            "duration_seconds": duration,
            "revision": revision,
            "payload": {
                "kind": "stay" if event_type == "location.stay" else "sample",
                "latitude": 31.23,
                "longitude": 121.47,
                "accuracy_m": 20.0,
                "observed_until": "2026-07-30T14:25:00Z",
            },
        }

    @staticmethod
    def batch(device_id, events, *, batch_id=None, sent_at="2026-07-30T14:20:10Z"):
        return {
            "schema_version": "v1",
            "batch_id": batch_id or str(uuid.uuid4()),
            "device": {
                "device_id": device_id,
                "platform": "android" if device_id.startswith("android") else "desktop",
                "display_name": device_id,
            },
            "sent_at": sent_at,
            "events": events,
        }

    def upload(self, payload, token):
        return self.request(
            "POST",
            "/v1/events/batches",
            payload,
            token=token,
            idempotency_key=payload["batch_id"],
        )

    def test_health_reports_central_role_and_no_p2p_capabilities(self):
        status, health = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["role"], "central")
        self.assertEqual(health["api_version"], "v1")
        self.assertEqual(
            health["capabilities"],
            {
                "legacy_push": False,
                "activitywatch_collection": False,
                "tailscale_discovery": False,
                "outbound_replication": False,
                "client_enrollment_claim": True,
                "ai_reader_passive_read": True,
            },
        )
        self.assertEqual(self.server.store.journal_mode(), "wal")
        status, body = self.request("POST", "/push", {})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_minimum_schema_and_hashed_token_storage_are_created(self):
        connection = sqlite3.connect(self.database_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            token_rows = connection.execute(
                "SELECT token_hash, device_id FROM device_tokens ORDER BY device_id"
            ).fetchall()
        finally:
            connection.close()
        self.assertTrue({"devices", "events", "batches", "device_tokens"} <= tables)
        self.assertEqual(
            {row[1] for row in token_rows},
            {"desktop-install-a", "android-install-b"},
        )
        self.assertNotIn(DESKTOP_TOKEN, {row[0] for row in token_rows})
        self.assertTrue(all(len(row[0]) == 64 for row in token_rows))

    def test_token_is_required_and_bound_to_one_device(self):
        payload = self.batch("desktop-install-a", [self.aw_event()])

        status, body = self.upload(payload, WRONG_TOKEN)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "invalid_token")

        other_device = copy.deepcopy(payload)
        other_device["device"]["device_id"] = "android-install-b"
        other_device["device"]["platform"] = "android"
        status, body = self.upload(other_device, DESKTOP_TOKEN)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "device_token_mismatch")
        self.assertEqual(self.server.store.count_events(), 0)

    def test_device_platform_is_immutable_and_conflict_writes_no_events(self):
        first_event = self.aw_event()
        first_batch = self.batch("desktop-install-a", [first_event])
        self.assertEqual(self.upload(first_batch, DESKTOP_TOKEN)[0], 200)

        conflicting_event = self.location_observation()
        conflicting_batch = self.batch("desktop-install-a", [conflicting_event])
        conflicting_batch["device"]["platform"] = "android"
        status, body = self.upload(conflicting_batch, DESKTOP_TOKEN)

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "device_identity_conflict")
        self.assertEqual(self.server.store.count_events(), 1)
        self.assertIsNone(self.server.store.fetch_event(conflicting_event["event_id"]))

    def test_same_batch_same_body_returns_original_ack_and_changed_body_conflicts(self):
        payload = self.batch("desktop-install-a", [self.aw_event()])
        status, first_ack = self.upload(payload, DESKTOP_TOKEN)
        self.assertEqual(status, 200)

        status, retry_ack = self.upload(copy.deepcopy(payload), DESKTOP_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(retry_ack, first_ack)

        changed = copy.deepcopy(payload)
        changed["sent_at"] = "2026-07-30T14:20:11Z"
        status, conflict = self.upload(changed, DESKTOP_TOKEN)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "idempotency_conflict")
        self.assertEqual(self.server.store.count_events(), 1)

    def test_concurrent_retry_commits_once_and_returns_one_durable_ack(self):
        payload = self.batch("desktop-install-a", [self.aw_event()])
        gate = threading.Barrier(3)
        results = []

        def upload_from_thread():
            gate.wait()
            results.append(self.upload(copy.deepcopy(payload), DESKTOP_TOKEN))

        workers = [threading.Thread(target=upload_from_thread) for _ in range(2)]
        for worker in workers:
            worker.start()
        gate.wait()
        for worker in workers:
            worker.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.server.store.count_events(), 1)

    def test_stored_and_duplicate_are_confirmed_per_event(self):
        event = self.aw_event()
        first = self.batch("desktop-install-a", [event])
        status, stored = self.upload(first, DESKTOP_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(stored["accepted_event_ids"], [event["event_id"]])
        self.assertEqual(stored["confirmed_event_ids"], [event["event_id"]])
        self.assertEqual(stored["duplicate_event_ids"], [])
        self.assertEqual(
            stored["event_results"],
            [{"event_id": event["event_id"], "status": "stored"}],
        )

        second = self.batch("desktop-install-a", [copy.deepcopy(event)])
        status, duplicate = self.upload(second, DESKTOP_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["accepted_event_ids"], [])
        self.assertEqual(duplicate["confirmed_event_ids"], [event["event_id"]])
        self.assertEqual(duplicate["duplicate_event_ids"], [event["event_id"]])
        self.assertEqual(
            duplicate["event_results"],
            [{"event_id": event["event_id"], "status": "duplicate"}],
        )
        self.assertEqual(self.server.store.count_events(), 1)

    def test_activitywatch_updates_only_with_higher_revision(self):
        event_id = str(uuid.uuid4())
        initial = self.batch(
            "desktop-install-a",
            [self.aw_event(event_id, revision=1, duration=10)],
        )
        self.assertEqual(self.upload(initial, DESKTOP_TOKEN)[0], 200)

        updated_event = self.aw_event(event_id, revision=2, duration=25)
        updated = self.batch("desktop-install-a", [updated_event])
        status, acknowledgement = self.upload(updated, DESKTOP_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(
            acknowledgement["event_results"],
            [{"event_id": event_id, "status": "updated"}],
        )
        self.assertEqual(acknowledgement["accepted_event_ids"], [event_id])
        self.assertEqual(acknowledgement["confirmed_event_ids"], [event_id])
        stored = self.server.store.fetch_event(event_id)
        self.assertEqual(stored["revision"], 2)
        self.assertEqual(stored["duration_seconds"], 25)

        stale = self.batch(
            "desktop-install-a",
            [self.aw_event(event_id, revision=1, duration=30)],
        )
        status, acknowledgement = self.upload(stale, DESKTOP_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["confirmed_event_ids"], [])
        self.assertEqual(acknowledgement["accepted_event_ids"], [])
        self.assertEqual(acknowledgement["event_results"][0]["status"], "rejected")
        self.assertEqual(acknowledgement["event_results"][0]["code"], "stale_revision")

    def test_non_monotonic_activitywatch_update_is_rejected(self):
        event_id = str(uuid.uuid4())
        initial = self.batch(
            "desktop-install-a",
            [self.aw_event(event_id, revision=1, duration=30)],
        )
        self.upload(initial, DESKTOP_TOKEN)
        candidate = self.batch(
            "desktop-install-a",
            [self.aw_event(event_id, revision=2, duration=20)],
        )
        status, acknowledgement = self.upload(candidate, DESKTOP_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["confirmed_event_ids"], [])
        self.assertEqual(
            acknowledgement["event_results"][0]["code"],
            "non_monotonic_update",
        )
        self.assertEqual(self.server.store.fetch_event(event_id)["duration_seconds"], 30)

    def test_activitywatch_update_cannot_remove_existing_duration(self):
        event_id = str(uuid.uuid4())
        initial = self.batch(
            "desktop-install-a",
            [self.aw_event(event_id, revision=1, duration=30)],
        )
        self.assertEqual(self.upload(initial, DESKTOP_TOKEN)[0], 200)

        candidate_event = self.aw_event(event_id, revision=2, duration=30)
        candidate_event.pop("duration_seconds")
        candidate = self.batch("desktop-install-a", [candidate_event])
        status, acknowledgement = self.upload(candidate, DESKTOP_TOKEN)

        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["confirmed_event_ids"], [])
        self.assertEqual(
            acknowledgement["event_results"][0]["code"],
            "non_monotonic_update",
        )
        stored = self.server.store.fetch_event(event_id)
        self.assertEqual(stored["revision"], 1)
        self.assertEqual(stored["duration_seconds"], 30)

    def test_activitywatch_revision_cannot_rewrite_event_identity(self):
        event_id = str(uuid.uuid4())
        initial_event = self.aw_event(event_id, revision=1, duration=30)
        initial = self.batch("desktop-install-a", [initial_event])
        self.assertEqual(self.upload(initial, DESKTOP_TOKEN)[0], 200)

        mutations = (
            lambda event: event["payload"]["activitywatch"].update(bucket_id="other-bucket"),
            lambda event: event["payload"]["activitywatch"].update(event_id=999),
            lambda event: event["payload"]["activitywatch"].update(kind="afk"),
            lambda event: event["payload"]["app"].update(package_name="other.exe"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                candidate_event = self.aw_event(event_id, revision=2, duration=40)
                mutate(candidate_event)
                candidate = self.batch("desktop-install-a", [candidate_event])
                status, acknowledgement = self.upload(candidate, DESKTOP_TOKEN)
                self.assertEqual(status, 200)
                self.assertEqual(acknowledgement["confirmed_event_ids"], [])
                self.assertEqual(
                    acknowledgement["event_results"][0]["code"],
                    "event_conflict",
                )
        stored = self.server.store.fetch_event(event_id)
        self.assertEqual(stored["revision"], 1)
        self.assertEqual(stored["duration_seconds"], 30)

    def test_location_observation_is_immutable_even_at_higher_revision(self):
        event_id = str(uuid.uuid4())
        first = self.batch(
            "android-install-b",
            [self.location_observation(event_id, revision=0, latitude=31.23)],
        )
        self.assertEqual(self.upload(first, PHONE_TOKEN)[0], 200)

        changed = self.batch(
            "android-install-b",
            [self.location_observation(event_id, revision=1, latitude=31.24)],
        )
        status, acknowledgement = self.upload(changed, PHONE_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["confirmed_event_ids"], [])
        self.assertEqual(acknowledgement["event_results"][0]["status"], "rejected")
        self.assertEqual(acknowledgement["event_results"][0]["code"], "event_conflict")
        self.assertEqual(self.server.store.fetch_event(event_id)["payload"]["latitude"], 31.23)

    def test_android_usage_and_active_location_segment_update_then_finalize(self):
        app_event = self.aw_event(revision=0, duration=300)
        app_event["source"] = {
            "kind": "android",
            "collector": "usage_stats",
            "reliability": "observed",
        }
        app_event["payload"].pop("activitywatch")
        app_event["payload"]["app"]["package_name"] = "com.example.reader"
        segment_id = str(uuid.uuid4())
        active = self.location_segment(
            "location.sample", segment_id, revision=1, duration=300
        )
        active["payload"].update(
            is_active=True,
            observed_until="2026-07-30T14:15:00Z",
        )

        status, first_ack = self.upload(
            self.batch("android-install-b", [app_event, active]), PHONE_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(first_ack["confirmed_event_ids"]),
            {app_event["event_id"], segment_id},
        )

        extended = self.location_segment(
            "location.sample", segment_id, revision=2, duration=600
        )
        extended["payload"].update(
            is_active=True,
            observed_until="2026-07-30T14:20:00Z",
        )
        status, update_ack = self.upload(
            self.batch("android-install-b", [extended]), PHONE_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(update_ack["event_results"][0]["status"], "updated")

        finalized = self.location_segment(
            "location.stay", segment_id, revision=3, duration=900
        )
        finalized["payload"].update(
            is_active=False,
            observed_until="2026-07-30T14:25:00Z",
        )
        status, final_ack = self.upload(
            self.batch("android-install-b", [finalized]), PHONE_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(final_ack["event_results"][0]["status"], "updated")

        stored = self.server.store.fetch_event(segment_id)
        self.assertEqual(stored["device_id"], "android-install-b")
        self.assertEqual(stored["event_type"], "location.stay")
        self.assertEqual(stored["revision"], 3)
        self.assertEqual(stored["duration_seconds"], 900)
        self.assertFalse(stored["payload"]["is_active"])

    def test_location_stay_cannot_be_downgraded_to_sample(self):
        event_id = str(uuid.uuid4())
        initial = self.batch(
            "android-install-b",
            [self.location_segment("location.stay", event_id, revision=1)],
        )
        self.assertEqual(self.upload(initial, PHONE_TOKEN)[0], 200)

        downgrade = self.batch(
            "android-install-b",
            [
                self.location_segment(
                    "location.sample",
                    event_id,
                    revision=2,
                    duration=1200,
                )
            ],
        )
        status, acknowledgement = self.upload(downgrade, PHONE_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["confirmed_event_ids"], [])
        self.assertEqual(acknowledgement["event_results"][0]["code"], "event_conflict")
        self.assertEqual(self.server.store.fetch_event(event_id)["event_type"], "location.stay")

    def test_event_id_cannot_move_between_devices(self):
        event_id = str(uuid.uuid4())
        desktop_event = self.aw_event(event_id, revision=1)
        desktop_batch = self.batch("desktop-install-a", [desktop_event])
        self.assertEqual(self.upload(desktop_batch, DESKTOP_TOKEN)[0], 200)

        phone_event = self.location_observation(event_id)
        phone_batch = self.batch("android-install-b", [phone_event])
        status, acknowledgement = self.upload(phone_batch, PHONE_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["confirmed_event_ids"], [])
        self.assertEqual(acknowledgement["event_results"][0]["status"], "rejected")
        self.assertEqual(
            acknowledgement["event_results"][0]["code"],
            "event_id_conflict",
        )
        self.assertEqual(self.server.store.fetch_event(event_id)["device_id"], "desktop-install-a")

    def test_invalid_event_is_rejected_without_blocking_valid_event(self):
        valid = self.aw_event()
        invalid = copy.deepcopy(self.aw_event())
        invalid["duration_seconds"] = -1
        payload = self.batch("desktop-install-a", [invalid, valid])
        status, acknowledgement = self.upload(payload, DESKTOP_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["confirmed_event_ids"], [valid["event_id"]])
        self.assertEqual(acknowledgement["accepted_event_ids"], [valid["event_id"]])
        self.assertEqual(acknowledgement["event_results"][0]["status"], "rejected")
        self.assertEqual(acknowledgement["event_results"][0]["code"], "invalid_duration")
        self.assertEqual(acknowledgement["event_results"][1]["status"], "stored")
        self.assertEqual(self.server.store.count_events(), 1)

    def test_missing_or_invalid_event_id_rejects_the_whole_batch(self):
        invalid_events = [
            {"event_type": "manual.note"},
            {"event_id": "not-a-uuid", "event_type": "manual.note"},
        ]
        for invalid_event in invalid_events:
            with self.subTest(event_id=invalid_event.get("event_id")):
                payload = self.batch("desktop-install-a", [invalid_event])
                status, body = self.upload(payload, DESKTOP_TOKEN)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid_batch")
        self.assertEqual(self.server.store.count_events(), 0)


class CentralOperationsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_weak_tokens_are_rejected_and_tests_can_explicitly_allow_empty(self):
        with self.assertRaisesRegex(ValueError, "weak device token"):
            CentralConfig(
                database_path=self.root / "weak.sqlite3",
                token_bindings={"a" * 32: "desktop-install-a"},
            )

        empty = CentralConfig(
            database_path=self.root / "empty.sqlite3",
            token_bindings={},
        )
        with self.assertRaises(MissingDeviceTokenError):
            create_server(empty, ("127.0.0.1", 0))

        allowed = CentralConfig(
            database_path=self.root / "allowed-empty.sqlite3",
            token_bindings={},
            allow_empty_tokens=True,
        )
        server = create_server(allowed, ("127.0.0.1", 0))
        server.server_close()

    def test_init_writes_external_secret_without_printing_it_and_diagnose_is_safe(self):
        config_path = self.root / "external" / "central.json"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = central_server.main(
                [
                    "init",
                    "--config",
                    str(config_path),
                    "--device-id",
                    "desktop-install-a",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        generated_token = next(iter(payload["token_bindings"]))
        generated_read_token = payload["read_token"]
        self.assertGreaterEqual(len(generated_token), 32)
        self.assertGreaterEqual(len(set(generated_token)), 8)
        self.assertGreaterEqual(len(generated_read_token), 32)
        self.assertGreaterEqual(len(set(generated_read_token)), 8)
        self.assertNotEqual(generated_read_token, generated_token)
        self.assertNotIn(generated_token, stdout.getvalue())
        self.assertNotIn(generated_read_token, stdout.getvalue())
        self.assertNotIn(str(Path(__file__).resolve().parents[1]), str(config_path))

        second_stdout = io.StringIO()
        with redirect_stdout(second_stdout), redirect_stderr(io.StringIO()):
            result = central_server.main(
                [
                    "init",
                    "--config",
                    str(config_path),
                    "--device-id",
                    "android-install-b",
                ]
            )
        self.assertEqual(result, 0)
        updated_payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_payload["read_token"], generated_read_token)
        self.assertEqual(
            set(updated_payload["token_bindings"].values()),
            {"desktop-install-a", "android-install-b"},
        )
        self.assertNotIn(generated_read_token, updated_payload["token_bindings"])
        self.assertNotIn(generated_read_token, second_stdout.getvalue())
        self.assertIn("preserved", second_stdout.getvalue())

        config = CentralConfig.from_environment(
            {"LIFE_RADIO_CENTRAL_CONFIG": str(config_path)}
        )
        server = create_server(config, ("127.0.0.1", 0))
        server.server_close()

        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            result = central_server.main(
                ["diagnose", "--config", str(config_path)]
            )
        self.assertEqual(result, 0)
        diagnostic_output = stdout.getvalue()
        report = json.loads(diagnostic_output)
        self.assertEqual(report["journal_mode"], "wal")
        self.assertEqual(report["devices"], 0)
        self.assertEqual(report["events"], 0)
        self.assertEqual(report["batches"], 0)
        self.assertNotIn(generated_token, diagnostic_output)
        self.assertNotIn("payload", diagnostic_output.casefold())

    def test_run_fails_with_initialization_hint_when_config_has_no_tokens(self):
        config_path = self.root / "empty-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "database_path": str(self.root / "empty.sqlite3"),
                    "token_bindings": {},
                }
            ),
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            result = central_server.main(["run", "--config", str(config_path)])
        self.assertEqual(result, 2)
        self.assertIn("central_server.py init --device-id", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
