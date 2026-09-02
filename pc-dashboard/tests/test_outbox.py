import copy
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from outbox import Outbox


DEVICE = {
    "device_id": "desktop-11111111-1111-4111-8111-111111111111",
    "platform": "desktop",
    "display_name": "Test PC",
}


def event(duration: int, event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "occurred_at": "2026-07-30T10:00:00Z",
        "event_type": "app.foreground",
        "source": {
            "kind": "desktop",
            "collector": "activitywatch",
            "reliability": "observed",
        },
        "duration_seconds": duration,
        "payload": {
            "app": {
                "package_name": "chrome.exe",
                "display_name": "chrome.exe",
            },
        },
        "_received_at": "2026-07-30T10:01:00Z",
    }


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "outbox.sqlite3"
        self.now = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_same_event_revision_is_not_queued_again_after_ack(self):
        item = event(10)
        with Outbox(self.database_path) as outbox:
            first = outbox.upsert_event(item, now=self.now)
            batch = outbox.prepare_batch(DEVICE, now=self.now)
            outbox.acknowledge(
                batch["batch_id"],
                {
                    "batch_id": batch["batch_id"],
                    "confirmed_event_ids": [item["event_id"]],
                },
                now=self.now,
            )
            second = outbox.upsert_event(
                {**item, "_received_at": "2026-07-30T10:05:00Z"},
                now=self.now + timedelta(minutes=5),
            )

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(outbox.event_status(item["event_id"])["state"], "acked")
            self.assertIsNone(
                outbox.prepare_batch(DEVICE, now=self.now + timedelta(minutes=5))
            )

    def test_all_events_acked_requires_every_current_event(self):
        first = event(10)
        second = event(20)
        with Outbox(self.database_path) as outbox:
            outbox.upsert_events([first, second], now=self.now)
            batch = outbox.prepare_batch(DEVICE, now=self.now)
            outbox.acknowledge(
                batch["batch_id"],
                {"confirmed_event_ids": [first["event_id"]]},
                now=self.now,
            )
            self.assertTrue(outbox.all_events_acked([first["event_id"]]))
            self.assertFalse(outbox.all_events_acked([first["event_id"], second["event_id"]]))
            self.assertFalse(outbox.all_events_acked([]))

    def test_compaction_keeps_revision_memory_without_requeueing_same_event(self):
        item = {**event(10), "revision": 3}
        with Outbox(self.database_path) as outbox:
            outbox.upsert_event(item, now=self.now)
            batch = outbox.prepare_batch(DEVICE, now=self.now)
            outbox.acknowledge(
                batch["batch_id"],
                {"confirmed_event_ids": [item["event_id"]]},
                now=self.now,
            )

            result = outbox.compact_confirmed(
                event_types={"app.foreground"},
                completed_before=self.now + timedelta(days=1),
                vacuum=True,
            )

            self.assertEqual(result["events_compacted"], 1)
            self.assertEqual(result["batches_removed"], 1)
            self.assertIsNone(outbox.event_status(item["event_id"]))
            self.assertEqual(outbox.event_version(item["event_id"])["revision"], 3)
            self.assertTrue(outbox.all_events_acked([item["event_id"]]))
            replay = outbox.upsert_event(item, now=self.now + timedelta(days=1))
            self.assertFalse(replay["changed"])
            self.assertEqual(replay["state"], "acked")
            self.assertIsNone(outbox.prepare_batch(DEVICE, now=self.now + timedelta(days=1)))

            updated = {**item, "duration_seconds": 20, "revision": 4}
            self.assertTrue(outbox.upsert_event(updated)["changed"])
            self.assertFalse(outbox.all_events_acked([item["event_id"]]))

    def test_updated_aw_event_becomes_pending_after_old_revision_ack(self):
        original = event(10)
        updated = copy.deepcopy(original)
        updated["duration_seconds"] = 25
        with Outbox(self.database_path) as outbox:
            outbox.upsert_event(original, now=self.now)
            first_batch = outbox.prepare_batch(DEVICE, now=self.now)
            outbox.acknowledge(
                first_batch["batch_id"],
                {"confirmed_event_ids": [original["event_id"]]},
                now=self.now,
            )

            change = outbox.upsert_event(
                updated, now=self.now + timedelta(minutes=1),
            )
            second_batch = outbox.prepare_batch(
                DEVICE, now=self.now + timedelta(minutes=1),
            )

            self.assertTrue(change["changed"])
            self.assertNotEqual(second_batch["batch_id"], first_batch["batch_id"])
            self.assertEqual(
                second_batch["payload"]["events"][0]["duration_seconds"], 25,
            )

    def test_update_while_batch_is_inflight_survives_old_ack(self):
        original = event(10)
        updated = copy.deepcopy(original)
        updated["duration_seconds"] = 30
        with Outbox(self.database_path) as outbox:
            outbox.upsert_event(original, now=self.now)
            batch = outbox.prepare_batch(DEVICE, now=self.now)
            outbox.upsert_event(
                updated, now=self.now + timedelta(minutes=1),
            )
            outbox.acknowledge(
                batch["batch_id"],
                {"confirmed_event_ids": [original["event_id"]]},
                now=self.now + timedelta(minutes=2),
            )

            self.assertEqual(
                outbox.event_status(original["event_id"])["state"], "pending",
            )
            next_batch = outbox.prepare_batch(
                DEVICE, now=self.now + timedelta(minutes=2),
            )
            self.assertEqual(
                next_batch["payload"]["events"][0]["duration_seconds"], 30,
            )

    def test_lost_ack_retries_same_persisted_batch_after_restart(self):
        item = event(10)
        outbox = Outbox(self.database_path)
        outbox.upsert_event(item, now=self.now)
        first_batch = outbox.prepare_batch(DEVICE, now=self.now)
        outbox.record_attempt(first_batch["batch_id"], now=self.now)
        outbox.close()

        with Outbox(self.database_path) as restarted:
            retried_batch = restarted.prepare_batch(
                DEVICE, now=self.now + timedelta(minutes=1),
            )

            self.assertEqual(
                retried_batch["batch_id"], first_batch["batch_id"],
            )
            self.assertEqual(
                retried_batch["payload"], first_batch["payload"],
            )
            self.assertEqual(retried_batch["attempt_count"], 1)

    def test_retry_batch_is_hidden_until_exact_retry_time(self):
        item = event(10)
        retry_at = self.now + timedelta(seconds=1)
        with Outbox(self.database_path) as outbox:
            outbox.upsert_event(item, now=self.now)
            batch = outbox.prepare_batch(DEVICE, now=self.now)
            outbox.record_attempt(
                batch["batch_id"],
                error="temporary network failure",
                retry_at=retry_at,
                now=self.now,
            )

            self.assertIsNone(
                outbox.prepare_batch(
                    DEVICE,
                    now=retry_at - timedelta(microseconds=1),
                )
            )
            due = outbox.prepare_batch(DEVICE, now=retry_at)
            self.assertEqual(due["batch_id"], batch["batch_id"])
            self.assertEqual(
                due["next_attempt_at"], "2026-07-30T10:00:01.000000Z",
            )

    def test_partial_ack_only_confirms_explicit_events(self):
        accepted = event(10)
        duplicate = event(20)
        rejected = event(30)
        missing = event(40)
        with Outbox(self.database_path) as outbox:
            outbox.upsert_events(
                [accepted, duplicate, rejected, missing], now=self.now,
            )
            batch = outbox.prepare_batch(DEVICE, now=self.now)
            summary = outbox.acknowledge(
                batch["batch_id"],
                {
                    "accepted_event_ids": [accepted["event_id"]],
                    "duplicate_event_ids": [duplicate["event_id"]],
                    "rejected_events": [{
                        "event_id": rejected["event_id"],
                        "code": "invalid_payload",
                    }],
                },
                now=self.now,
            )

            self.assertEqual(
                summary, {"confirmed": 2, "rejected": 1, "unconfirmed": 1},
            )
            self.assertEqual(
                outbox.event_status(accepted["event_id"])["state"], "acked",
            )
            self.assertEqual(
                outbox.event_status(duplicate["event_id"])["state"], "acked",
            )
            self.assertEqual(
                outbox.event_status(rejected["event_id"])["state"], "rejected",
            )
            self.assertEqual(
                outbox.event_status(missing["event_id"])["state"], "pending",
            )
            retry = outbox.prepare_batch(
                DEVICE, now=self.now + timedelta(minutes=1),
            )
            self.assertEqual(
                [item["event_id"] for item in retry["payload"]["events"]],
                [missing["event_id"]],
            )

    def test_confirmed_event_ids_take_precedence_over_legacy_ack_fields(self):
        confirmed = event(10)
        legacy_only = event(20)
        with Outbox(self.database_path) as outbox:
            outbox.upsert_events([confirmed, legacy_only], now=self.now)
            batch = outbox.prepare_batch(DEVICE, now=self.now)
            outbox.acknowledge(
                batch["batch_id"],
                {
                    "confirmed_event_ids": [confirmed["event_id"]],
                    "accepted_event_ids": [legacy_only["event_id"]],
                },
                now=self.now,
            )

            self.assertEqual(
                outbox.event_status(confirmed["event_id"])["state"], "acked",
            )
            self.assertEqual(
                outbox.event_status(legacy_only["event_id"])["state"], "pending",
            )


if __name__ == "__main__":
    unittest.main()
