"""Central, cross-device sleep-reference derivation.

This is an activity-gap estimate, not a medical sleep measurement.  It only
uses explicit user-interaction intervals and never treats an online process or
a missing PC AFK stream as activity.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc
try:
    HEALTH_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # Windows can run without the optional tzdata package.
    HEALTH_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
MIN_REST_GAP = timedelta(hours=1)
MAX_STITCHED_INTERACTION = timedelta(minutes=30)


@dataclass(frozen=True)
class Interaction:
    start: datetime
    end: datetime
    device_ids: frozenset[str]
    start_device_ids: frozenset[str]
    end_device_ids: frozenset[str]
    start_apps: frozenset[tuple[str, str, str]]
    end_apps: frozenset[tuple[str, str, str]]


def _parse_utc(value: str) -> datetime | None:
    if not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clip(start: datetime, end: datetime, floor: datetime, ceiling: datetime) -> tuple[datetime, datetime] | None:
    clipped_start = max(start, floor)
    clipped_end = min(end, ceiling)
    return (clipped_start, clipped_end) if clipped_start < clipped_end else None


def _merge(interactions: list[Interaction]) -> list[Interaction]:
    merged: list[Interaction] = []
    for item in sorted(interactions, key=lambda value: (value.start, value.end)):
        if not merged or item.start > merged[-1].end:
            merged.append(item)
            continue
        previous = merged[-1]
        start_device_ids = (
            previous.start_device_ids | item.start_device_ids
            if item.start == previous.start
            else previous.start_device_ids
        )
        start_apps = (
            previous.start_apps | item.start_apps
            if item.start == previous.start
            else previous.start_apps
        )
        if item.end > previous.end:
            end_device_ids = item.end_device_ids
            end_apps = item.end_apps
        elif item.end == previous.end:
            end_device_ids = previous.end_device_ids | item.end_device_ids
            end_apps = previous.end_apps | item.end_apps
        else:
            end_device_ids = previous.end_device_ids
            end_apps = previous.end_apps
        merged[-1] = Interaction(
            previous.start,
            max(previous.end, item.end),
            previous.device_ids | item.device_ids,
            start_device_ids,
            end_device_ids,
            start_apps,
            end_apps,
        )
    return merged


def _intersections(windows: list[Interaction], active: list[Interaction]) -> list[Interaction]:
    result: list[Interaction] = []
    for window in windows:
        for presence in active:
            clipped = _clip(window.start, window.end, presence.start, presence.end)
            if clipped is not None:
                result.append(Interaction(
                    clipped[0], clipped[1], window.device_ids,
                    window.device_ids, window.device_ids,
                    window.start_apps, window.end_apps,
                ))
    return _merge(result)


def _night_window(target_date: date, now: datetime) -> tuple[datetime, datetime, datetime, bool]:
    local_start = datetime.combine(target_date - timedelta(days=1), time(21), HEALTH_TIMEZONE)
    local_end = datetime.combine(target_date, time(12), HEALTH_TIMEZONE)
    full_start = local_start.astimezone(UTC)
    full_end = local_end.astimezone(UTC)
    current = now.astimezone(UTC)
    return full_start, min(full_end, current), full_end, current < full_end


def _load_interactions(
    connection: sqlite3.Connection,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[Interaction], list[str]]:
    if window_end <= window_start:
        return [], []
    rows = connection.execute(
        """
        SELECT e.device_id, e.occurred_at, e.duration_seconds, e.event_type, e.payload_json,
               d.platform
        FROM events e
        JOIN devices d ON d.device_id = e.device_id
        WHERE e.event_type IN ('app.foreground', 'device.input_state')
          AND e.occurred_at >= ? AND e.occurred_at < ?
        ORDER BY e.device_id, e.occurred_at, e.event_id
        """,
        (_utc_text(window_start - timedelta(days=1)), _utc_text(window_end)),
    )
    android: list[Interaction] = []
    pc_windows: dict[str, list[Interaction]] = {}
    pc_not_afk: dict[str, list[Interaction]] = {}
    pc_window_devices: set[str] = set()
    for row in rows:
        occurred = _parse_utc(str(row["occurred_at"]))
        duration = row["duration_seconds"]
        if occurred is None or isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            continue
        clipped = _clip(occurred, occurred + timedelta(seconds=duration), window_start, window_end)
        if clipped is None:
            continue
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        device_id = str(row["device_id"])
        platform = str(row["platform"])
        ids = frozenset({device_id})
        event_type = str(row["event_type"])
        if event_type == "device.input_state":
            if platform == "desktop" and payload.get("status") == "active":
                pc_not_afk.setdefault(device_id, []).append(
                    Interaction(clipped[0], clipped[1], ids, ids, ids, frozenset(), frozenset())
                )
            continue
        activitywatch = payload.get("activitywatch")
        activitywatch = activitywatch if isinstance(activitywatch, dict) else {}
        raw = activitywatch.get("data")
        raw = raw if isinstance(raw, dict) else {}
        app = payload.get("app")
        app = app if isinstance(app, dict) else {}
        app_value = (
            (app.get("display_name") or app.get("package_name"))
            if platform == "android"
            else (app.get("display_name") or app.get("package_name") or raw.get("app") or "")
        )
        app_name = app_value.strip() if isinstance(app_value, str) else ""
        apps = frozenset({(device_id, platform, app_name)}) if app_name else frozenset()
        interval = Interaction(clipped[0], clipped[1], ids, ids, ids, apps, apps)
        if platform == "android":
            android.append(interval)
            continue
        if platform != "desktop":
            continue
        kind = str(activitywatch.get("kind") or "window")
        if kind == "window":
            pc_window_devices.add(device_id)
            pc_windows.setdefault(device_id, []).append(interval)
        elif kind == "afk" and raw.get("status") == "not-afk":
            pc_not_afk.setdefault(device_id, []).append(interval)

    interactions = list(android)
    warnings: list[str] = []
    for device_id in sorted(pc_window_devices):
        if not pc_not_afk.get(device_id):
            warnings.append(f"pc_not_afk_missing:{device_id}")
            continue
        interactions.extend(
            _intersections(_merge(pc_windows.get(device_id, [])), _merge(pc_not_afk[device_id]))
        )
    return _merge(interactions), warnings


def _best_rest(interactions: list[Interaction]) -> dict[str, object] | None:
    if len(interactions) < 2:
        return None
    long_gaps = [
        index
        for index in range(len(interactions) - 1)
        if interactions[index + 1].start - interactions[index].end >= MIN_REST_GAP
    ]
    best: dict[str, object] | None = None
    for start_pos, first_gap_index in enumerate(long_gaps):
        for end_pos in range(start_pos, len(long_gaps)):
            last_gap_index = long_gaps[end_pos]
            if end_pos > start_pos:
                previous_gap_index = long_gaps[end_pos - 1]
                separator = (
                    interactions[last_gap_index].end
                    - interactions[previous_gap_index + 1].start
                )
                if separator > MAX_STITCHED_INTERACTION:
                    break
            interruption = sum(
                (interactions[index].end - interactions[index].start for index in range(first_gap_index + 1, last_gap_index + 1)),
                timedelta(),
            )
            rest_start = interactions[first_gap_index].end
            rest_end = interactions[last_gap_index + 1].start
            rest = rest_end - rest_start - interruption
            device_ids: set[str] = set()
            for item in interactions[first_gap_index:last_gap_index + 2]:
                device_ids.update(item.device_ids)
            candidate = {
                "estimated_start": _utc_text(rest_start),
                "estimated_end": _utc_text(rest_end),
                "finalized_at": _utc_text(rest_end),
                "interval_seconds": int((rest_end - rest_start).total_seconds()),
                "rest_seconds": int(rest.total_seconds()),
                "interruption_seconds": int(interruption.total_seconds()),
                "last_activity_at": _utc_text(rest_start),
                "first_activity_at": _utc_text(rest_end),
                "last_activity_device_ids": sorted(interactions[first_gap_index].end_device_ids),
                "first_activity_device_ids": sorted(interactions[last_gap_index + 1].start_device_ids),
                "last_activity_apps": [
                    {"device_id": device_id, "platform": platform, "app_name": app_name}
                    for device_id, platform, app_name in sorted(interactions[first_gap_index].end_apps)
                ],
                "first_activity_apps": [
                    {"device_id": device_id, "platform": platform, "app_name": app_name}
                    for device_id, platform, app_name in sorted(interactions[last_gap_index + 1].start_apps)
                ],
                "contributing_device_ids": sorted(device_ids),
            }
            if best is None or candidate["interval_seconds"] > best["interval_seconds"]:
                best = candidate
    return best


def derive_sleep_reference(
    connection: sqlite3.Connection,
    target_date: date,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if target_date > current.astimezone(HEALTH_TIMEZONE).date():
        raise ValueError("date must not be in the future")
    window_start, effective_end, full_end, is_current = _night_window(target_date, current)
    interactions, warnings = _load_interactions(connection, window_start, effective_end)
    candidate = _best_rest(interactions)
    result: dict[str, object] = {
        "status": ("estimating" if is_current else "insufficient_data") if candidate is None else "final",
        "window_start": _utc_text(window_start),
        "window_end": _utc_text(effective_end if is_current else full_end),
        "estimated_start": None,
        "estimated_end": None,
        "finalized_at": None,
        "interval_seconds": None,
        "rest_seconds": None,
        "interruption_seconds": None,
        "last_activity_at": None,
        "first_activity_at": None,
        "last_activity_device_ids": [],
        "first_activity_device_ids": [],
        "last_activity_apps": [],
        "first_activity_apps": [],
        "contributing_device_ids": [],
        "warnings": warnings,
    }
    if candidate is not None:
        result.update(candidate)
    return result
