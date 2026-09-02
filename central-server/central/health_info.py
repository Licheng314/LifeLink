"""Unified central health-information projection."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .health_sleep import derive_sleep_reference
from .health_steps import derive_steps_by_device


HEALTH_TIMEZONE_NAME = "Asia/Shanghai"
try:
    HEALTH_TIMEZONE = ZoneInfo(HEALTH_TIMEZONE_NAME)
except ZoneInfoNotFoundError:  # Windows can run without the optional tzdata package.
    HEALTH_TIMEZONE = timezone(timedelta(hours=8), name=HEALTH_TIMEZONE_NAME)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _step_events(connection: sqlite3.Connection, target_date: date) -> list[dict[str, object]]:
    local_start = datetime.combine(target_date, time.min, HEALTH_TIMEZONE)
    local_end = local_start + timedelta(days=1)
    start_text = _utc_text(local_start)
    end_text = _utc_text(local_end)
    day_rows = connection.execute(
        """
        SELECT device_id, occurred_at, event_id, payload_json
        FROM events
        WHERE event_type = 'health.steps_observation'
          AND occurred_at >= ? AND occurred_at < ?
        ORDER BY device_id, occurred_at, event_id
        """,
        (start_text, end_text),
    ).fetchall()
    device_ids = sorted({str(row["device_id"]) for row in day_rows})
    baseline_rows = []
    for device_id in device_ids:
        baseline = connection.execute(
            """
            SELECT device_id, occurred_at, event_id, payload_json
            FROM events
            WHERE event_type = 'health.steps_observation'
              AND device_id = ? AND occurred_at < ?
            ORDER BY occurred_at DESC, event_id DESC
            LIMIT 1
            """,
            (device_id, start_text),
        ).fetchone()
        if baseline is not None:
            baseline_rows.append(baseline)
    events: list[dict[str, object]] = []
    for row in sorted([*baseline_rows, *day_rows], key=lambda row: (str(row["device_id"]), str(row["occurred_at"]), str(row["event_id"]))):
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            payload = None
        events.append(
            {
                "device_id": str(row["device_id"]),
                "occurred_at": str(row["occurred_at"]),
                "event_type": "health.steps_observation",
                "payload": payload,
            }
        )
    return events


def build_health_info(
    connection: sqlite3.Connection,
    target_date: date,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if target_date > current.astimezone(HEALTH_TIMEZONE).date():
        raise ValueError("date must not be in the future")

    names = {
        str(row["device_id"]): str(row["custom_name"] or row["display_name"] or row["device_id"])
        for row in connection.execute("SELECT device_id, display_name, custom_name FROM devices")
    }
    device_references = {
        str(row["device_id"]): {
            "device_id": str(row["device_id"]),
            "display_name": str(row["custom_name"] or row["display_name"] or row["device_id"]),
            "platform": str(row["platform"]),
        }
        for row in connection.execute("SELECT device_id, platform, display_name, custom_name FROM devices")
    }
    devices = []
    for result in derive_steps_by_device(_step_events(connection, target_date), target_date.isoformat()):
        devices.append(
            {
                "device_id": result.device_id,
                "display_name": names.get(result.device_id, result.device_id),
                "status": result.status,
                "steps": result.steps,
                "hourly_steps": list(result.hourly_steps),
                "sample_count": result.sample_count,
                "first_sample_at": result.first_sample_at,
                "last_sample_at": result.last_sample_at,
                "warnings": list(result.warnings),
            }
        )
    sleep = derive_sleep_reference(connection, target_date, now=current)
    sleep["last_activity_devices"] = [
        device_references[device_id]
        for device_id in sleep.pop("last_activity_device_ids", [])
        if device_id in device_references
    ]
    sleep["first_activity_devices"] = [
        device_references[device_id]
        for device_id in sleep.pop("first_activity_device_ids", [])
        if device_id in device_references
    ]
    for field in ("last_activity_apps", "first_activity_apps"):
        sleep[field] = [
            {
                **activity,
                "device_display_name": device_references.get(activity["device_id"], {}).get(
                    "display_name", activity["device_id"]
                ),
            }
            for activity in sleep[field]
        ]
    return {
        "date": target_date.isoformat(),
        "timezone": HEALTH_TIMEZONE_NAME,
        "sleep": sleep,
        "steps": {"devices": devices},
    }
