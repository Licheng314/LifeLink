"""Read-only projections for the central dashboard API."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ai_summary import usage_ai_summary


try:
    DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    DISPLAY_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_READ_RANGE = timedelta(days=1)
CHROME_SWITCH_MIN_SECONDS = 2
CHROME_URL_RESUME_GRACE_SECONDS = 180


def parse_read_range(from_value: str | None, to_value: str | None) -> tuple[datetime, datetime]:
    start = _parse_utc(from_value)
    end = _parse_utc(to_value)
    if start is None or end is None:
        raise ValueError("from and to must be UTC ISO-8601 timestamps ending in Z")
    if end <= start:
        raise ValueError("to must be later than from")
    if end - start > MAX_READ_RANGE:
        raise ValueError("read range must not exceed 24 hours")
    return start, end


def devices_view(
    connection: sqlite3.Connection,
    start: datetime,
    end: datetime,
    local_device_id: str | None = None,
) -> dict[str, Any]:
    start_text, end_text = _utc_text(start), _utc_text(end)
    generated = datetime.now(timezone.utc)
    generated_text = _utc_text(generated)
    devices = {
        str(row["device_id"]): {
            "device_key": str(row["device_id"]),
            "device_id": str(row["device_id"]),
            "platform": str(row["platform"]),
            "display_name": str(row["display_name"]),
            "_retired": row["retired_at"] is not None,
            "is_local": str(row["device_id"]) == local_device_id,
            "status": _sync_status(str(row["last_seen_at"]), generated),
            "last_seen_at": str(row["last_seen_at"]),
            "last_received_at": str(row["last_seen_at"]),
            "event_count": 0,
            "batch_count": 0,
            "categories": {},
        }
        for row in connection.execute(
            """
            SELECT device_id, platform,
                   COALESCE(custom_name, display_name) AS display_name,
                   last_seen_at, retired_at
            FROM devices
            ORDER BY COALESCE(custom_name, display_name) COLLATE NOCASE, device_id
            """
        )
    }
    event_ids_by_device: defaultdict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        """
        SELECT event_id, device_id, event_type, occurred_at, duration_seconds
        FROM events
        WHERE occurred_at >= ? AND occurred_at < ?
        UNION ALL
        SELECT event_id, device_id, event_type, occurred_at, duration_seconds
        FROM events
        WHERE occurred_at < ?
          AND julianday(occurred_at) + COALESCE(duration_seconds, 0) / 86400.0 > julianday(?)
        """,
        (start_text, end_text, start_text, start_text),
    ):
        item = devices.get(str(row["device_id"]))
        occurred = _parse_utc(str(row["occurred_at"]))
        if item is None or occurred is None or not _event_overlaps(
            occurred,
            max(0, int(row["duration_seconds"] or 0)),
            start,
            end,
        ):
            continue
        item["event_count"] += 1
        event_type = str(row["event_type"])
        item["categories"][event_type] = item["categories"].get(event_type, 0) + 1
        event_ids_by_device[str(row["device_id"])].add(str(row["event_id"]))
    for row in connection.execute(
        "SELECT device_id, ack_json FROM batches"
    ):
        item = devices.get(str(row["device_id"]))
        relevant_ids = event_ids_by_device.get(str(row["device_id"]), set())
        if item is None or not relevant_ids:
            continue
        try:
            acknowledgement = json.loads(str(row["ack_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        results = acknowledgement.get("event_results") if isinstance(acknowledgement, dict) else None
        if isinstance(results, list) and any(
            isinstance(result, dict) and result.get("event_id") in relevant_ids
            for result in results
        ):
            item["batch_count"] += 1
    for item in devices.values():
        stats = {
            "event_count": item["event_count"],
            "batch_count": item["batch_count"],
            "categories": dict(item["categories"]),
        }
        item["window"] = stats
        item["today"] = {
            "event_count": stats["event_count"],
            "batch_count": stats["batch_count"],
            "categories": dict(stats["categories"]),
        }
    visible_devices = []
    for item in devices.values():
        if not item.pop("_retired") or item["event_count"] > 0:
            visible_devices.append(item)
    return {
        "from": start_text,
        "to": end_text,
        "window": {"from": start_text, "to": end_text},
        "generated_at": generated_text,
        "online_window_seconds": 600,
        "devices": visible_devices,
    }


def usage_view(
    connection: sqlite3.Connection,
    start: datetime,
    end: datetime,
    local_device_id: str | None = None,
    *,
    is_blacklisted_app: Callable[[str, str], bool] | None = None,
    is_blacklisted_site: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    summaries: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        """SELECT device_id, platform,
                  COALESCE(custom_name, display_name) AS display_name,
                  retired_at
           FROM devices"""
    ):
        device_id = str(row["device_id"])
        platform = str(row["platform"])
        summaries[device_id] = _empty_device_summary(
            device_id,
            platform,
            str(row["display_name"]),
            is_local=device_id == local_device_id,
            retired=row["retired_at"] is not None,
        )

    chrome_windows: defaultdict[str, list[tuple[datetime, int]]] = defaultdict(list)
    non_chrome_windows: defaultdict[str, list[tuple[datetime, int]]] = defaultdict(list)
    chrome_markers: defaultdict[str, list[tuple[datetime, str]]] = defaultdict(list)
    afk_intervals: defaultdict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    window_intervals: defaultdict[
        str, list[tuple[datetime, datetime, str, bool]]
    ] = defaultdict(list)

    marker_floor = start - timedelta(seconds=CHROME_URL_RESUME_GRACE_SECONDS)
    rows = connection.execute(
        """
        SELECT event_id, device_id, occurred_at, duration_seconds, event_type, payload_json
        FROM events
        WHERE event_type IN ('app.foreground', 'web.foreground', 'device.input_state')
          AND occurred_at >= ? AND occurred_at < ?
        UNION ALL
        SELECT event_id, device_id, occurred_at, duration_seconds, event_type, payload_json
        FROM events
        WHERE event_type IN ('app.foreground', 'web.foreground', 'device.input_state')
          AND occurred_at < ?
          AND julianday(occurred_at) + COALESCE(duration_seconds, 0) / 86400.0 > julianday(?)
        ORDER BY device_id, occurred_at, event_id
        """,
        (_utc_text(marker_floor), _utc_text(end), _utc_text(marker_floor), _utc_text(start)),
    )
    for row in rows:
        device_id = str(row["device_id"])
        summary = summaries.get(device_id)
        occurred = _parse_utc(str(row["occurred_at"]))
        if summary is None or occurred is None:
            continue
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(row["event_type"])
        if event_type == "web.foreground":
            domain = str(payload.get("domain") or "").lower().removeprefix("www.")
            if domain and marker_floor <= occurred < end:
                chrome_markers[device_id].append((occurred, domain))
            if start <= occurred < end:
                summary["web_events"] += 1
                summary["events"] += 1
            continue

        duration = max(0, int(row["duration_seconds"] or 0))
        clipped = _clip_interval(occurred, duration, start, end)
        if clipped is None:
            if duration != 0 or not start <= occurred < end:
                continue
            clipped = (occurred, occurred)
        clipped_start, clipped_end = clipped
        clipped_seconds = max(0, int((clipped_end - clipped_start).total_seconds()))
        summary["events"] += 1

        if event_type == "device.input_state":
            status = str(payload.get("status") or "")
            if status in {"afk", "locked"}:
                summary["afk_seconds"] += clipped_seconds
                afk_intervals[device_id].append((clipped_start, clipped_end))
            continue

        activitywatch = payload.get("activitywatch")
        activitywatch = activitywatch if isinstance(activitywatch, dict) else {}
        kind = str(activitywatch.get("kind") or "window")
        raw = activitywatch.get("data")
        raw = raw if isinstance(raw, dict) else {}

        if kind == "web" and marker_floor <= occurred < end:
            domain = urlparse(str(raw.get("url") or "")).netloc.lower().removeprefix("www.")
            if domain and _is_chrome_web_marker(activitywatch):
                chrome_markers[device_id].append((occurred, domain))

        if kind == "afk":
            summary["afk_seconds"] += clipped_seconds
            if summary["platform"] == "desktop" and raw.get("status") == "afk":
                afk_intervals[device_id].append((clipped_start, clipped_end))
            continue
        if kind == "web":
            summary["web_events"] += 1
            continue

        summary["window_events"] += 1
        app = payload.get("app")
        app = app if isinstance(app, dict) else {}
        app_name = str(app.get("display_name") or app.get("package_name") or "未识别应用")
        window_intervals[device_id].append(
            (clipped_start, clipped_end, app_name, _is_chrome_window(app, activitywatch))
        )

    for device_id, summary in summaries.items():
        retained_intervals: list[tuple[datetime, datetime]] = []
        exclusions = (
            _merge_intervals(afk_intervals[device_id])
            if summary["platform"] == "desktop"
            else []
        )
        for interval_start, interval_end, app_name, is_chrome in window_intervals[device_id]:
            pieces = _subtract_intervals(interval_start, interval_end, exclusions)
            for piece_start, piece_end in pieces:
                seconds = int((piece_end - piece_start).total_seconds())
                if seconds <= 0:
                    continue
                summary["apps"][app_name] = summary["apps"].get(app_name, 0) + seconds
                _add_named_hourly(summary["hourly_apps"], app_name, piece_start, seconds)
                retained_intervals.append((piece_start, piece_end))
                if summary["platform"] == "desktop":
                    target = chrome_windows if is_chrome else non_chrome_windows
                    target[device_id].append((piece_start, seconds))

        # Device use is the union of retained foreground intervals. This keeps
        # one device at no more than 60 minutes per clock hour even if watcher
        # revisions or adjacent windows overlap. ``hourly_online`` remains only
        # as a compatibility alias for older clients.
        for interval_start, interval_end in _merge_intervals(retained_intervals):
            seconds = int((interval_end - interval_start).total_seconds())
            _add_hourly(summary["hourly"], interval_start, seconds)
            _add_hourly(
                summary["hourly_online"],
                interval_start,
                seconds,
            )
        for domain, occurred, duration in _derive_chrome_domain_segments(
            chrome_windows[device_id],
            chrome_markers[device_id],
            non_chrome_windows[device_id],
        ):
            summary["sites"][domain] = summary["sites"].get(domain, 0) + duration
            _add_named_hourly(summary["hourly_sites"], domain, occurred, duration)

    visible = []
    for summary in summaries.values():
        retired = bool(summary.pop("_retired"))
        if not retired or int(summary["events"]) > 0:
            visible.append(summary)
    ordered = sorted(
        visible,
        key=lambda item: (str(item["display_name"]).casefold(), item["device_id"]),
    )
    total = _empty_total_summary()
    for summary in ordered:
        _merge_summary(total, summary)
    return {
        "date": start.astimezone(DISPLAY_TIMEZONE).date().isoformat(),
        "from": _utc_text(start),
        "to": _utc_text(end),
        "window": {"from": _utc_text(start), "to": _utc_text(end)},
        "generated_at": _utc_text(datetime.now(timezone.utc)),
        "timezone": str(DISPLAY_TIMEZONE),
        "day_start_hour": start.astimezone(DISPLAY_TIMEZONE).hour,
        "devices": ordered,
        "all": total,
        "ai_summary": usage_ai_summary(
            ordered, start, end,
            is_blacklisted_app=is_blacklisted_app or (lambda _a, _p: False),
            is_blacklisted_site=is_blacklisted_site or (lambda _a, _p: False),
        ),
    }


def _empty_device_summary(
    device_id: str,
    platform: str,
    display_name: str,
    *,
    is_local: bool,
    retired: bool = False,
) -> dict[str, Any]:
    return {
        "device_key": device_id,
        "device_id": device_id,
        "display_name": display_name,
        "platform": platform,
        "is_local": is_local,
        "_retired": retired,
        **_empty_total_summary(),
    }


def _empty_total_summary() -> dict[str, Any]:
    return {
        "events": 0,
        "window_events": 0,
        "web_events": 0,
        "afk_seconds": 0,
        "apps": {},
        "hourly": {},
        "hourly_apps": {},
        "hourly_online": {},
        "sites": {},
        "hourly_sites": {},
    }


def _merge_summary(total: dict[str, Any], item: dict[str, Any]) -> None:
    for field in ("events", "window_events", "web_events", "afk_seconds"):
        total[field] += int(item[field])
    for field in ("apps", "hourly", "hourly_online", "sites"):
        for key, seconds in item[field].items():
            total[field][key] = total[field].get(key, 0) + int(seconds)
    for field in ("hourly_apps", "hourly_sites"):
        for hour, values in item[field].items():
            destination = total[field].setdefault(hour, {})
            for key, seconds in values.items():
                destination[key] = destination.get(key, 0) + int(seconds)


def _parse_utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clip_interval(
    occurred: datetime,
    duration: int,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime] | None:
    interval_end = occurred + timedelta(seconds=duration)
    clipped_start = max(occurred, start)
    clipped_end = min(interval_end, end)
    return (clipped_start, clipped_end) if clipped_end > clipped_start else None


def _event_overlaps(
    occurred: datetime,
    duration: int,
    start: datetime,
    end: datetime,
) -> bool:
    if duration <= 0:
        return start <= occurred < end
    return occurred < end and occurred + timedelta(seconds=duration) > start


def _sync_status(last_seen_at: str, generated_at: datetime) -> str:
    last_seen = _parse_utc(last_seen_at)
    if last_seen is None:
        return "disconnected"
    age = (generated_at - last_seen).total_seconds()
    return "connected" if 0 <= age <= 600 else "disconnected"


def _add_hourly(target: dict[str, int], occurred: datetime, amount: int) -> None:
    cursor = occurred.astimezone(DISPLAY_TIMEZONE)
    remaining = max(0, int(amount))
    while remaining:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment = min(remaining, max(1, int((next_hour - cursor).total_seconds())))
        hour = str(cursor.hour)
        target[hour] = target.get(hour, 0) + segment
        remaining -= segment
        cursor = next_hour


def _add_named_hourly(
    target: dict[str, dict[str, int]],
    name: str,
    occurred: datetime,
    amount: int,
) -> None:
    cursor = occurred.astimezone(DISPLAY_TIMEZONE)
    remaining = max(0, int(amount))
    while remaining:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment = min(remaining, max(1, int((next_hour - cursor).total_seconds())))
        values = target.setdefault(str(cursor.hour), {})
        values[name] = values.get(name, 0) + segment
        remaining -= segment
        cursor = next_hour


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals, key=lambda item: item[0]):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(item[0], item[1]) for item in merged]


def _subtract_intervals(
    start: datetime,
    end: datetime,
    exclusions: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Remove explicit AFK overlaps from one foreground interval."""
    pieces: list[tuple[datetime, datetime]] = []
    cursor = start
    for excluded_start, excluded_end in exclusions:
        if excluded_end <= cursor:
            continue
        if excluded_start >= end:
            break
        if excluded_start > cursor:
            pieces.append((cursor, min(excluded_start, end)))
        cursor = max(cursor, excluded_end)
        if cursor >= end:
            break
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def _is_chrome_window(app: dict[str, Any], activitywatch: dict[str, Any]) -> bool:
    raw = activitywatch.get("data") if isinstance(activitywatch.get("data"), dict) else {}
    values = (
        app.get("package_name"), app.get("process_name"), app.get("display_name"), raw.get("app")
    )
    browser_names = ("chrome", "msedge", "firefox", "brave", "vivaldi", "opera")
    return any(name in str(value).lower() for value in values if value for name in browser_names)


def _is_chrome_web_marker(activitywatch: dict[str, Any]) -> bool:
    return "web-chrome" in str(activitywatch.get("bucket_id") or "").lower()


def _derive_chrome_domain_segments(
    chrome_windows: list[tuple[datetime, int]],
    web_markers: list[tuple[datetime, str]],
    non_chrome_windows: list[tuple[datetime, int]],
) -> list[tuple[str, datetime, int]]:
    segments: list[tuple[str, datetime, int]] = []
    markers = sorted(web_markers, key=lambda item: item[0])
    for intervals in _chrome_sessions(chrome_windows, non_chrome_windows):
        session_start, session_end = intervals[0][0], intervals[-1][1]
        candidates = [
            item for item in markers
            if session_start - timedelta(seconds=2) <= item[0] < session_end
        ]
        previous = max(
            (
                item for item in markers
                if session_start - timedelta(seconds=CHROME_URL_RESUME_GRACE_SECONDS)
                <= item[0] < session_start - timedelta(seconds=2)
            ),
            key=lambda item: item[0],
            default=None,
        )
        if previous is not None:
            candidates.insert(0, (session_start, previous[1]))
        marker_index = 0
        current_domain: str | None = None
        for interval_start, interval_end in intervals:
            while marker_index < len(candidates) and candidates[marker_index][0] <= interval_start:
                current_domain = candidates[marker_index][1]
                marker_index += 1
            cursor = interval_start
            while marker_index < len(candidates) and candidates[marker_index][0] < interval_end:
                marker_time, next_domain = candidates[marker_index]
                if current_domain and marker_time > cursor:
                    seconds = int((marker_time - cursor).total_seconds())
                    if seconds:
                        segments.append((current_domain, cursor, seconds))
                current_domain = next_domain
                cursor = max(cursor, marker_time)
                marker_index += 1
            if current_domain and interval_end > cursor:
                segments.append(
                    (current_domain, cursor, int((interval_end - cursor).total_seconds()))
                )
    return segments


def _chrome_sessions(
    chrome_windows: list[tuple[datetime, int]],
    non_chrome_windows: list[tuple[datetime, int]],
) -> list[list[list[datetime]]]:
    timeline = [
        (start, 0, start + timedelta(seconds=duration))
        for start, duration in non_chrome_windows
        if duration >= CHROME_SWITCH_MIN_SECONDS
    ] + [
        (start, 1, start + timedelta(seconds=duration))
        for start, duration in chrome_windows
        if duration > 0
    ]
    sessions: list[list[list[datetime]]] = []
    current: list[list[datetime]] | None = None
    for start, kind, end in sorted(timeline, key=lambda item: (item[0], item[1])):
        if kind == 0:
            current = None
            continue
        if current is None:
            current = []
            sessions.append(current)
        if current and start <= current[-1][1]:
            current[-1][1] = max(current[-1][1], end)
        else:
            current.append([start, end])
    return [session for session in sessions if session]
