"""Persistent event outbox for uploading one desktop to a central server."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MAX_EVENTS_PER_BATCH = 500
EVENT_STATES = ("pending", "acked", "rejected")
BATCH_STATES = ("inflight", "retry", "completed")


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return (
        current.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _uploadable_event(event: dict[str, Any]) -> dict[str, Any]:
    required = ("event_id", "occurred_at", "event_type", "source", "payload")
    missing = [key for key in required if key not in event]
    if missing:
        raise ValueError(f"event is missing required fields: {', '.join(missing)}")
    try:
        uuid.UUID(str(event["event_id"]))
    except (ValueError, AttributeError) as error:
        raise ValueError("event_id must be a UUID") from error
    if not isinstance(event.get("source"), dict) or not isinstance(event.get("payload"), dict):
        raise ValueError("event source and payload must be objects")
    # Local storage metadata is never part of the wire event or its revision.
    return {
        key: value for key, value in event.items()
        if isinstance(key, str) and not key.startswith("_")
    }


def event_revision_hash(event: dict[str, Any]) -> str:
    uploadable = _uploadable_event(event)
    return hashlib.sha256(_canonical_json(uploadable).encode("utf-8")).hexdigest()


def event_content_hash(event: dict[str, Any]) -> str:
    """Hash source-neutral event content without its delivery revision."""
    uploadable = _uploadable_event(event)
    uploadable.pop("revision", None)
    return hashlib.sha256(_canonical_json(uploadable).encode("utf-8")).hexdigest()


def _string_ids(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


class Outbox:
    """SQLite-backed queue with immutable, retryable upload batches."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=30, check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS outbox_events (
                    event_id TEXT PRIMARY KEY,
                    revision_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    acked_revision_hash TEXT,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending', 'acked', 'rejected')
                    ),
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox_batches (
                    batch_id TEXT PRIMARY KEY,
                    device_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('inflight', 'retry', 'completed')
                    ),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS outbox_batch_events (
                    batch_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    revision_hash TEXT NOT NULL,
                    PRIMARY KEY (batch_id, event_id),
                    FOREIGN KEY (batch_id)
                        REFERENCES outbox_batches(batch_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_events_state
                    ON outbox_events(state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_outbox_batches_state
                    ON outbox_batches(state, created_at);

                CREATE TABLE IF NOT EXISTS outbox_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox_event_fingerprints (
                    event_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Outbox":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def upsert_event(
        self, event: dict[str, Any], *, now: datetime | None = None,
    ) -> dict[str, Any]:
        uploadable = _uploadable_event(event)
        event_id = str(uploadable["event_id"])
        event_json = _canonical_json(uploadable)
        revision = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
        content_hash = event_content_hash(uploadable)
        event_revision = int(uploadable.get("revision", 0) or 0)
        updated_at = utc_timestamp(now)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT revision_hash, state FROM outbox_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None and existing["revision_hash"] == revision:
                return {
                    "event_id": event_id,
                    "revision_hash": revision,
                    "changed": False,
                    "state": existing["state"],
                }
            fingerprint = self._connection.execute(
                "SELECT revision, content_hash FROM outbox_event_fingerprints WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if (
                existing is None
                and fingerprint is not None
                and int(fingerprint["revision"]) == event_revision
                and str(fingerprint["content_hash"]) == content_hash
            ):
                return {
                    "event_id": event_id,
                    "revision_hash": revision,
                    "changed": False,
                    "state": "acked",
                }
            self._connection.execute(
                """
                INSERT INTO outbox_events (
                    event_id, revision_hash, event_json, acked_revision_hash,
                    state, last_error, updated_at
                ) VALUES (?, ?, ?, NULL, 'pending', NULL, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    revision_hash = excluded.revision_hash,
                    event_json = excluded.event_json,
                    state = 'pending',
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (event_id, revision, event_json, updated_at),
            )
            return {
                "event_id": event_id,
                "revision_hash": revision,
                "changed": True,
                "state": "pending",
            }

    def upsert_events(
        self, events: Iterable[dict[str, Any]], *, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return [self.upsert_event(event, now=now) for event in events]

    @staticmethod
    def _validate_device(device: dict[str, Any]) -> dict[str, str]:
        if not isinstance(device, dict):
            raise ValueError("device must be an object")
        device_id = device.get("device_id")
        platform = device.get("platform")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device.device_id is required")
        if platform not in {"desktop", "android", "web"}:
            raise ValueError("device.platform is invalid")
        return {
            "device_id": device_id,
            "platform": str(platform),
            "display_name": str(device.get("display_name") or device_id),
        }

    @staticmethod
    def _batch_result(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_id": row["batch_id"],
            "payload": json.loads(row["payload_json"]),
            "state": row["state"],
            "attempt_count": row["attempt_count"],
            "next_attempt_at": row["next_attempt_at"],
            "last_error": row["last_error"],
        }

    def prepare_batch(
        self,
        device: dict[str, Any],
        *,
        max_events: int = MAX_EVENTS_PER_BATCH,
        now: datetime | None = None,
        force_retry: bool = False,
    ) -> dict[str, Any] | None:
        clean_device = self._validate_device(device)
        limit = min(MAX_EVENTS_PER_BATCH, int(max_events))
        if limit < 1:
            raise ValueError("max_events must be at least 1")
        current_at = utc_timestamp(now)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """
                    SELECT * FROM outbox_batches
                    WHERE state IN ('inflight', 'retry')
                    ORDER BY created_at, batch_id
                    LIMIT 1
                    """
                ).fetchone()
                if existing is not None:
                    if (
                        existing["state"] == "retry"
                        and existing["next_attempt_at"]
                        and existing["next_attempt_at"] > current_at
                        and not force_retry
                    ):
                        self._connection.commit()
                        return None
                    result = self._batch_result(existing)
                    self._connection.commit()
                    return result

                rows = self._connection.execute(
                    """
                    SELECT event_id, revision_hash, event_json
                    FROM outbox_events
                    WHERE state = 'pending'
                    ORDER BY updated_at, event_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                if not rows:
                    self._connection.commit()
                    return None

                batch_id = str(uuid.uuid4())
                payload = {
                    "schema_version": "v1",
                    "batch_id": batch_id,
                    "device": clean_device,
                    "sent_at": current_at,
                    "events": [json.loads(row["event_json"]) for row in rows],
                }
                payload_json = _canonical_json(payload)
                self._connection.execute(
                    """
                    INSERT INTO outbox_batches (
                        batch_id, device_json, payload_json, state, created_at
                    ) VALUES (?, ?, ?, 'inflight', ?)
                    """,
                    (
                        batch_id, _canonical_json(clean_device), payload_json,
                        current_at,
                    ),
                )
                self._connection.executemany(
                    """
                    INSERT INTO outbox_batch_events (
                        batch_id, event_id, revision_hash
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (batch_id, row["event_id"], row["revision_hash"])
                        for row in rows
                    ],
                )
                created = self._connection.execute(
                    "SELECT * FROM outbox_batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                self._connection.commit()
                return self._batch_result(created)
            except Exception:
                self._connection.rollback()
                raise

    def record_attempt(
        self,
        batch_id: str,
        *,
        error: str | None = None,
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        attempted_at = utc_timestamp(now)
        next_attempt_at = utc_timestamp(retry_at) if retry_at is not None else None
        state = "retry" if error is not None else "inflight"
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE outbox_batches
                SET state = ?, attempt_count = attempt_count + 1,
                    last_attempt_at = ?, next_attempt_at = ?, last_error = ?
                WHERE batch_id = ? AND state IN ('inflight', 'retry')
                """,
                (
                    state, attempted_at, next_attempt_at,
                    str(error) if error is not None else None, batch_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"active batch not found: {batch_id}")

    def acknowledge(
        self,
        batch_id: str,
        acknowledgement: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        if not isinstance(acknowledgement, dict):
            raise ValueError("acknowledgement must be an object")
        acknowledged_batch_id = acknowledgement.get("batch_id")
        if acknowledged_batch_id is not None and acknowledged_batch_id != batch_id:
            raise ValueError("acknowledgement batch_id does not match")

        if "confirmed_event_ids" in acknowledgement:
            confirmed = _string_ids(acknowledgement.get("confirmed_event_ids"))
        else:
            confirmed = (
                _string_ids(acknowledgement.get("accepted_event_ids"))
                | _string_ids(acknowledgement.get("duplicate_event_ids"))
            )

        rejected: dict[str, str] = {}
        rejected_items = acknowledgement.get("rejected_events")
        if isinstance(rejected_items, list):
            for item in rejected_items:
                if not isinstance(item, dict) or not isinstance(item.get("event_id"), str):
                    continue
                detail = str(item.get("code") or "rejected")
                if item.get("message"):
                    detail = f"{detail}: {item['message']}"
                rejected[item["event_id"]] = detail

        completed_at = utc_timestamp(now)
        summary = {"confirmed": 0, "rejected": 0, "unconfirmed": 0}
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                batch = self._connection.execute(
                    """
                    SELECT state FROM outbox_batches
                    WHERE batch_id = ?
                    """,
                    (batch_id,),
                ).fetchone()
                if batch is None or batch["state"] not in {"inflight", "retry"}:
                    raise KeyError(f"active batch not found: {batch_id}")
                rows = self._connection.execute(
                    """
                    SELECT batch_event.event_id, batch_event.revision_hash,
                           event.revision_hash AS current_revision
                    FROM outbox_batch_events AS batch_event
                    JOIN outbox_events AS event
                      ON event.event_id = batch_event.event_id
                    WHERE batch_event.batch_id = ?
                    """,
                    (batch_id,),
                ).fetchall()
                batch_event_ids = {row["event_id"] for row in rows}
                confirmed &= batch_event_ids
                rejected = {
                    event_id: detail for event_id, detail in rejected.items()
                    if event_id in batch_event_ids
                }

                for row in rows:
                    event_id = row["event_id"]
                    sent_revision = row["revision_hash"]
                    current_revision = row["current_revision"]
                    if event_id in confirmed:
                        state = "acked" if current_revision == sent_revision else "pending"
                        self._connection.execute(
                            """
                            UPDATE outbox_events
                            SET acked_revision_hash = ?, state = ?,
                                last_error = NULL, updated_at = ?
                            WHERE event_id = ?
                            """,
                            (sent_revision, state, completed_at, event_id),
                        )
                        summary["confirmed"] += 1
                    elif event_id in rejected:
                        state = "rejected" if current_revision == sent_revision else "pending"
                        self._connection.execute(
                            """
                            UPDATE outbox_events
                            SET state = ?, last_error = ?, updated_at = ?
                            WHERE event_id = ?
                            """,
                            (state, rejected[event_id], completed_at, event_id),
                        )
                        summary["rejected"] += 1
                    else:
                        self._connection.execute(
                            """
                            UPDATE outbox_events
                            SET state = 'pending',
                                last_error = 'event was not confirmed by server',
                                updated_at = ?
                            WHERE event_id = ?
                            """,
                            (completed_at, event_id),
                        )
                        summary["unconfirmed"] += 1

                batch_error = None
                if summary["rejected"] or summary["unconfirmed"]:
                    batch_error = (
                        f"{summary['rejected']} rejected, "
                        f"{summary['unconfirmed']} unconfirmed"
                    )
                self._connection.execute(
                    """
                    UPDATE outbox_batches
                    SET state = 'completed', completed_at = ?,
                        next_attempt_at = NULL, last_error = ?
                    WHERE batch_id = ?
                    """,
                    (completed_at, batch_error, batch_id),
                )
                self._connection.commit()
                return summary
            except Exception:
                self._connection.rollback()
                raise

    def event_status(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outbox_events WHERE event_id = ?", (event_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def event_version(self, event_id: str) -> dict[str, Any] | None:
        """Return compact revision memory even after an ACKed body is removed."""
        with self._lock:
            row = self._connection.execute(
                "SELECT event_json FROM outbox_events WHERE event_id = ?", (event_id,),
            ).fetchone()
            if row is not None:
                try:
                    document = json.loads(str(row["event_json"]))
                except (json.JSONDecodeError, TypeError):
                    return None
                return {
                    "revision": int(document.get("revision", 0) or 0),
                    "content_hash": event_content_hash(document),
                }
            fingerprint = self._connection.execute(
                "SELECT revision, content_hash FROM outbox_event_fingerprints WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return dict(fingerprint) if fingerprint is not None else None

    def all_events_acked(self, event_ids: Iterable[str]) -> bool:
        """Return true only when every requested current revision is ACKed."""
        normalized = {str(event_id) for event_id in event_ids if str(event_id)}
        if not normalized:
            return False
        with self._lock:
            acked = 0
            ordered = sorted(normalized)
            for offset in range(0, len(ordered), 500):
                chunk = ordered[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                row = self._connection.execute(
                    f"""
                    SELECT COUNT(*) FROM (
                        SELECT event_id FROM outbox_events
                        WHERE state = 'acked' AND event_id IN ({placeholders})
                        UNION
                        SELECT fingerprint.event_id
                        FROM outbox_event_fingerprints AS fingerprint
                        LEFT JOIN outbox_events AS current
                          ON current.event_id = fingerprint.event_id
                        WHERE current.event_id IS NULL
                          AND fingerprint.event_id IN ({placeholders})
                    )
                    """,
                    chunk + chunk,
                ).fetchone()
                acked += int(row[0])
        return acked == len(normalized)

    def compact_confirmed(
        self,
        *,
        event_types: set[str],
        completed_before: datetime,
        vacuum: bool = False,
    ) -> dict[str, int]:
        """Replace ACKed bodies with revision fingerprints and retire old batches."""
        if not event_types:
            return {"events_compacted": 0, "batches_removed": 0}
        compacted = 0
        removed_batches = 0
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT event_id, event_json, updated_at FROM outbox_events WHERE state = 'acked'"
                ).fetchall()
                removable: list[str] = []
                fingerprints: list[tuple[str, int, str, str]] = []
                for row in rows:
                    try:
                        document = json.loads(str(row["event_json"]))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if str(document.get("event_type") or "") not in event_types:
                        continue
                    event_id = str(row["event_id"])
                    removable.append(event_id)
                    fingerprints.append((
                        event_id,
                        int(document.get("revision", 0) or 0),
                        event_content_hash(document),
                        str(row["updated_at"]),
                    ))
                self._connection.executemany(
                    """
                    INSERT INTO outbox_event_fingerprints (
                        event_id, revision, content_hash, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        revision = excluded.revision,
                        content_hash = excluded.content_hash,
                        updated_at = excluded.updated_at
                    """,
                    fingerprints,
                )
                self._connection.executemany(
                    "DELETE FROM outbox_events WHERE event_id = ? AND state = 'acked'",
                    [(event_id,) for event_id in removable],
                )
                compacted = len(removable)
                cutoff = utc_timestamp(completed_before)
                cursor = self._connection.execute(
                    "DELETE FROM outbox_batches WHERE state = 'completed' AND completed_at < ?",
                    (cutoff,),
                )
                removed_batches = max(0, int(cursor.rowcount))
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            if vacuum and (compacted or removed_batches):
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._connection.execute("VACUUM")
        return {
            "events_compacted": compacted,
            "batches_removed": removed_batches,
        }

    def get_metadata(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM outbox_metadata WHERE key = ?", (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def list_events(self, event_types: set[str] | None = None) -> list[dict[str, Any]]:
        """Return current local event revisions for lightweight local views."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_json FROM outbox_events ORDER BY updated_at, event_id"
            ).fetchall()
        events = [json.loads(str(row["event_json"])) for row in rows]
        if event_types is None:
            return events
        return [event for event in events if str(event.get("event_type")) in event_types]

    def set_metadata(
        self, key: str, value: str, *, now: datetime | None = None,
    ) -> None:
        if not key:
            raise ValueError("metadata key must not be empty")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO outbox_metadata (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, str(value), utc_timestamp(now)),
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM outbox_events
                GROUP BY state
                """
            ).fetchall()
            active = self._connection.execute(
                """
                SELECT batch_id, state, attempt_count, next_attempt_at, last_error
                FROM outbox_batches
                WHERE state IN ('inflight', 'retry')
                ORDER BY created_at, batch_id
                LIMIT 1
                """
            ).fetchone()
        counts = {state: 0 for state in EVENT_STATES}
        counts.update({row["state"]: row["count"] for row in rows})
        return {
            **counts,
            "total": sum(counts.values()),
            "active_batch": dict(active) if active is not None else None,
        }
