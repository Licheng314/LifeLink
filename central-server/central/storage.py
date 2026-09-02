"""Transactional SQLite storage for the central context service."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .domain import (
    BatchEnvelope,
    EventRejection,
    NormalizedEvent,
    canonical_json,
    normalize_event,
    utc_timestamp,
)
from .locations import locations_view
from .read_model import devices_view, usage_view
from .health_info import build_health_info
from .ai_readers import AIReaderService


logger = logging.getLogger(__name__)


def _display_duration_text(value: str | None) -> str | None:
    """Normalize legacy large-minute phrases at read time without rewriting history."""
    if value is None:
        return None
    def replace(match: re.Match[str]) -> str:
        minutes = int(match.group(1))
        if minutes < 60:
            return match.group(0)
        hours, remainder = divmod(minutes, 60)
        return f"{hours}小时{remainder}分钟" if remainder else f"{hours}小时"
    return re.sub(r"\b(\d+)\s*分钟", replace, value)


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    display_name TEXT NOT NULL,
    custom_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_json TEXT NOT NULL,
    duration_seconds INTEGER,
    revision INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    event_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    is_mutable INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_device_occurred
    ON events(device_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type_occurred
    ON events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_occurred
    ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_end_time
    ON events((julianday(occurred_at) + COALESCE(duration_seconds, 0) / 86400.0));
CREATE INDEX IF NOT EXISTS idx_events_type_end_time
    ON events(event_type, (julianday(occurred_at) + COALESCE(duration_seconds, 0) / 86400.0));

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    request_hash TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    ack_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_device_received
    ON batches(device_id, received_at);

CREATE TABLE IF NOT EXISTS device_tokens (
    token_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS client_invitations (
    invitation_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    central_base_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claimed_device_id TEXT,
    claimed_platform TEXT,
    claimed_display_name TEXT,
    claimed_at TEXT,
    issued_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_client_invitations_expires
    ON client_invitations(expires_at);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shared_settings (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    timezone TEXT NOT NULL CHECK (timezone = 'Asia/Shanghai'),
    day_start_hour INTEGER NOT NULL CHECK (
        typeof(day_start_hour) = 'integer' AND day_start_hour BETWEEN 0 AND 23
    ),
    primary_health_device_id TEXT,
    sleep_local_time TEXT NOT NULL DEFAULT '23:00',
    ai_display_name TEXT NOT NULL DEFAULT 'AI',
    morning_report_json TEXT NOT NULL DEFAULT '{"enabled":false,"mode":"after_first_usage","delay_minutes":60,"local_time":null}',
    evening_report_json TEXT NOT NULL DEFAULT '{"enabled":false,"local_time":"23:00"}',
    periodic_summary_json TEXT NOT NULL DEFAULT '{"enabled":false,"start_local_time":"10:00","end_local_time":"22:00","interval_minutes":120}',
    settings_version INTEGER NOT NULL CHECK (settings_version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist_rules (
    rule_id TEXT PRIMARY KEY,
    rule_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    normalized_pattern TEXT NOT NULL,
    platform_scope TEXT NOT NULL DEFAULT 'pc',
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blacklist_rules_enabled
    ON blacklist_rules(enabled, rule_type);

CREATE TABLE IF NOT EXISTS wishes (
    wish_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    duration_days INTEGER NOT NULL CHECK (duration_days IN (3, 7)),
    status TEXT NOT NULL CHECK (status IN ('active', 'cancelled', 'archived')),
    created_at TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    timezone TEXT NOT NULL CHECK (timezone = 'Asia/Shanghai'),
    day_start_hour INTEGER NOT NULL CHECK (day_start_hour BETWEEN 0 AND 23),
    settings_version INTEGER NOT NULL,
    ai_tracking_enabled INTEGER NOT NULL DEFAULT 0,
    cancelled_at TEXT,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wishes_status_created ON wishes(status, created_at);

-- Deletion must not retain user-entered content or the wish's dates/results.
-- The original create request is retained only to prevent a delayed retry from
-- recreating a deleted wish.
CREATE TABLE IF NOT EXISTS deleted_wish_tombstones (
    wish_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    deleted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wish_days (
    wish_id TEXT NOT NULL REFERENCES wishes(wish_id),
    business_date TEXT NOT NULL,
    evaluation TEXT CHECK (evaluation IN ('completed', 'not_completed') OR evaluation IS NULL),
    evaluation_source TEXT CHECK (evaluation_source IN ('manual', 'automatic') OR evaluation_source IS NULL),
    evaluated_at TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wish_id, business_date)
);

CREATE TABLE IF NOT EXISTS timeline_events (
    timeline_event_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_key TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('wish', 'trigger', 'device', 'user', 'system')),
    importance TEXT NOT NULL CHECK (importance IN ('low', 'normal', 'high')),
    title TEXT NOT NULL,
    detail TEXT,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('device', 'user', 'central')),
    source_device_id TEXT,
    wish_id TEXT,
    trigger_id TEXT,
    subject_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    statistics_window_json TEXT,
    delivery_json TEXT,
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_timeline_occurred ON timeline_events(occurred_at DESC, timeline_event_id DESC);
CREATE INDEX IF NOT EXISTS idx_timeline_wish ON timeline_events(wish_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS event_triggers (
    trigger_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    wish_id TEXT REFERENCES wishes(wish_id),
    trigger_type TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_triggered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_triggers_wish ON event_triggers(wish_id, created_at);
"""


class IdempotencyConflict(RuntimeError):
    pass


class DeviceIdentityConflict(RuntimeError):
    pass


class InvitationInvalid(RuntimeError):
    pass


class InvitationExpired(RuntimeError):
    pass


class InvitationAlreadyClaimed(RuntimeError):
    pass


class WishLimitReached(RuntimeError):
    pass


class WishNotCancellable(RuntimeError):
    pass


class WishNotCompletable(RuntimeError):
    pass


class WishDaysIncomplete(RuntimeError):
    def __init__(self, missing_business_dates: list[str]) -> None:
        super().__init__("all reached wish days must be assessed before manual completion")
        self.missing_business_dates = missing_business_dates


class WishDeleted(RuntimeError):
    pass


class FutureWishDay(RuntimeError):
    pass


class WishDayNotFound(RuntimeError):
    pass


class TriggerConfigurationConflict(RuntimeError):
    pass


class CentralStore:
    def __init__(self, database_path: Path, token_bindings: Mapping[str, str]) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # Construction only ensures the supplied credentials exist; it never
        # revokes credentials it does not know about. This keeps ad-hoc, test and
        # diagnostic use of CentralStore safe against an empty or partial binding
        # map (a previous version wiped every active token when CentralStore(db,
        # {}) was opened on an existing database). The production HTTP server
        # makes the on-disk config authoritative via reconcile_credentials().
        self._provision_tokens(token_bindings, revoke_unlisted=False)
        self.ai_readers = AIReaderService(self)

    def reconcile_credentials(self, token_bindings: Mapping[str, str]) -> None:
        """Make the configured device credential set authoritative.

        Invoked by the production server at startup. It reactivates listed
        tokens whose device is not retired and revokes active tokens that are no
        longer present in ``token_bindings``. An empty binding map is rejected
        when active credentials exist, to prevent a misloaded config from
        locking out every registered device.
        """
        self._provision_tokens(token_bindings, revoke_unlisted=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            # Check if blacklist_rules already existed before running SCHEMA
            table_existed = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='blacklist_rules'"
            ).fetchone() is not None
            connection.executescript(SCHEMA)
            self._migrate_device_management(connection)
            self._migrate_shared_settings(connection)
            self._migrate_blacklist_platform_scope(connection)
            self._seed_blacklist_rules(connection, table_already_existed=table_existed)
            self._seed_shared_settings(connection)

    @staticmethod
    def _migrate_device_management(connection: sqlite3.Connection) -> None:
        """Add central aliases and logical-retirement state without rewriting facts."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(devices)")
        }
        if "custom_name" not in columns:
            connection.execute("ALTER TABLE devices ADD COLUMN custom_name TEXT")
        if "retired_at" not in columns:
            connection.execute("ALTER TABLE devices ADD COLUMN retired_at TEXT")

    @staticmethod
    def _migrate_shared_settings(connection: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(shared_settings)")}
        if "primary_health_device_id" not in columns:
            connection.execute("ALTER TABLE shared_settings ADD COLUMN primary_health_device_id TEXT")
        for name, sql in (
            ("sleep_local_time", "ALTER TABLE shared_settings ADD COLUMN sleep_local_time TEXT NOT NULL DEFAULT '23:00'"),
            ("ai_display_name", "ALTER TABLE shared_settings ADD COLUMN ai_display_name TEXT NOT NULL DEFAULT 'AI'"),
            ("morning_report_json", "ALTER TABLE shared_settings ADD COLUMN morning_report_json TEXT NOT NULL DEFAULT '{\"enabled\":false,\"mode\":\"after_first_usage\",\"delay_minutes\":60,\"local_time\":null}'"),
            ("evening_report_json", "ALTER TABLE shared_settings ADD COLUMN evening_report_json TEXT NOT NULL DEFAULT '{\"enabled\":false,\"local_time\":\"23:00\"}'"),
            ("periodic_summary_json", "ALTER TABLE shared_settings ADD COLUMN periodic_summary_json TEXT NOT NULL DEFAULT '{\"enabled\":false,\"start_local_time\":\"10:00\",\"end_local_time\":\"22:00\",\"interval_minutes\":120}'"),
        ):
            if name not in columns:
                connection.execute(sql)
        event_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(timeline_events)")}
        if "statistics_window_json" not in event_columns:
            connection.execute("ALTER TABLE timeline_events ADD COLUMN statistics_window_json TEXT")
        if "delivery_json" not in event_columns:
            connection.execute("ALTER TABLE timeline_events ADD COLUMN delivery_json TEXT")

    @staticmethod
    def _seed_shared_settings(connection: sqlite3.Connection) -> None:
        """Create the sole shared-settings row without resetting an existing value."""
        connection.execute(
            """
            INSERT OR IGNORE INTO shared_settings(
                singleton_id, timezone, day_start_hour, settings_version, updated_at
            ) VALUES (1, 'Asia/Shanghai', 0, 1, ?)
            """,
            (utc_timestamp(),),
        )

    @staticmethod
    def _migrate_blacklist_platform_scope(connection: sqlite3.Connection) -> None:
        """Idempotently upgrade blacklist_rules with platform_scope column and index."""
        try:
            connection.execute("SELECT platform_scope FROM blacklist_rules LIMIT 0")
        except sqlite3.OperationalError:
            connection.execute(
                "ALTER TABLE blacklist_rules ADD COLUMN platform_scope TEXT NOT NULL DEFAULT 'tmp'"
            )
        connection.execute(
            "UPDATE blacklist_rules SET platform_scope = 'pc'"
            " WHERE rule_type = 'app' AND platform_scope = 'tmp'"
        )
        connection.execute(
            "UPDATE blacklist_rules SET platform_scope = 'web'"
            " WHERE rule_type = 'domain' AND platform_scope = 'tmp'"
        )
        connection.execute("DROP INDEX IF EXISTS idx_blacklist_rules_type_pattern")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklist_rules_type_scope_pattern
                ON blacklist_rules(rule_type, platform_scope, normalized_pattern)
            """
        )

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _provision_tokens(self, token_bindings: Mapping[str, str], *, revoke_unlisted: bool) -> None:
        now = utc_timestamp()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if revoke_unlisted:
                    active_count = connection.execute(
                        "SELECT COUNT(*) FROM device_tokens WHERE revoked_at IS NULL"
                    ).fetchone()[0]
                    if not token_bindings and active_count:
                        raise ValueError(
                            "refusing to revoke all device credentials: token_bindings "
                            "is empty but active credentials exist; verify central config"
                        )
                    if token_bindings:
                        listed_hashes = [self.token_hash(token) for token in token_bindings]
                        placeholders = ",".join("?" for _ in listed_hashes)
                        connection.execute(
                            f"UPDATE device_tokens SET revoked_at = ? "
                            f"WHERE revoked_at IS NULL AND token_hash NOT IN ({placeholders})",
                            (now, *listed_hashes),
                        )
                for token, device_id in token_bindings.items():
                    digest = self.token_hash(token)
                    device = connection.execute(
                        "SELECT retired_at FROM devices WHERE device_id = ?",
                        (device_id,),
                    ).fetchone()
                    if device is not None and device["retired_at"] is not None:
                        # A stale external binding must never revive a retired
                        # device; revoke it explicitly in case it is still active.
                        connection.execute(
                            "UPDATE device_tokens SET revoked_at = ? "
                            "WHERE token_hash = ? AND revoked_at IS NULL",
                            (now, digest),
                        )
                        continue
                    row = connection.execute(
                        "SELECT device_id FROM device_tokens WHERE token_hash = ?",
                        (digest,),
                    ).fetchone()
                    if row is not None and row["device_id"] != device_id:
                        raise ValueError("one central device token cannot be rebound to another device_id")
                    connection.execute(
                        """
                        INSERT INTO device_tokens(token_hash, device_id, created_at, revoked_at)
                        VALUES (?, ?, ?, NULL)
                        ON CONFLICT(token_hash) DO UPDATE SET revoked_at = NULL
                        """,
                        (digest, device_id, now),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def device_for_token(self, token: str) -> str | None:
        digest = self.token_hash(token)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT device_id
                FROM device_tokens
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (digest,),
            ).fetchone()
        return str(row["device_id"]) if row is not None else None

    # ---------------------------------------------------------------
    #  Central device names and logical retirement
    # ---------------------------------------------------------------

    @staticmethod
    def _managed_device(row: sqlite3.Row) -> dict[str, Any]:
        custom_name = str(row["custom_name"]) if row["custom_name"] is not None else None
        reported_name = str(row["display_name"])
        return {
            "device_id": str(row["device_id"]),
            "platform": str(row["platform"]),
            "display_name": custom_name or reported_name,
            "reported_name": reported_name,
            "custom_name": custom_name,
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
        }

    @staticmethod
    def _sync_status(last_seen_at: str, generated_at: datetime) -> str:
        if not isinstance(last_seen_at, str) or not last_seen_at.endswith("Z"):
            return "disconnected"
        try:
            last_seen = datetime.fromisoformat(last_seen_at[:-1] + "+00:00")
        except ValueError:
            return "disconnected"
        age = (generated_at - last_seen).total_seconds()
        return "connected" if 0 <= age <= 600 else "disconnected"

    def list_managed_devices(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT device_id, platform, display_name, custom_name,
                          first_seen_at, last_seen_at
                   FROM devices
                   WHERE retired_at IS NULL
                   ORDER BY COALESCE(custom_name, display_name) COLLATE NOCASE, device_id"""
            ).fetchall()
            devices = [self._managed_device(row) for row in rows]
            generated = self._now()
            for device in devices:
                device["status"] = self._sync_status(device["last_seen_at"], generated)
        return devices

    def rename_device(self, device_id: str, custom_name: str) -> dict[str, Any] | None:
        normalized = custom_name.strip()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT retired_at FROM devices WHERE device_id = ?", (device_id,),
            ).fetchone()
            if row is None or row["retired_at"] is not None:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE devices SET custom_name = ? WHERE device_id = ?",
                (normalized, device_id),
            )
            result = connection.execute(
                """SELECT device_id, platform, display_name, custom_name,
                          first_seen_at, last_seen_at
                   FROM devices WHERE device_id = ?""",
                (device_id,),
            ).fetchone()
            connection.commit()
        return self._managed_device(result)  # type: ignore[arg-type]

    def retire_device(self, device_id: str) -> bool | None:
        """Retire a device and revoke credentials without deleting its facts."""
        now = utc_timestamp()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT retired_at FROM devices WHERE device_id = ?", (device_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["retired_at"] is not None:
                connection.commit()
                return False
            connection.execute(
                "UPDATE devices SET retired_at = ? WHERE device_id = ?", (now, device_id),
            )
            connection.execute(
                "UPDATE device_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (now, device_id),
            )
            connection.execute(
                """UPDATE event_triggers
                   SET enabled = 0, updated_at = ?
                   WHERE enabled = 1
                     AND trigger_type = 'device_usage_milestone'
                     AND json_extract(parameters_json, '$.device_id') = ?""",
                (now, device_id),
            )
            connection.execute(
                """UPDATE shared_settings
                   SET primary_health_device_id = NULL,
                       settings_version = settings_version + 1, updated_at = ?
                   WHERE singleton_id = 1 AND primary_health_device_id = ?""",
                (now, device_id),
            )
            connection.commit()
        return True

    # ---------------------------------------------------------------
    #  Shared cross-day settings
    # ---------------------------------------------------------------

    @staticmethod
    def _shared_settings_from_row(
        row: sqlite3.Row, *, ai_display_name: str | None = None
    ) -> dict[str, Any]:
        return {
            "timezone": str(row["timezone"]),
            "day_start_hour": int(row["day_start_hour"]),
            "primary_health_device_id": str(row["primary_health_device_id"]) if row["primary_health_device_id"] is not None else None,
            "sleep_local_time": str(row["sleep_local_time"]),
            "ai_display_name": ai_display_name or "AI",
            "morning_report": json.loads(row["morning_report_json"]),
            "evening_report": json.loads(row["evening_report_json"]),
            "periodic_summary": json.loads(row["periodic_summary_json"]),
            "settings_version": int(row["settings_version"]),
            "updated_at": str(row["updated_at"]),
        }

    def get_shared_settings(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM shared_settings WHERE singleton_id = 1
                """
            ).fetchone()
            reader = connection.execute(
                """
                SELECT display_name FROM ai_readers
                WHERE revoked_at IS NULL AND token_expires_at > ?
                ORDER BY paired_at DESC, reader_id DESC LIMIT 1
                """,
                (utc_timestamp(),),
            ).fetchone()
        if row is None:  # Defensive only: initialization always seeds the singleton.
            raise RuntimeError("shared settings are not initialized")
        return self._shared_settings_from_row(
            row,
            ai_display_name=str(reader["display_name"]) if reader is not None else None,
        )

    def update_shared_settings(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically update the supported shared settings."""
        if not changes or not set(changes) <= {"day_start_hour", "primary_health_device_id", "sleep_local_time", "morning_report", "evening_report", "periodic_summary"}:
            raise ValueError("unsupported shared setting")
        day_start_hour = changes.get("day_start_hour")
        if "day_start_hour" in changes and (
            isinstance(day_start_hour, bool) or not isinstance(day_start_hour, int) or not 0 <= day_start_hour <= 23
        ):
            raise ValueError("day_start_hour must be an integer from 0 to 23")
        primary = changes.get("primary_health_device_id")
        if "primary_health_device_id" in changes and primary is not None and (not isinstance(primary, str) or not primary):
            raise ValueError("primary_health_device_id must be a non-empty string or null")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM shared_settings WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise RuntimeError("shared settings are not initialized")
                if primary is not None and "primary_health_device_id" in changes:
                    device = connection.execute(
                        "SELECT platform, retired_at FROM devices WHERE device_id = ?", (primary,),
                    ).fetchone()
                    if device is None or device["retired_at"] is not None or str(device["platform"]) != "android":
                        raise ValueError("primary health device must be an active Android device")
                self._validate_event_settings(changes)
                next_hour = int(day_start_hour) if "day_start_hour" in changes else int(row["day_start_hour"])
                next_primary = primary if "primary_health_device_id" in changes else row["primary_health_device_id"]
                next_values = (next_hour, next_primary, changes.get("sleep_local_time", row["sleep_local_time"]), row["ai_display_name"], canonical_json(changes.get("morning_report", json.loads(row["morning_report_json"]))), canonical_json(changes.get("evening_report", json.loads(row["evening_report_json"]))), canonical_json(changes.get("periodic_summary", json.loads(row["periodic_summary_json"]))))
                old_values = (int(row["day_start_hour"]), row["primary_health_device_id"], row["sleep_local_time"], row["ai_display_name"], canonical_json(json.loads(row["morning_report_json"])), canonical_json(json.loads(row["evening_report_json"])), canonical_json(json.loads(row["periodic_summary_json"])))
                if old_values != next_values:
                    connection.execute(
                        """
                        UPDATE shared_settings SET day_start_hour = ?, primary_health_device_id = ?, sleep_local_time = ?, ai_display_name = ?, morning_report_json = ?, evening_report_json = ?, periodic_summary_json = ?, settings_version = settings_version + 1,
                            updated_at = ?
                        WHERE singleton_id = 1
                        """,
                        (*next_values, utc_timestamp()),
                    )
                    row = connection.execute(
                        """
                        SELECT *
                        FROM shared_settings WHERE singleton_id = 1
                        """
                    ).fetchone()
                projected_reader = connection.execute(
                    """
                    SELECT display_name FROM ai_readers
                    WHERE revoked_at IS NULL AND token_expires_at > ?
                    ORDER BY paired_at DESC, reader_id DESC LIMIT 1
                    """,
                    (utc_timestamp(),),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._shared_settings_from_row(
            row,
            ai_display_name=(
                str(projected_reader["display_name"])
                if projected_reader is not None else None
            ),
        )

    @staticmethod
    def _validate_event_settings(changes: Mapping[str, Any]) -> None:
        if "sleep_local_time" in changes and (not isinstance(changes["sleep_local_time"], str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", changes["sleep_local_time"]) is None): raise ValueError("sleep_local_time must be HH:mm")
        for name in ("morning_report", "evening_report", "periodic_summary"):
            if name in changes and not isinstance(changes[name], dict): raise ValueError(f"{name} must be an object")
        def clock(value: Any) -> bool:
            return isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is not None
        morning = changes.get("morning_report")
        if morning is not None:
            if set(morning) != {"enabled", "mode", "delay_minutes", "local_time"} or not isinstance(morning["enabled"], bool) or morning["mode"] not in {"after_first_usage", "fixed_time"} or isinstance(morning["delay_minutes"], bool) or not isinstance(morning["delay_minutes"], int) or not 1 <= morning["delay_minutes"] <= 720 or (morning["mode"] == "fixed_time" and not clock(morning["local_time"])) or (morning["mode"] == "after_first_usage" and morning["local_time"] is not None): raise ValueError("invalid morning_report")
        evening = changes.get("evening_report")
        if evening is not None and (set(evening) != {"enabled", "local_time"} or not isinstance(evening["enabled"], bool) or not clock(evening["local_time"])): raise ValueError("invalid evening_report")
        periodic = changes.get("periodic_summary")
        if periodic is not None and (set(periodic) != {"enabled", "start_local_time", "end_local_time", "interval_minutes"} or not isinstance(periodic["enabled"], bool) or not clock(periodic["start_local_time"]) or not clock(periodic["end_local_time"]) or periodic.get("interval_minutes") not in {30, 60, 120, 180, 240}): raise ValueError("invalid periodic_summary")

    def update_shared_day_start_hour(self, day_start_hour: int) -> dict[str, Any]:
        return self.update_shared_settings({"day_start_hour": day_start_hour})

    # ---------------------------------------------------------------
    # Wishes, timeline, and trigger configuration
    # ---------------------------------------------------------------

    @staticmethod
    def _now(value: datetime | None = None) -> datetime:
        now = value or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _business_date(now: datetime, hour: int, tz_name: str = "Asia/Shanghai") -> date:
        if tz_name != "Asia/Shanghai":
            raise ValueError("unsupported business timezone")
        # v1 fixes the only supported zone to UTC+08:00, avoiding a runtime tzdata dependency.
        local = now.astimezone(timezone(timedelta(hours=8))) - timedelta(hours=hour)
        return local.date()

    @staticmethod
    def _business_day_end_utc(business_date: str, hour: int, tz_name: str) -> datetime:
        if tz_name != "Asia/Shanghai":
            raise ValueError("unsupported business timezone")
        next_day = date.fromisoformat(business_date) + timedelta(days=1)
        local = datetime.combine(next_day, datetime.min.time(), timezone(timedelta(hours=8))) + timedelta(hours=hour)
        return local.astimezone(timezone.utc)

    @staticmethod
    def _wish_day(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "business_date": row["business_date"], "evaluation": row["evaluation"],
            "evaluation_source": row["evaluation_source"], "evaluated_at": row["evaluated_at"],
            "revision": int(row["revision"]),
        }

    @classmethod
    def _wish_from_connection(cls, connection: sqlite3.Connection, wish_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM wishes WHERE wish_id = ?", (wish_id,)).fetchone()
        if row is None:
            return None
        days = connection.execute("SELECT * FROM wish_days WHERE wish_id = ? ORDER BY business_date", (wish_id,)).fetchall()
        completed = sum(day["evaluation"] == "completed" for day in days)
        return {
            "wish_id": row["wish_id"], "text": row["text"], "duration_days": int(row["duration_days"]),
            "status": row["status"], "created_at": row["created_at"], "starts_on": row["starts_on"],
            "ends_on": row["ends_on"], "business_day_snapshot": {
                "timezone": row["timezone"], "day_start_hour": int(row["day_start_hour"]),
                "settings_version": int(row["settings_version"]),
            }, "ai_tracking_enabled": bool(row["ai_tracking_enabled"]), "cancelled_at": row["cancelled_at"],
            "archived_at": row["archived_at"], "completed_days": completed,
            "wish_days": [cls._wish_day(day) for day in days],
        }

    @staticmethod
    def _append_timeline(connection: sqlite3.Connection, *, occurred_at: str, event_key: str,
                         category: str, importance: str, title: str, source_kind: str,
                         source_device_id: str | None, wish_id: str | None, trigger_id: str | None,
                         subject: dict[str, Any], evidence: dict[str, Any], dedupe_key: str,
                         detail: str | None = None, statistics_window: dict[str, Any] | None = None,
                         delivery: dict[str, Any] | None = None) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO timeline_events(
                timeline_event_id, occurred_at, created_at, event_key, category, importance, title, detail,
                source_kind, source_device_id, wish_id, trigger_id, subject_json, evidence_json, statistics_window_json, delivery_json, dedupe_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), occurred_at, utc_timestamp(), event_key, category, importance, title, detail,
             source_kind, source_device_id, wish_id, trigger_id, canonical_json(subject), canonical_json(evidence), canonical_json(statistics_window) if statistics_window else None, canonical_json(delivery) if delivery else None, dedupe_key),
        )

    def _finalize_due_in_connection(self, connection: sqlite3.Connection, now: datetime) -> None:
        rows = connection.execute("SELECT * FROM wishes WHERE status = 'active'").fetchall()
        now_text = utc_timestamp(now)
        for wish in rows:
            deadline = self._business_day_end_utc(wish["ends_on"], int(wish["day_start_hour"]), wish["timezone"]) + timedelta(hours=72)
            if now < deadline:
                continue
            connection.execute(
                """UPDATE wish_days SET evaluation = 'not_completed', evaluation_source = 'automatic',
                   evaluated_at = ? WHERE wish_id = ? AND evaluation IS NULL""", (now_text, wish["wish_id"]),
            )
            self._complete_wish_in_connection(connection, wish, now_text, source_device_id=None, automatic=True)

    def _complete_wish_in_connection(
        self, connection: sqlite3.Connection, wish: sqlite3.Row, now_text: str,
        *, source_device_id: str | None, automatic: bool,
    ) -> None:
        days = connection.execute(
            "SELECT evaluation FROM wish_days WHERE wish_id = ?", (wish["wish_id"],)
        ).fetchall()
        completed = sum(day["evaluation"] == "completed" for day in days)
        duration = int(wish["duration_days"])
        if completed >= duration:
            closing = "恭喜全部完成！"
        elif completed * 2 >= duration:
            closing = "完成的还不错！"
        else:
            closing = "再接再厉吧！"
        self._append_timeline(
            connection, occurred_at=now_text, event_key="wish.period_completed",
            category="wish", importance="normal", title=f"「{wish['text']}」心愿已完成",
            detail=f"完成的天数 {completed}/{duration}，{closing}",
            source_kind="central" if automatic else "user", source_device_id=source_device_id,
            wish_id=wish["wish_id"], trigger_id=None,
            subject={"duration_days": duration},
            evidence={"completed_days": completed, "automatic_finalized": automatic},
            dedupe_key=f"wish:{wish['wish_id']}:period_completed",
        )
        connection.execute(
            "UPDATE wishes SET status = 'archived', archived_at = ? WHERE wish_id = ?",
            (now_text, wish["wish_id"]),
        )
        connection.execute(
            "UPDATE event_triggers SET enabled = 0, updated_at = ? WHERE wish_id = ? AND enabled = 1",
            (now_text, wish["wish_id"]),
        )

    def complete_wish(
        self, wish_id: str, *, source_device_id: str, now: datetime | None = None,
    ) -> dict[str, Any] | None:
        now_dt = self._now(now)
        now_text = utc_timestamp(now_dt)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._finalize_due_in_connection(connection, now_dt)
                wish = connection.execute("SELECT * FROM wishes WHERE wish_id = ?", (wish_id,)).fetchone()
                if wish is None:
                    connection.commit(); return None
                if wish["status"] == "archived":
                    result = self._wish_from_connection(connection, wish_id)
                    connection.commit(); return result
                if wish["status"] != "active":
                    raise WishNotCompletable("only an active wish can be completed")
                period_end = self._business_day_end_utc(
                    wish["ends_on"], int(wish["day_start_hour"]), wish["timezone"]
                )
                if now_dt < period_end:
                    raise WishNotCompletable("the final wish business day has not ended")
                missing = [
                    str(row["business_date"])
                    for row in connection.execute(
                        "SELECT business_date FROM wish_days WHERE wish_id = ? AND evaluation IS NULL ORDER BY business_date",
                        (wish_id,),
                    ).fetchall()
                ]
                if missing:
                    raise WishDaysIncomplete(missing)
                self._complete_wish_in_connection(
                    connection, wish, now_text, source_device_id=source_device_id, automatic=False,
                )
                result = self._wish_from_connection(connection, wish_id)
                connection.commit(); return result
            except Exception:
                connection.rollback()
                raise

    def create_wish(self, *, request_id: str, request_hash: str, text: str, duration_days: int,
                     ai_tracking_enabled: bool, source_device_id: str, now: datetime | None = None) -> dict[str, Any]:
        now_dt = self._now(now)
        now_text = utc_timestamp(now_dt)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._finalize_due_in_connection(connection, now_dt)
                prior = connection.execute("SELECT request_hash, wish_id FROM wishes WHERE request_id = ?", (request_id,)).fetchone()
                if prior is not None:
                    if prior["request_hash"] != request_hash:
                        raise IdempotencyConflict("request_id was already used with different request content")
                    result = self._wish_from_connection(connection, prior["wish_id"])
                    connection.commit(); return result  # type: ignore[return-value]
                tombstone = connection.execute(
                    "SELECT request_hash FROM deleted_wish_tombstones WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if tombstone is not None:
                    if tombstone["request_hash"] != request_hash:
                        raise IdempotencyConflict("request_id was already used with different request content")
                    raise WishDeleted("the wish created by this request_id was deleted")
                active = connection.execute("SELECT COUNT(*) FROM wishes WHERE status = 'active'").fetchone()[0]
                if active >= 3:
                    raise WishLimitReached("at most three unarchived wishes are allowed")
                settings = connection.execute("SELECT * FROM shared_settings WHERE singleton_id = 1").fetchone()
                if settings is None:
                    raise RuntimeError("shared settings are not initialized")
                creation_day = self._business_date(now_dt, int(settings["day_start_hour"]), settings["timezone"])
                starts_on = creation_day
                wish_id = str(uuid.uuid4())
                ends_on = starts_on + timedelta(days=duration_days - 1)
                connection.execute("""INSERT INTO wishes(wish_id, request_id, request_hash, text, duration_days, status,
                    created_at, starts_on, ends_on, timezone, day_start_hour, settings_version, ai_tracking_enabled)
                    VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
                    (wish_id, request_id, request_hash, text, duration_days, now_text, starts_on.isoformat(), ends_on.isoformat(),
                     settings["timezone"], settings["day_start_hour"], settings["settings_version"], int(ai_tracking_enabled)))
                for offset in range(duration_days):
                    connection.execute("INSERT INTO wish_days(wish_id, business_date, evaluation, evaluation_source, evaluated_at, revision) VALUES (?, ?, NULL, NULL, NULL, 0)",
                        (wish_id, (starts_on + timedelta(days=offset)).isoformat()))
                self._append_timeline(connection, occurred_at=now_text, event_key="wish.created", category="wish",
                    importance="normal", title=f"「{text}」心愿已创建", source_kind="user", source_device_id=source_device_id,
                    wish_id=wish_id, trigger_id=None, subject={"text": text}, evidence={"duration_days": duration_days},
                    dedupe_key=f"wish:{wish_id}:created")
                result = self._wish_from_connection(connection, wish_id)
                connection.commit(); return result  # type: ignore[return-value]
            except Exception:
                connection.rollback(); raise

    def patch_wish_text(self, wish_id: str, text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Update only the central authoritative wish text.

        Like every other wish read/write operation, this first applies any due
        lazy finalization in the same transaction. The edit itself changes no
        field other than text and does not produce a text-update timeline event.
        """
        now_dt = self._now(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._finalize_due_in_connection(connection, now_dt)
                wish = connection.execute(
                    "SELECT wish_id FROM wishes WHERE wish_id = ?", (wish_id,)
                ).fetchone()
                if wish is None:
                    connection.commit()
                    return None
                connection.execute("UPDATE wishes SET text = ? WHERE wish_id = ?", (text, wish_id))
                result = self._wish_from_connection(connection, wish_id)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def delete_wish(self, wish_id: str, *, now: datetime | None = None) -> bool | None:
        """Atomically remove a wish while retaining only an idempotency tombstone.

        True trigger history remains readable, but no longer links to the
        deleted wish or its now-removed trigger records. Device ``events`` are
        intentionally never touched.
        """
        deleted_at = utc_timestamp(self._now(now))
        lifecycle_keys = (
            "wish.created", "wish.cancelled", "wish.period_completed",
            "wish.result_revised",
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                wish = connection.execute(
                    "SELECT wish_id, request_id, request_hash FROM wishes WHERE wish_id = ?",
                    (wish_id,),
                ).fetchone()
                if wish is None:
                    tombstone = connection.execute(
                        "SELECT 1 FROM deleted_wish_tombstones WHERE wish_id = ?", (wish_id,)
                    ).fetchone()
                    connection.commit()
                    # False represents an idempotent retry; None preserves
                    # not-found semantics for an ID never seen by central.
                    return False if tombstone is not None else None
                trigger_ids = [
                    str(row["trigger_id"])
                    for row in connection.execute(
                        "SELECT trigger_id FROM event_triggers WHERE wish_id = ?", (wish_id,)
                    ).fetchall()
                ]
                placeholders = ", ".join("?" for _ in lifecycle_keys)
                connection.execute(
                    f"DELETE FROM timeline_events WHERE wish_id = ? AND event_key IN ({placeholders})",
                    (wish_id, *lifecycle_keys),
                )
                if trigger_ids:
                    trigger_placeholders = ", ".join("?" for _ in trigger_ids)
                    connection.execute(
                        f"""UPDATE timeline_events
                            SET wish_id = NULL, trigger_id = NULL
                            WHERE wish_id = ? OR trigger_id IN ({trigger_placeholders})""",
                        (wish_id, *trigger_ids),
                    )
                else:
                    connection.execute(
                        "UPDATE timeline_events SET wish_id = NULL WHERE wish_id = ?", (wish_id,)
                    )
                connection.execute("DELETE FROM event_triggers WHERE wish_id = ?", (wish_id,))
                connection.execute("DELETE FROM wish_days WHERE wish_id = ?", (wish_id,))
                connection.execute("DELETE FROM wishes WHERE wish_id = ?", (wish_id,))
                connection.execute(
                    """INSERT INTO deleted_wish_tombstones(wish_id, request_id, request_hash, deleted_at)
                       VALUES (?, ?, ?, ?)""",
                    (wish["wish_id"], wish["request_id"], wish["request_hash"], deleted_at),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def list_wishes(self, *, include_archived: bool, now: datetime | None = None) -> list[dict[str, Any]]:
        now_dt = self._now(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._finalize_due_in_connection(connection, now_dt)
                query = "SELECT wish_id FROM wishes" + ("" if include_archived else " WHERE status = 'active'") + " ORDER BY created_at DESC, wish_id DESC"
                result = [self._wish_from_connection(connection, row["wish_id"]) for row in connection.execute(query).fetchall()]
                connection.commit(); return [item for item in result if item is not None]
            except Exception:
                connection.rollback(); raise

    def get_wish(self, wish_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._finalize_due_in_connection(connection, self._now(now))
                result = self._wish_from_connection(connection, wish_id)
                connection.commit(); return result
            except Exception:
                connection.rollback(); raise

    def assess_wish_day(self, *, wish_id: str, business_date: str, evaluation: str, source_device_id: str,
                         now: datetime | None = None) -> dict[str, Any] | None:
        now_dt = self._now(now)
        now_text = utc_timestamp(now_dt)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._finalize_due_in_connection(connection, now_dt)
                wish = connection.execute("SELECT * FROM wishes WHERE wish_id = ?", (wish_id,)).fetchone()
                if wish is None:
                    connection.commit(); return None
                try:
                    parsed_date = date.fromisoformat(business_date)
                    if parsed_date.isoformat() != business_date:
                        raise ValueError
                except ValueError:
                    raise ValueError("business_date must be YYYY-MM-DD")
                day = connection.execute("SELECT * FROM wish_days WHERE wish_id = ? AND business_date = ?", (wish_id, business_date)).fetchone()
                if day is None:
                    raise WishDayNotFound("business_date is not a fixed day for this wish")
                if parsed_date > self._business_date(now_dt, int(wish["day_start_hour"]), wish["timezone"]):
                    raise FutureWishDay("future wish days cannot be assessed")
                if day["evaluation"] == evaluation and day["evaluation_source"] == "manual":
                    connection.commit(); return self._wish_day(day)
                revised = wish["status"] == "archived" and day["evaluation"] is not None and day["evaluation"] != evaluation
                revision = int(day["revision"]) + (1 if revised else 0)
                connection.execute("UPDATE wish_days SET evaluation=?, evaluation_source='manual', evaluated_at=?, revision=? WHERE wish_id=? AND business_date=?",
                    (evaluation, now_text, revision, wish_id, business_date))
                if revised:
                    completed = connection.execute("SELECT COUNT(*) FROM wish_days WHERE wish_id=? AND evaluation='completed'", (wish_id,)).fetchone()[0]
                    self._append_timeline(connection, occurred_at=now_text, event_key="wish.result_revised", category="wish", importance="normal", title=f"「{wish['text']}」心愿结果已修改", source_kind="user", source_device_id=source_device_id, wish_id=wish_id, trigger_id=None, subject={"business_date": business_date}, evidence={"completed_days": completed, "duration_days": int(wish["duration_days"])}, dedupe_key=f"wish:{wish_id}:result_revised:{business_date}:{revision}")
                result = connection.execute("SELECT * FROM wish_days WHERE wish_id=? AND business_date=?", (wish_id, business_date)).fetchone()
                connection.commit(); return self._wish_day(result)  # type: ignore[arg-type]
            except Exception:
                connection.rollback(); raise

    def cancel_wish(self, wish_id: str, *, source_device_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        now_dt = self._now(now)
        now_text = utc_timestamp(now_dt)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._finalize_due_in_connection(connection, now_dt)
                wish = connection.execute("SELECT * FROM wishes WHERE wish_id=?", (wish_id,)).fetchone()
                if wish is None:
                    connection.commit(); return None
                if wish["status"] == "archived":
                    raise WishNotCancellable("normally archived wishes cannot be cancelled")
                if wish["status"] == "active":
                    connection.execute("UPDATE wishes SET status='cancelled', cancelled_at=?, archived_at=? WHERE wish_id=?", (now_text, now_text, wish_id))
                    connection.execute("UPDATE event_triggers SET enabled = 0, updated_at = ? WHERE wish_id = ? AND enabled = 1", (now_text, wish_id))
                    self._append_timeline(connection, occurred_at=now_text, event_key="wish.cancelled", category="wish", importance="normal", title=f"「{wish['text']}」心愿已取消", source_kind="user", source_device_id=source_device_id, wish_id=wish_id, trigger_id=None, subject={}, evidence={}, dedupe_key=f"wish:{wish_id}:cancelled")
                result = self._wish_from_connection(connection, wish_id)
                connection.commit(); return result
            except Exception:
                connection.rollback(); raise

    TRIGGER_TYPES: tuple[dict[str, Any], ...] = (
        {"trigger_type": "blacklist_usage_milestone", "display_name": "Blacklist usage milestone", "config_version": 1, "target_scopes": ["global", "wish"], "interval_minutes": {"minimum": 1, "allowed_values": [15, 30, 60, 120]}, "parameters_schema": {"required": ["platform_scope"], "properties": {"platform_scope": {"enum": ["all", "pc", "android", "web"], "default": "all"}}}},
        {"trigger_type": "device_usage_milestone", "display_name": "Device usage milestone", "config_version": 1, "target_scopes": ["global", "wish"], "interval_minutes": {"minimum": 1, "allowed_values": [15, 30, 60, 120]}, "parameters_schema": {"required": ["device_id"], "properties": {"device_id": {"type": "string"}}}},
        {"trigger_type": "late_usage_milestone", "display_name": "Late usage milestone", "config_version": 1, "target_scopes": ["global", "wish"], "interval_minutes": {"minimum": 1, "allowed_values": [15, 30, 60, 120]}, "parameters_schema": {"required": ["device_id", "start_local_time"], "properties": {"device_id": {"type": "string", "default": "all"}, "start_local_time": {"pattern": "HH:MM"}}}},
        {"trigger_type": "scheduled_reminder", "display_name": "Scheduled reminder", "config_version": 1, "target_scopes": ["wish"], "interval_minutes": {"minimum": 1, "allowed_values": [1]}, "parameters_schema": {"required": ["reminder_local_time"], "properties": {"reminder_local_time": {"pattern": "HH:MM"}}}},
    )

    @classmethod
    def trigger_types(cls) -> list[dict[str, Any]]:
        return [dict(item) for item in cls.TRIGGER_TYPES]

    @classmethod
    def validate_trigger(cls, trigger_type: str, config_version: int, parameters: Any, interval_minutes: Any) -> None:
        allowed = {item["trigger_type"]: item for item in cls.TRIGGER_TYPES}
        if trigger_type not in allowed or isinstance(config_version, bool) or not isinstance(config_version, int) or config_version != 1:
            raise ValueError("unsupported trigger type or config_version")
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or interval_minutes not in allowed[trigger_type]["interval_minutes"]["allowed_values"]:
            raise ValueError("interval_minutes is not allowed for this trigger type")
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be an object")
        expected = {
            "blacklist_usage_milestone": {"platform_scope"},
            "device_usage_milestone": {"device_id"},
            "late_usage_milestone": {"device_id", "start_local_time"},
            "scheduled_reminder": {"reminder_local_time"},
        }[trigger_type]
        if set(parameters) != expected:
            raise ValueError("parameters contain missing or undeclared fields")
        if trigger_type == "blacklist_usage_milestone" and parameters["platform_scope"] not in {"all", "pc", "android", "web"}:
            raise ValueError("invalid platform_scope")
        if "device_id" in expected and (not isinstance(parameters["device_id"], str) or not parameters["device_id"].strip() or len(parameters["device_id"]) > 200):
            raise ValueError("invalid device_id")
        if trigger_type == "late_usage_milestone" and (not isinstance(parameters["start_local_time"], str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", parameters["start_local_time"]) is None):
            raise ValueError("invalid start_local_time")
        if trigger_type == "scheduled_reminder" and (not isinstance(parameters["reminder_local_time"], str) or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", parameters["reminder_local_time"]) is None):
            raise ValueError("invalid reminder_local_time")

    @staticmethod
    def _trigger_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"trigger_id": row["trigger_id"], "wish_id": row["wish_id"], "trigger_type": row["trigger_type"], "config_version": int(row["config_version"]), "parameters": json.loads(row["parameters_json"]), "interval_minutes": int(row["interval_minutes"]), "enabled": bool(row["enabled"]), "created_at": row["created_at"], "updated_at": row["updated_at"], "last_triggered_at": row["last_triggered_at"]}

    def create_trigger(self, *, request_id: str, request_hash: str, wish_id: str | None, trigger_type: str,
                       config_version: int, parameters: dict[str, Any], interval_minutes: int, enabled: bool,
                       now: datetime | None = None) -> dict[str, Any]:
        self.validate_trigger(trigger_type, config_version, parameters, interval_minutes)
        if trigger_type == "scheduled_reminder" and wish_id is None:
            raise ValueError("scheduled_reminder requires wish_id")
        now_text = utc_timestamp(self._now(now))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute("SELECT request_hash, trigger_id FROM event_triggers WHERE request_id=?", (request_id,)).fetchone()
                if prior is not None:
                    if prior["request_hash"] != request_hash: raise IdempotencyConflict("request_id was already used with different request content")
                    result = connection.execute("SELECT * FROM event_triggers WHERE trigger_id=?", (prior["trigger_id"],)).fetchone()
                    connection.commit(); return self._trigger_from_row(result)
                if wish_id is not None:
                    wish = connection.execute("SELECT status FROM wishes WHERE wish_id=?", (wish_id,)).fetchone()
                    if wish is None: raise KeyError("wish_not_found")
                    if wish["status"] != "active": raise TriggerConfigurationConflict("triggers can only be enabled for active wishes")
                try:
                    connection.execute("""INSERT INTO event_triggers(trigger_id, request_id, request_hash, wish_id, trigger_type, config_version, parameters_json, interval_minutes, enabled, created_at, updated_at, last_triggered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""", (str(uuid.uuid4()), request_id, request_hash, wish_id, trigger_type, config_version, canonical_json(parameters), interval_minutes, int(enabled), now_text, now_text))
                except sqlite3.IntegrityError as error:
                    raise TriggerConfigurationConflict("trigger configuration already exists") from error
                result = connection.execute("SELECT * FROM event_triggers WHERE request_id=?", (request_id,)).fetchone()
                connection.commit(); return self._trigger_from_row(result)
            except Exception:
                connection.rollback(); raise

    def list_triggers(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._trigger_from_row(row) for row in connection.execute("SELECT * FROM event_triggers ORDER BY created_at DESC, trigger_id DESC").fetchall()]

    def patch_trigger(self, trigger_id: str, patch: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM event_triggers WHERE trigger_id=?", (trigger_id,)).fetchone()
                if row is None: connection.commit(); return None
                wish_id = patch.get("wish_id", row["wish_id"])
                if "wish_id" in patch and wish_id is not None:
                    wish = connection.execute("SELECT status FROM wishes WHERE wish_id=?", (wish_id,)).fetchone()
                    if wish is None: raise KeyError("wish_not_found")
                    if wish["status"] != "active": raise TriggerConfigurationConflict("triggers can only be associated with active wishes")
                if row["wish_id"] is not None:
                    current_wish = connection.execute("SELECT status FROM wishes WHERE wish_id=?", (row["wish_id"],)).fetchone()
                    changes_configuration = any(key in patch for key in ("wish_id", "parameters", "interval_minutes")) or patch.get("enabled") is True
                    if current_wish is not None and current_wish["status"] != "active" and changes_configuration:
                        raise TriggerConfigurationConflict("inactive wishes cannot regain or change trigger configuration")
                parameters = patch.get("parameters", json.loads(row["parameters_json"]))
                interval = patch.get("interval_minutes", int(row["interval_minutes"]))
                enabled = patch.get("enabled", bool(row["enabled"]))
                self.validate_trigger(row["trigger_type"], int(row["config_version"]), parameters, interval)
                if not isinstance(enabled, bool): raise ValueError("enabled must be boolean")
                connection.execute("UPDATE event_triggers SET wish_id=?, parameters_json=?, interval_minutes=?, enabled=?, updated_at=? WHERE trigger_id=?", (wish_id, canonical_json(parameters), interval, int(enabled), utc_timestamp(self._now(now)), trigger_id))
                result = connection.execute("SELECT * FROM event_triggers WHERE trigger_id=?", (trigger_id,)).fetchone()
                connection.commit(); return self._trigger_from_row(result)
            except Exception:
                connection.rollback(); raise

    def delete_trigger(self, trigger_id: str) -> bool:
        with self._connection() as connection:
            return connection.execute("DELETE FROM event_triggers WHERE trigger_id=?", (trigger_id,)).rowcount > 0

    def list_timeline(self, start: datetime, end: datetime, *, category: str | None = None,
                      wish_id: str | None = None, importance: str | None = None) -> dict[str, Any]:
        filters, params = ["occurred_at >= ?", "occurred_at < ?"], [utc_timestamp(start), utc_timestamp(end)]
        for name, value in (("category", category), ("wish_id", wish_id), ("importance", importance)):
            if value is not None: filters.append(f"{name} = ?"); params.append(value)
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM timeline_events WHERE " + " AND ".join(filters) + " ORDER BY occurred_at DESC, timeline_event_id DESC", params).fetchall()
            device_names = {
                str(row["device_id"]): str(row["effective_name"])
                for row in connection.execute(
                    """SELECT device_id,
                              COALESCE(custom_name, display_name) AS effective_name
                       FROM devices"""
                )
            }
        primary_reader, served_event_ids = self.ai_readers.served_event_ids_for_primary()
        events = []
        for row in rows:
            subject = json.loads(row["subject_json"])
            evidence = json.loads(row["evidence_json"])
            referenced_device_id = subject.get("device_id") if isinstance(subject, dict) else None
            referenced_device_id = referenced_device_id or row["source_device_id"]
            device_name = device_names.get(str(referenced_device_id)) if referenced_device_id else None
            title = str(row["title"])
            if row["event_key"] == "device_usage_milestone" and device_name and not row["wish_id"]:
                title = f"设备使用·{device_name}"
            ai_reader_state = "not_applicable" if row["importance"] == "low" else (
                "served" if str(row["timeline_event_id"]) in served_event_ids else "not_served"
            )
            events.append({
                "timeline_event_id": row["timeline_event_id"], "occurred_at": row["occurred_at"],
                "created_at": row["created_at"], "event_key": row["event_key"],
                "category": row["category"], "importance": row["importance"],
                "title": title, "detail": _display_duration_text(row["detail"]), "source_kind": row["source_kind"],
                "source_device_id": row["source_device_id"],
                "device_display_name": device_name,
                "wish_id": row["wish_id"], "trigger_id": row["trigger_id"],
                "subject": subject, "evidence": evidence,
                "statistics_window": json.loads(row["statistics_window_json"]) if row["statistics_window_json"] else None,
                "delivery": json.loads(row["delivery_json"]) if row["delivery_json"] else None,
                "dedupe_key": row["dedupe_key"],
                "ai_reader": {
                    "state": ai_reader_state,
                    "reader_id": primary_reader["reader_id"] if primary_reader else None,
                    "reader_display_name": primary_reader["display_name"] if primary_reader else None,
                },
            })
        return {"window": {"from": utc_timestamp(start), "to": utc_timestamp(end)}, "events": events}

    def create_client_invitation(
        self,
        *,
        invitation_id: str,
        invitation_token: str,
        scope: str,
        central_base_url: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        token_hash = self.token_hash(invitation_token)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO client_invitations(
                    invitation_id, token_hash, scope, central_base_url,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    invitation_id,
                    token_hash,
                    scope,
                    central_base_url,
                    created_at,
                    expires_at,
                ),
            )

    def claim_client_invitation(
        self,
        *,
        invitation_id: str,
        invitation_token: str,
        device_id: str,
        platform: str,
        display_name: str,
        claimed_at: str,
        credential_provider: Callable[[str, bool, str], tuple[str, str | None]],
    ) -> dict[str, Any]:
        """Claim once, while allowing an identical device to retry safely.

        The credential provider runs under SQLite's write lock. It persists the
        permanent credential to external config before this transaction makes
        the claim visible. A failed SQLite commit may therefore leave a safe,
        reusable config binding; the next retry recovers that same credential.
        """
        supplied_hash = self.token_hash(invitation_token)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT invitation_id, token_hash, scope, central_base_url,
                           expires_at, claimed_device_id, claimed_platform,
                           claimed_display_name, claimed_at, issued_at
                    FROM client_invitations
                    WHERE invitation_id = ?
                    """,
                    (invitation_id,),
                ).fetchone()
                if row is None or not hmac.compare_digest(
                    str(row["token_hash"]), supplied_hash
                ):
                    raise InvitationInvalid("invitation is missing or invalid")

                expires = datetime.fromisoformat(
                    str(row["expires_at"])[:-1] + "+00:00"
                )
                claimed = row["claimed_device_id"] is not None
                if datetime.fromisoformat(
                    claimed_at[:-1] + "+00:00"
                ) >= expires:
                    raise InvitationExpired("invitation has expired")
                if claimed and str(row["claimed_device_id"]) != device_id:
                    raise InvitationAlreadyClaimed(
                        "invitation was already claimed by another device"
                    )

                upload_token, read_token = credential_provider(
                    device_id, not claimed, str(row["scope"])
                )
                if claimed:
                    issued_at = str(row["issued_at"])
                    result_platform = str(row["claimed_platform"])
                    result_display_name = str(row["claimed_display_name"])
                else:
                    issued_at = claimed_at
                    result_platform = platform
                    result_display_name = display_name
                    connection.execute(
                        """
                        UPDATE client_invitations
                        SET claimed_device_id = ?, claimed_platform = ?,
                            claimed_display_name = ?, claimed_at = ?, issued_at = ?
                        WHERE invitation_id = ?
                        """,
                        (
                            device_id,
                            platform,
                            display_name,
                            claimed_at,
                            issued_at,
                            invitation_id,
                        ),
                    )
                digest = self.token_hash(upload_token)
                existing = connection.execute(
                    "SELECT device_id FROM device_tokens WHERE token_hash = ?",
                    (digest,),
                ).fetchone()
                if existing is not None and str(existing["device_id"]) != device_id:
                    raise ValueError(
                        "one central device token cannot be rebound to another device_id"
                    )
                connection.execute(
                    """
                    INSERT INTO device_tokens(token_hash, device_id, created_at, revoked_at)
                    VALUES (?, ?, ?, NULL)
                    ON CONFLICT(token_hash) DO UPDATE SET revoked_at = NULL
                    """,
                    (digest, device_id, claimed_at),
                )
                if not claimed:
                    # A new invitation is the only supported way to reactivate
                    # the same stable installation after logical deletion.
                    connection.execute(
                        "UPDATE devices SET retired_at = NULL WHERE device_id = ?",
                        (device_id,),
                    )
                connection.commit()
                return {
                    "scope": str(row["scope"]),
                    "central_base_url": str(row["central_base_url"]),
                    "device_id": device_id,
                    "platform": result_platform,
                    "display_name": result_display_name,
                    "upload_token": upload_token,
                    "read_token": read_token,
                    "issued_at": issued_at,
                    "idempotent": claimed,
                }
            except Exception:
                connection.rollback()
                raise

    # ---------------------------------------------------------------
    #  Blacklist rules
    # ---------------------------------------------------------------

    SEED_RULES: tuple[tuple[str, str, str], ...] = (
        ("app", "steam", "Steam 游戏"),
        ("app", "Minecraft", "Minecraft"),
        ("app", "javaw", "Minecraft (Java)"),
        ("app", "steamwebhelper", "Steam"),
        ("domain", "bilibili.com", "B站"),
        ("domain", "jandan.net", "煎蛋"),
        ("domain", "zhihu.com", "知乎"),
    )

    @staticmethod
    def _normalise_blacklist_pattern(rule_type: str, pattern: str) -> str:
        text = pattern.strip().casefold()
        if rule_type == "domain":
            text = re.sub(r"^www\.", "", text)
            text = text.rstrip(".")
        return text

    @classmethod
    def _seed_blacklist_rules(cls, connection: sqlite3.Connection, *, table_already_existed: bool = False) -> None:
        sentinel = connection.execute(
            "SELECT value FROM kv WHERE key = 'blacklist_seed_v1'"
        ).fetchone()
        if sentinel is not None:
            return
        if table_already_existed:
            # Table existed before this run — only write sentinel, never re-seed
            connection.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES('blacklist_seed_v1', '1')"
            )
            return
        now = utc_timestamp()
        for rule_type, pattern, label in cls.SEED_RULES:
            normalized = cls._normalise_blacklist_pattern(rule_type, pattern)
            platform_scope = "pc" if rule_type == "app" else "web"
            connection.execute(
                """
                INSERT OR IGNORE INTO blacklist_rules(
                    rule_id, rule_type, pattern, normalized_pattern, platform_scope,
                    label, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (str(uuid.uuid4()), rule_type, pattern, normalized, platform_scope, label, now, now),
            )
        connection.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES('blacklist_seed_v1', '1')"
        )

    def list_blacklist_rules(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT rule_id, rule_type, pattern, normalized_pattern,
                       platform_scope, label, enabled, created_at, updated_at
                FROM blacklist_rules
                ORDER BY rule_type, created_at, rule_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_blacklist_rule(
        self, rule_type: str, pattern: str, label: str, *,
        enabled: bool = True, platform_scope: str = "pc",
    ) -> dict[str, Any]:
        now = utc_timestamp()
        normalized = self._normalise_blacklist_pattern(rule_type, pattern)
        if not normalized:
            raise ValueError("pattern must not be empty")
        if rule_type == "app" and platform_scope not in {"pc", "android"}:
            raise ValueError("app rules require platform_scope of pc or android")
        if rule_type == "domain" and platform_scope != "web":
            raise ValueError("domain rules require platform_scope of web")
        rule_id = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO blacklist_rules(
                    rule_id, rule_type, pattern, normalized_pattern, platform_scope,
                    label, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rule_id, rule_type, pattern, normalized, platform_scope, label, int(enabled), now, now),
            )
        return self._get_rule(rule_id)

    def update_blacklist_rule(
        self, rule_id: str, label: str | None = None, enabled: bool | None = None
    ) -> dict[str, Any] | None:
        updates = []
        params: list[Any] = []
        if label is not None:
            updates.append("label = ?")
            params.append(label)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(int(enabled))
        if not updates:
            return self._get_rule(rule_id)
        updates.append("updated_at = ?")
        params.append(utc_timestamp())
        params.append(rule_id)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE blacklist_rules SET {', '.join(updates)} WHERE rule_id = ?",
                params,
            )
        return self._get_rule(rule_id)

    def delete_blacklist_rule(self, rule_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM blacklist_rules WHERE rule_id = ?", (rule_id,)
            )
        return cursor.rowcount > 0

    def _get_rule(self, rule_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT rule_id, rule_type, pattern, normalized_pattern,
                       platform_scope, label, enabled, created_at, updated_at
                FROM blacklist_rules
                WHERE rule_id = ?
                """,
                (rule_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def enabled_patterns(self, rule_type: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT normalized_pattern
                FROM blacklist_rules
                WHERE rule_type = ? AND enabled = 1
                ORDER BY pattern
                """,
                (rule_type,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def enabled_patterns_for_scope(self, rule_type: str, platform_scope: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT normalized_pattern
                FROM blacklist_rules
                WHERE rule_type = ? AND platform_scope = ? AND enabled = 1
                ORDER BY pattern
                """,
                (rule_type, platform_scope),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def _active_app_patterns(self, platform: str = "pc") -> list[str]:
        return self.enabled_patterns_for_scope("app", platform)

    def _active_domain_patterns(self) -> list[str]:
        return self.enabled_patterns_for_scope("domain", "web")

    def _is_enabled_app(self, name: str, platform: str = "pc") -> bool:
        lowered = name.casefold()
        return any(term in lowered for term in self._active_app_patterns(platform))

    def _is_enabled_domain(self, domain: str) -> bool:
        lowered = domain.casefold()
        lowered = re.sub(r"^www\.", "", lowered).rstrip(".")
        for pattern in self._active_domain_patterns():
            if lowered == pattern or lowered.endswith("." + pattern):
                return True
        return False

    def ping(self) -> None:
        with self._connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def journal_mode(self) -> str:
        with self._connection() as connection:
            row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    def count_events(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def fetch_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT event_id, device_id, occurred_at, event_type, source_json,
                       duration_seconds, revision, payload_json, event_json,
                       content_hash, is_mutable, received_at, updated_at
                FROM events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["source"] = json.loads(result.pop("source_json"))
        result["payload"] = json.loads(result.pop("payload_json"))
        result["event"] = json.loads(result.pop("event_json"))
        result["is_mutable"] = bool(result["is_mutable"])
        return result

    def read_devices(
        self,
        start: Any,
        end: Any,
        local_device_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            return devices_view(connection, start, end, local_device_id)

    def read_locations(
        self,
        start: Any,
        end: Any,
        local_device_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            return locations_view(connection, start, end, local_device_id)

    def read_usage(
        self,
        start: Any,
        end: Any,
        local_device_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            return usage_view(
                connection, start, end, local_device_id,
                is_blacklisted_app=lambda name, plat: self._is_enabled_app(name, plat),
                is_blacklisted_site=lambda domain, plat: self._is_enabled_domain(domain),
            )

    def read_health_info(self, target_date: date, *, now: datetime | None = None) -> dict[str, object]:
        """Dynamically derive health references without materializing a second truth."""
        with self._connection() as connection:
            return build_health_info(connection, target_date, now=now)

    @staticmethod
    def _calendar_module(event_type: str) -> str:
        """Assign each raw event to exactly one calendar-size module."""
        if event_type in {"app.foreground", "web.foreground", "device.input_state"}:
            return "usage"
        if event_type.startswith("location."):
            return "location"
        if event_type.startswith("health."):
            return "health"
        return "other"

    @staticmethod
    def _calendar_business_date(occurred_at: str, day_start_hour: int) -> str:
        timestamp = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        local = timestamp.astimezone(timezone(timedelta(hours=8))) - timedelta(hours=day_start_hour)
        return local.date().isoformat()

    @staticmethod
    def _calendar_timeline_bytes(row: sqlite3.Row) -> int:
        """Measure stable, user-readable timeline content without SQLite metadata."""
        content = {
            "occurred_at": row["occurred_at"],
            "created_at": row["created_at"],
            "event_key": row["event_key"],
            "category": row["category"],
            "importance": row["importance"],
            "title": row["title"],
            "detail": row["detail"],
            "source_kind": row["source_kind"],
            "source_device_id": row["source_device_id"],
            "wish_id": row["wish_id"],
            "trigger_id": row["trigger_id"],
            "subject": json.loads(row["subject_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "statistics_window": json.loads(row["statistics_window_json"])
            if row["statistics_window_json"] else None,
            "delivery": json.loads(row["delivery_json"]) if row["delivery_json"] else None,
        }
        return len(canonical_json(content).encode("utf-8"))

    def calendar_days(self, from_date: str, to_date: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Return logical central content sizes grouped by the shared business day.

        Raw events are measured once from their canonical event JSON, rather than
        counting SQLite's duplicated projections. Timeline rows use a stable
        user-readable projection, excluding IDs, indexes, and other storage metadata.
        """
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
        if start_date.isoformat() != from_date or end_date.isoformat() != to_date:
            raise ValueError("from and to must be YYYY-MM-DD")
        if end_date < start_date:
            raise ValueError("to must not be before from")
        if (end_date - start_date).days + 1 > 42:
            raise ValueError("calendar range must include at most 42 days")

        settings = self.get_shared_settings()
        day_start_hour = int(settings["day_start_hour"])
        local_zone = timezone(timedelta(hours=8))
        range_start = datetime.combine(start_date, time(day_start_hour), tzinfo=local_zone).astimezone(timezone.utc)
        range_end = datetime.combine(end_date + timedelta(days=1), time(day_start_hour), tzinfo=local_zone).astimezone(timezone.utc)
        modules = ("usage", "location", "health", "timeline", "other")
        days = {
            (start_date + timedelta(days=offset)).isoformat(): {
                "business_date": (start_date + timedelta(days=offset)).isoformat(),
                "available": False,
                "total_bytes": 0,
                "modules": {name: {"bytes": 0, "records": 0} for name in modules},
            }
            for offset in range((end_date - start_date).days + 1)
        }
        bounds = (utc_timestamp(range_start), utc_timestamp(range_end))
        with self._connection() as connection:
            raw_rows = connection.execute(
                """SELECT occurred_at, event_type, event_json FROM events
                   WHERE occurred_at >= ? AND occurred_at < ?""", bounds
            ).fetchall()
            timeline_rows = connection.execute(
                """SELECT occurred_at, created_at, event_key, category, importance, title, detail,
                          source_kind, source_device_id, wish_id, trigger_id, subject_json, evidence_json,
                          statistics_window_json, delivery_json
                   FROM timeline_events WHERE occurred_at >= ? AND occurred_at < ?""", bounds
            ).fetchall()
            earliest_latest = connection.execute(
                """SELECT MIN(occurred_at) AS earliest, MAX(occurred_at) AS latest FROM (
                       SELECT occurred_at FROM events
                       UNION ALL
                       SELECT occurred_at FROM timeline_events
                   )"""
            ).fetchone()

        for row in raw_rows:
            business_date = self._calendar_business_date(str(row["occurred_at"]), day_start_hour)
            day = days.get(business_date)
            if day is None:
                continue
            module = self._calendar_module(str(row["event_type"]))
            # event_json is the canonical full event representation; source_json
            # and payload_json are intentionally not counted again.
            byte_count = len(canonical_json(json.loads(row["event_json"])).encode("utf-8"))
            day["available"] = True
            day["modules"][module]["bytes"] += byte_count
            day["modules"][module]["records"] += 1
            day["total_bytes"] += byte_count
        for row in timeline_rows:
            business_date = self._calendar_business_date(str(row["occurred_at"]), day_start_hour)
            day = days.get(business_date)
            if day is None:
                continue
            byte_count = self._calendar_timeline_bytes(row)
            day["available"] = True
            day["modules"]["timeline"]["bytes"] += byte_count
            day["modules"]["timeline"]["records"] += 1
            day["total_bytes"] += byte_count

        current = now or datetime.now(timezone.utc)
        earliest = earliest_latest["earliest"]
        latest = earliest_latest["latest"]
        return {
            "timezone": settings["timezone"],
            "day_start_hour": day_start_hour,
            "today_business_date": self._business_date(current, day_start_hour, settings["timezone"]).isoformat(),
            "earliest_available_date": self._calendar_business_date(str(earliest), day_start_hour) if earliest else None,
            "latest_available_date": self._calendar_business_date(str(latest), day_start_hour) if latest else None,
            "days": list(days.values()),
        }

    def event_background(self, business_date: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        """Build the v1.13 dynamic background from existing read models only."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        settings = self.get_shared_settings()
        local = now_dt + timedelta(hours=8) - timedelta(hours=int(settings["day_start_hour"]))
        target = date.fromisoformat(business_date) if business_date else local.date()
        start = datetime.combine(target, time(int(settings["day_start_hour"])), tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        with self._connection() as connection:
            usage = usage_view(connection, start, min(end, now_dt), None,
                is_blacklisted_app=lambda name, plat: self._is_enabled_app(name, plat),
                is_blacklisted_site=lambda domain, plat: self._is_enabled_domain(domain))
            wishes = [self._wish_from_connection(connection, str(row["wish_id"])) for row in connection.execute("SELECT wish_id FROM wishes WHERE status = 'active' ORDER BY created_at DESC").fetchall()]
            location = locations_view(connection, start, min(end, now_dt), None, now=now_dt)
            realtime = []
            online_devices = []
            for row in connection.execute("""SELECT device_id, COALESCE(custom_name, display_name) name, last_seen_at
                                             FROM devices WHERE retired_at IS NULL
                                             ORDER BY COALESCE(custom_name, display_name), device_id""").fetchall():
                observed = row["last_seen_at"]
                try:
                    stale = now_dt - datetime.fromisoformat(str(observed).replace("Z", "+00:00")) > timedelta(minutes=15)
                except (TypeError, ValueError):
                    stale = True
                if not stale:
                    online_devices.append(row)
            latest_apps: dict[str, dict[str, Any]] = {}
            online_ids = {str(row["device_id"]) for row in online_devices}
            for row in connection.execute("""SELECT e.device_id, COALESCE(d.custom_name,d.display_name) name, e.occurred_at, e.duration_seconds, e.payload_json
                                             FROM events e JOIN devices d ON d.device_id=e.device_id
                                             WHERE d.retired_at IS NULL AND e.event_type='app.foreground'
                                               AND e.occurred_at >= ? AND e.occurred_at <= ?
                                             ORDER BY e.device_id, e.occurred_at DESC""", (utc_timestamp(start), utc_timestamp(now_dt))).fetchall():
                device_id = str(row["device_id"])
                if device_id not in online_ids or device_id in latest_apps:
                    continue
                try: payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError): payload = {}
                aw = payload.get("activitywatch") if isinstance(payload, dict) and isinstance(payload.get("activitywatch"), dict) else {}
                if aw.get("kind") == "afk":
                    continue
                app = payload.get("app") if isinstance(payload, dict) and isinstance(payload.get("app"), dict) else {}
                name = str(app.get("display_name") or app.get("package_name") or "未命名应用")
                occurred = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                observed_at = min(now_dt, occurred + timedelta(seconds=max(0, int(row["duration_seconds"] or 0))))
                stale = now_dt - observed_at > timedelta(minutes=15)
                if not stale:
                    latest_apps[device_id] = {"kind":"current_app", "observed_at":utc_timestamp(observed_at), "is_stale":False, "include_in_ai":True, "device_id":row["device_id"], "display_text":f"{row['name']}正在使用{name}。"}
            # Current device and application are emitted as one visual group.
            # Offline devices and their old applications do not belong in the
            # current-state background at all.
            for row in online_devices:
                device_id = str(row["device_id"])
                realtime.append({"kind":"device_online", "observed_at":row["last_seen_at"], "is_stale":False, "include_in_ai":True, "device_id":row["device_id"], "display_text":f"{row['name']}在线。"})
                if device_id in latest_apps:
                    realtime.append(latest_apps[device_id])
        total = sum(sum(int(v) for v in d.get("hourly", {}).values()) for d in usage.get("devices", []))
        # Blacklist usage is computed by matching enabled rules against each
        # device's apps/sites here; usage_view does not emit per-device
        # blacklist_apps/blacklist_sites fields.
        black = 0
        for d in usage.get("devices", []):
            platform = str(d.get("platform") or "")
            scope = "android" if platform == "android" else "pc"
            for name, seconds in (d.get("apps") or {}).items():
                if self._is_enabled_app(str(name), scope):
                    black += int(seconds)
            for domain, seconds in (d.get("sites") or {}).items():
                if self._is_enabled_domain(str(domain)):
                    black += int(seconds)
        def format_wish_context(wish: dict[str, Any], today_iso: str) -> str:
            days = [d for d in wish.get("wish_days", []) if isinstance(d, dict)]
            completed = [d for d in days if d.get("evaluation") == "completed"]
            not_completed = [
                d for d in days
                if d.get("evaluation") == "not_completed"
                and str(d.get("business_date") or "") <= today_iso
            ]
            pending_prior = [
                d for d in days
                if d.get("evaluation") is None
                and str(d.get("business_date") or "") < today_iso
            ]
            pending_today = [
                d for d in days
                if d.get("evaluation") is None
                and str(d.get("business_date") or "") == today_iso
            ]
            pending = pending_prior + pending_today
            future = [
                d for d in days
                if str(d.get("business_date") or "") > today_iso
            ]

            def dates(items: list[dict[str, Any]]) -> str:
                return "、".join(str(item.get("business_date"))[5:] for item in items)

            duration = int(wish.get("duration_days") or len(days))
            # The numerator is deliberately defined here, rather than inferred
            # from the number of reached days: only explicit completed results
            # count as completed progress.
            parts = [f"周期进度：已完成 {len(completed)}/{duration} 天（仅统计已完成）"]
            if completed:
                parts.append(f"已完成：{dates(completed)}")
            if not_completed:
                parts.append(f"已到达但未完成：{dates(not_completed)}（不需要提醒）")
            if pending_prior:
                parts.append(f"待填写：{dates(pending_prior)}（需要提醒用户填写结果）")
            if pending_today:
                parts.append(f"待填写：{dates(pending_today)}（今天的进度。不需要提醒）")
            if future:
                parts.append(f"尚未到达：{dates(future)}（不计入进度，不需要提醒）")
            return "；".join(parts)

        wish_items = []
        for wish in wishes:
            if not wish: continue
            if wish["status"] != "active": continue
            days = wish.get("wish_days", [])
            today_iso = target.isoformat()
            pending = any(
                d.get("evaluation") is None
                and str(d.get("business_date") or "") <= today_iso
                for d in days
            )
            state = "待完结" if pending and today_iso > str(wish.get("ends_on") or "") else "进行中"
            wish_items.append({
                "item_key": f"wish:{wish['wish_id']}",
                "text": f"「{wish['text']}」{state}，{format_wish_context(wish, today_iso)}。",
            })
        device_items = [{"item_key":"usage.total","text":f"所有设备当前业务日累计使用 {total // 3600} 小时 {(total % 3600) // 60} 分钟。"}] if total else []
        black_hours, black_minutes = divmod(black, 3600)
        black_minutes = black_minutes // 60
        black_duration = f"{black_hours} 小时 {black_minutes} 分钟" if black_hours else f"{black_minutes} 分钟"
        black_items = [{"item_key":"blacklist.total","text":f"黑名单当前业务日累计使用 {black_duration}。"}]
        location_items = []
        current_stays = location.get("current_stays", [])
        if current_stays:
            stay = current_stays[0]; observed = stay.get("observed_at"); realtime.append({"kind":"current_location","observed_at":observed,"is_stale":False,"include_in_ai":True,"device_id":stay.get("device_id"),"display_text":f"当前位于{stay.get('label') or '未解析地址'}。"}); location_items.append({"item_key":"location.current","text":f"当前位于{stay.get('label') or '未解析地址'}，已持续 {int(stay.get('duration_seconds', 0)) // 60} 分钟。"})
        elif location.get("latest"):
            latest_location = location["latest"]; observed = latest_location.get("observed_at"); stale = now_dt - datetime.fromisoformat(str(observed).replace("Z", "+00:00")).astimezone(timezone.utc) > timedelta(minutes=15); text = (f"当前位于{latest_location.get('label') or '未解析地址'}。" if not stale else f"上次位于{latest_location.get('label') or '未解析地址'}（上次更新：{str(observed)[11:16]}）。"); realtime.append({"kind":"current_location","observed_at":observed,"is_stale":stale,"include_in_ai":not stale,"device_id":latest_location.get("device_id"),"display_text":text}); location_items.append({"item_key":"location.current" if not stale else "location.last","text":text})
        activity_labels = {"stationary":"静止", "walking":"步行", "running":"跑步", "transport":"乘坐交通工具", "unknown":"未知"}
        activity_state = location.get("activity_state", {})
        current_activity = activity_state.get("current")
        if current_activity:
            state_text = activity_labels.get(str(current_activity.get("state")), "未知"); observed = current_activity.get("end_at"); stale = not observed or now_dt - datetime.fromisoformat(str(observed).replace("Z", "+00:00")).astimezone(timezone.utc) > timedelta(minutes=15); realtime.append({"kind":"current_activity","observed_at":observed,"is_stale":stale,"include_in_ai":not stale,"device_id":activity_state.get("primary_device_id"),"display_text":(f"当前处于{state_text}状态。" if not stale else f"上次活动状态为{state_text}（上次更新：{str(observed)[11:16]}）。")})

        # The background includes every classified activity interval that
        # overlaps the trailing hour. An interval crossing the cutoff keeps
        # its original boundaries and duration; it is never clipped to the
        # one-hour display window.
        activity_cutoff = now_dt - timedelta(hours=1)
        local_timezone = timezone(timedelta(hours=8))
        for interval in activity_state.get("intervals", []):
            try:
                interval_start = datetime.fromisoformat(str(interval.get("start_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
                interval_end = datetime.fromisoformat(str(interval.get("end_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            if interval_start >= now_dt or interval_end <= activity_cutoff:
                continue
            duration_seconds = max(0, int(interval.get("duration_seconds") or (interval_end - interval_start).total_seconds()))
            duration_minutes = duration_seconds // 60
            duration_hours, remaining_minutes = divmod(duration_minutes, 60)
            if duration_hours and remaining_minutes:
                duration_text = f"{duration_hours} 小时 {remaining_minutes} 分钟"
            elif duration_hours:
                duration_text = f"{duration_hours} 小时"
            else:
                duration_text = f"{duration_minutes} 分钟"
            state_text = activity_labels.get(str(interval.get("state")), "未知")
            start_label = interval_start.astimezone(local_timezone).strftime("%H:%M")
            end_label = interval_end.astimezone(local_timezone).strftime("%H:%M")
            location_items.append({
                "item_key": f"activity.interval:{utc_timestamp(interval_start)}",
                "text": f"{start_label}–{end_label} {state_text}，持续 {duration_text}。",
            })
        guide_texts = [
            "Life Link 是个人事实记录与回顾工具，不替用户评价人格，也不凭空推断动机。",
            "业务日按 Life Link 共享跨日时间划分，不一定在自然日零点结束。",
            "PC 设备使用时长已排除可确认的 AFK 时间；Android 表示前台应用时长。",
            "设备在线只表示最近 15 分钟收到可验证事实或心跳，不等同于持续使用；超过 15 分钟会标注「上次更新」与时间。",
            "心愿表示用户自行设定的、需要 AI 关注的短期目标；它可以用于触发警示提醒事件，也可以用于记录任意目标。心愿进度的分子只统计 evaluation=completed 的日期；未完成和待填写都不计入已完成天数，分母是固定周期总天数。",
            "心愿日期状态必须区分：未完成表示用户已明确记录未完成，不需要因此提醒；待填写表示日期已到但尚无结果，其中早于当前业务日的待填写需要提醒用户填写结果，当前业务日的待填写只是今天的进度、不需要提醒；尚未到达表示未来日期，不计入进度，也不提醒。不要把未完成改写成待填写，也不要把待填写推断为未完成。",
            "AI 只在待填写日期早于当前业务日时提醒用户填写结果；当前业务日待填写、用户已明确记录的未完成、以及尚未到达的日期都不提醒。提醒不应替用户判断结果。",
            "事件是低频事实摘要；背景是动态视图，超过 15 分钟的实时状态不会提供给 AI。",
            "🎯表示心愿关联提醒；📊表示独立系统里程碑；⭐表示应优先关注。",
            "黑名单是用户希望避免过度沉迷的特定应用或网站。黑名单按平台内正确匹配后汇总，不表示单条规则跨平台命中。",
            "睡眠区间只是多设备使用数据汇总的参考估算，而非来自贴身设备的准确采集。区间时长过长不代表睡眠时间长，但区间时长短大概率代表用户缺少睡眠。",
            "活动状态数据由手机收集的步数信息和地理定位信息汇总评估得到；同一分钟缺少任一来源时不生成活动状态，也不会跨缺证据区间补写。位置事实本身仍可独立展示。",
            "位置与活动经过漂移过滤和区间合并；缺少证据时不得补写推测地点或行程。",
            "事件文字是中央确认的事实摘要；不要猜测未显示的应用、位置、心愿结果或动机。",
        ]
        guide = [{"item_key":f"guide:{i}","text":text} for i, text in enumerate(guide_texts, 1)]
        _weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        _local_now = now_dt.astimezone(timezone(timedelta(hours=8)))
        generated_at_label = f"{_local_now.year}年{_local_now.month}月{_local_now.day}日 {_weekdays[_local_now.weekday()]} {_local_now:%H:%M}"
        return {"business_date":target.isoformat(), "generated_at":utc_timestamp(now_dt), "generated_at_label":generated_at_label, "background_summary": {"wish":{"title":"心愿","items":wish_items}, "device_and_apps":{"title":"设备与应用","items":device_items}, "blacklist":{"title":"黑名单","items":black_items}, "location_and_activity":{"title":"位置与活动","items":location_items}, "device_usage_seconds":total, "blacklist_usage_seconds":black}, "ai_understanding":{"title":"AI 理解说明","items":guide,"timezone":"Asia/Shanghai","real_time_valid_for_minutes":15}, "real_time_items":realtime}

    def ingest(self, batch: BatchEnvelope, request_hash: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_batch = connection.execute(
                    "SELECT request_hash, ack_json FROM batches WHERE batch_id = ?",
                    (batch.batch_id,),
                ).fetchone()
                if existing_batch is not None:
                    if existing_batch["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "batch_id was already used with different request content"
                        )
                    acknowledgement = json.loads(existing_batch["ack_json"])
                    connection.commit()
                    return acknowledgement

                received_at = utc_timestamp()
                existing_device = connection.execute(
                    "SELECT platform, retired_at FROM devices WHERE device_id = ?",
                    (batch.device.device_id,),
                ).fetchone()
                if existing_device is not None and existing_device["retired_at"] is not None:
                    raise DeviceIdentityConflict("device is retired and must be enrolled again")
                if (
                    existing_device is not None
                    and existing_device["platform"] != batch.device.platform
                ):
                    raise DeviceIdentityConflict(
                        "device_id is already registered with another platform"
                    )
                connection.execute(
                    """
                    INSERT INTO devices(device_id, platform, display_name, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        platform = excluded.platform,
                        display_name = excluded.display_name,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        batch.device.device_id,
                        batch.device.platform,
                        batch.device.display_name,
                        received_at,
                        received_at,
                    ),
                )

                event_results: list[dict[str, str]] = []
                accepted_event_ids: list[str] = []
                duplicate_event_ids: list[str] = []
                confirmed_event_ids: list[str] = []
                rejected_events: list[dict[str, str]] = []

                for candidate in batch.events:
                    event, rejection = normalize_event(candidate)
                    if rejection is not None:
                        self._append_rejection(event_results, rejected_events, rejection)
                        continue
                    assert event is not None
                    status, rejection = self._store_event(
                        connection,
                        batch.device.device_id,
                        event,
                        received_at,
                    )
                    if rejection is not None:
                        self._append_rejection(event_results, rejected_events, rejection)
                        continue
                    event_results.append({"event_id": event.event_id, "status": status})
                    confirmed_event_ids.append(event.event_id)
                    if status in {"stored", "updated"}:
                        accepted_event_ids.append(event.event_id)
                    if status == "duplicate":
                        duplicate_event_ids.append(event.event_id)

                acknowledgement = {
                    "batch_id": batch.batch_id,
                    "accepted_event_ids": accepted_event_ids,
                    "confirmed_event_ids": confirmed_event_ids,
                    "duplicate_event_ids": duplicate_event_ids,
                    "event_results": event_results,
                    "rejected_events": rejected_events,
                    "received_at": received_at,
                }
                connection.execute(
                    """
                    INSERT INTO batches(
                        batch_id, device_id, request_hash, sent_at, received_at, ack_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.batch_id,
                        batch.device.device_id,
                        request_hash,
                        batch.sent_at,
                        received_at,
                        canonical_json(acknowledgement),
                    ),
                )
                connection.commit()
                # Run milestone evaluators after successful ingest
                try:
                    from central.evaluator import evaluate_all_milestones
                    evaluate_all_milestones(connection, self, now=datetime.now(timezone.utc))
                except Exception:
                    # Derived timeline failure must not reject already-confirmed raw
                    # facts, but it must remain diagnosable and retryable.
                    logger.exception("timeline evaluator failed after ingest")
                return acknowledgement
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _append_rejection(
        event_results: list[dict[str, str]],
        rejected_events: list[dict[str, str]],
        rejection: EventRejection,
    ) -> None:
        result = {
            "event_id": rejection.event_id,
            "status": "rejected",
            "code": rejection.code,
            "message": rejection.message,
        }
        event_results.append(result)
        rejected_events.append(
            {
                "event_id": rejection.event_id,
                "code": rejection.code,
                "message": rejection.message,
            }
        )

    def _store_event(
        self,
        connection: sqlite3.Connection,
        device_id: str,
        event: NormalizedEvent,
        received_at: str,
    ) -> tuple[str, EventRejection | None]:
        existing = connection.execute(
            """
            SELECT device_id, occurred_at, event_type, source_json,
                   duration_seconds, revision, payload_json, content_hash, is_mutable
            FROM events
            WHERE event_id = ?
            """,
            (event.event_id,),
        ).fetchone()

        if existing is None:
            connection.execute(
                """
                INSERT INTO events(
                    event_id, device_id, occurred_at, event_type, source_json,
                    duration_seconds, revision, payload_json, event_json,
                    content_hash, is_mutable, received_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    device_id,
                    event.occurred_at,
                    event.event_type,
                    canonical_json(event.source),
                    event.duration_seconds,
                    event.revision,
                    canonical_json(event.payload),
                    canonical_json(event.document),
                    event.content_hash,
                    int(event.mutable),
                    received_at,
                    received_at,
                ),
            )
            return "stored", None

        if existing["device_id"] != device_id:
            return "rejected", EventRejection(
                event.event_id,
                "event_id_conflict",
                "event_id is already owned by another device",
            )
        if existing["content_hash"] == event.content_hash:
            return "duplicate", None

        existing_revision = int(existing["revision"])
        if event.revision < existing_revision:
            return "rejected", EventRejection(
                event.event_id,
                "stale_revision",
                "revision is older than the stored event",
            )
        if event.revision == existing_revision:
            return "rejected", EventRejection(
                event.event_id,
                "event_conflict",
                "the same event revision has different content",
            )

        if not bool(existing["is_mutable"]) or not event.mutable:
            return "rejected", EventRejection(
                event.event_id,
                "event_conflict",
                "this event type is immutable",
            )
        if existing["occurred_at"] != event.occurred_at:
            return "rejected", EventRejection(
                event.event_id,
                "event_conflict",
                "a mutable event cannot change occurred_at",
            )
        if existing["source_json"] != canonical_json(event.source):
            return "rejected", EventRejection(
                event.event_id,
                "event_conflict",
                "a mutable event cannot change source",
            )
        if str(existing["event_type"]) in {"app.foreground", "device.input_state", "web.foreground"}:
            stored_payload = json.loads(str(existing["payload_json"]))
            if self._mutable_fact_identity(
                str(existing["event_type"]), stored_payload
            ) != self._mutable_fact_identity(event.event_type, event.payload):
                return "rejected", EventRejection(
                    event.event_id,
                    "event_conflict",
                    "a mutable activity fact cannot change its identity",
                )
        if not self._event_type_update_allowed(str(existing["event_type"]), event.event_type):
            return "rejected", EventRejection(
                event.event_id,
                "event_conflict",
                "this mutable event cannot change event_type",
            )
        if str(existing["event_type"]).startswith("location."):
            location_error = self._location_update_error(
                json.loads(str(existing["payload_json"])), event.payload
            )
            if location_error is not None:
                return "rejected", EventRejection(
                    event.event_id,
                    "non_monotonic_update",
                    location_error,
                )
        old_duration = existing["duration_seconds"]
        if old_duration is not None and (
            event.duration_seconds is None
            or event.duration_seconds < int(old_duration)
        ):
            return "rejected", EventRejection(
                event.event_id,
                "non_monotonic_update",
                "duration_seconds cannot decrease",
            )

        connection.execute(
            """
            UPDATE events
            SET event_type = ?,
                duration_seconds = ?,
                revision = ?,
                payload_json = ?,
                event_json = ?,
                content_hash = ?,
                is_mutable = ?,
                updated_at = ?
            WHERE event_id = ?
            """,
            (
                event.event_type,
                event.duration_seconds,
                event.revision,
                canonical_json(event.payload),
                canonical_json(event.document),
                event.content_hash,
                int(event.mutable),
                received_at,
                event.event_id,
            ),
        )
        return "updated", None

    @staticmethod
    def _event_type_update_allowed(stored_type: str, incoming_type: str) -> bool:
        if stored_type == incoming_type:
            return True
        return stored_type == "location.sample" and incoming_type == "location.stay"

    @staticmethod
    def _location_update_error(
        stored_payload: dict[str, Any], incoming_payload: dict[str, Any]
    ) -> str | None:
        if stored_payload.get("is_active") is False:
            return "a finalized location segment cannot be updated"
        if (
            stored_payload.get("is_active") is True
            and incoming_payload.get("is_active") is not True
            and incoming_payload.get("is_active") is not False
        ):
            return "an active location segment update must declare is_active"
        for field in ("latitude", "longitude", "coordinate_precision_digits"):
            if field in stored_payload and incoming_payload.get(field) != stored_payload[field]:
                return f"a location segment update cannot change {field}"
        for field in ("observed_until", "latest_observed_at"):
            old_value = stored_payload.get(field)
            new_value = incoming_payload.get(field)
            if old_value is None or new_value is None:
                continue
            try:
                old_time = datetime.fromisoformat(str(old_value).removesuffix("Z") + "+00:00")
                new_time = datetime.fromisoformat(str(new_value).removesuffix("Z") + "+00:00")
            except ValueError:
                return f"{field} must be a UTC timestamp"
            if new_time < old_time:
                return f"{field} cannot move backwards"
        return None

    @staticmethod
    def _activitywatch_identity(payload: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
        activitywatch = (
            payload.get("activitywatch")
            if isinstance(payload.get("activitywatch"), dict)
            else {}
        )
        app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
        return (
            activitywatch.get("bucket_id"),
            activitywatch.get("event_id"),
            activitywatch.get("kind"),
            app.get("package_name"),
        )

    @classmethod
    def _mutable_fact_identity(cls, event_type: str, payload: dict[str, Any]) -> tuple[Any, ...]:
        if event_type == "device.input_state":
            return (event_type, payload.get("status"))
        if event_type == "web.foreground":
            browser = payload.get("browser_app") if isinstance(payload.get("browser_app"), dict) else {}
            return (
                event_type,
                payload.get("domain"),
                browser.get("package_name"),
                browser.get("process_name"),
            )
        return (event_type, *cls._activitywatch_identity(payload))


def readonly_diagnostics(database_path: Path) -> dict[str, Any]:
    """Inspect central metadata without creating or modifying the database."""
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"central database does not exist: {path}")

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        known_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"devices", "events", "batches"}
        missing = sorted(required - known_tables)
        if missing:
            raise ValueError(
                "database is not an initialized Life Link central store; missing: "
                + ", ".join(missing)
            )
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("devices", "events", "batches")
        }
    finally:
        connection.close()

    return {
        "database": str(path),
        "journal_mode": journal_mode,
        "devices": counts["devices"],
        "events": counts["events"],
        "batches": counts["batches"],
    }
