"""Central, read-time activity-state derivation.

Raw steps, locations and foreground/AFK events remain authoritative.  This
module only creates a replaceable projection for one requested business-day
window; it never writes derived rows back to SQLite.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID


MINUTE = timedelta(minutes=1)
MAX_SAMPLE_GAP = timedelta(minutes=15)
MAX_LOCATION_GAP = timedelta(minutes=45)
MAX_LOCATION_ACCURACY_M = 200.0
CURRENT_STALE_AFTER = timedelta(minutes=15)
LOCATION_CONTEXT_MAX_GAP = timedelta(minutes=10)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A few old Android rows omitted Z although the contract always meant UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    legacy = value.get("legacy_data")
    return legacy if isinstance(legacy, dict) else value


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _location_label(payload: dict[str, Any]) -> str:
    place = payload.get("place") if isinstance(payload.get("place"), dict) else {}
    for value in (place.get("display_label"), place.get("full_address")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts = [place.get(key) for key in ("city", "district", "road_or_poi")]
    return " · ".join(str(value).strip() for value in parts if isinstance(value, str) and value.strip()) or "未解析地址"


def _distance(first: dict[str, Any], second: dict[str, Any]) -> float:
    lat1, lat2 = math.radians(first["latitude"]), math.radians(second["latitude"])
    dlat = lat2 - lat1
    dlon = math.radians(second["longitude"] - first["longitude"])
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(value)))


def _clip(first: datetime, last: datetime, start: datetime, end: datetime) -> tuple[datetime, datetime] | None:
    clipped = max(first, start), min(last, end)
    return clipped if clipped[1] > clipped[0] else None


def _merge(intervals: list[tuple[datetime, datetime]], grace_seconds: int = 0) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1] + timedelta(seconds=grace_seconds):
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _subtract(interval: tuple[datetime, datetime], exclusions: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    pieces = [interval]
    for cut_start, cut_end in exclusions:
        next_pieces = []
        for start, end in pieces:
            if cut_end <= start or cut_start >= end:
                next_pieces.append((start, end))
                continue
            if cut_start > start:
                next_pieces.append((start, min(cut_start, end)))
            if cut_end < end:
                next_pieces.append((max(cut_end, start), end))
        pieces = next_pieces
    return pieces


def _valid_session(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _device_candidates(connection: sqlite3.Connection, start: datetime, end: datetime) -> list[dict[str, Any]]:
    result = []
    for row in connection.execute(
        """SELECT device_id, COALESCE(custom_name, display_name) AS display_name
           FROM devices WHERE platform = 'android' AND retired_at IS NULL"""
    ):
        device_id = str(row["device_id"])
        steps = connection.execute(
            """SELECT COUNT(*) FROM events WHERE device_id=? AND event_type='health.steps_observation'
               AND occurred_at>=? AND occurred_at<?""", (device_id, _utc(start), _utc(end)),
        ).fetchone()[0]
        locations = connection.execute(
            """SELECT COUNT(*) FROM events WHERE device_id=? AND event_type='location.observation'
               AND occurred_at>=? AND occurred_at<?""", (device_id, _utc(start), _utc(end)),
        ).fetchone()[0]
        result.append({
            "device_id": device_id, "display_name": str(row["display_name"]),
            "step_sample_count": int(steps), "location_observation_count": int(locations),
            "evidence_count": int(steps) + int(locations),
        })
    return sorted(result, key=lambda item: (-item["evidence_count"], -item["step_sample_count"], item["device_id"]))


def _step_edges(connection: sqlite3.Connection, device_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    baseline = connection.execute(
        """SELECT occurred_at, payload_json FROM events
           WHERE device_id=? AND event_type='health.steps_observation' AND occurred_at<?
           ORDER BY occurred_at DESC LIMIT 1""", (device_id, _utc(start)),
    ).fetchone()
    rows = connection.execute(
        """SELECT occurred_at, payload_json FROM events
           WHERE device_id=? AND event_type='health.steps_observation' AND occurred_at>=? AND occurred_at<?
           ORDER BY occurred_at""", (device_id, _utc(start), _utc(end)),
    ).fetchall()
    rows = ([baseline] if baseline is not None else []) + list(rows)
    samples = []
    for row in rows:
        at = _parse_time(row["occurred_at"])
        payload = _payload(row)
        value, session = payload.get("counter_value"), payload.get("counter_session_id")
        if at is None or isinstance(value, bool) or not isinstance(value, int) or value < 0 or not _valid_session(session):
            continue
        samples.append((at, value, str(session)))
    edges = []
    for previous, current in zip(samples, samples[1:]):
        if current[2] != previous[2] or current[1] < previous[1] or current[0] <= previous[0]:
            continue
        clipped = _clip(previous[0], current[0], start, end)
        if clipped is None:
            continue
        edges.append({"start": clipped[0], "end": clipped[1], "steps": current[1] - previous[1], "reliable": current[0] - previous[0] <= MAX_SAMPLE_GAP})
    return edges


def _location_points(connection: sqlite3.Connection, device_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT occurred_at, payload_json FROM events
           WHERE device_id=? AND event_type='location.observation' AND occurred_at>=? AND occurred_at<?
           ORDER BY occurred_at""", (device_id, _utc(start - MAX_LOCATION_GAP), _utc(end)),
    ).fetchall()
    points = []
    for row in rows:
        payload = _payload(row)
        at = _parse_time(payload.get("observed_at")) or _parse_time(payload.get("location_time")) or _parse_time(row["occurred_at"])
        latitude, longitude, accuracy = _number(payload.get("latitude")), _number(payload.get("longitude")), _number(payload.get("accuracy_m"))
        if at is None or latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        if accuracy is not None and accuracy > MAX_LOCATION_ACCURACY_M:
            continue
        if start - MAX_LOCATION_GAP <= at < end:
            points.append({
                "at": at,
                "latitude": latitude,
                "longitude": longitude,
                "accuracy": max(0.0, accuracy or 30.0),
                "address": _location_label(payload),
            })
    # Remove a short-lived isolated teleport while retaining sustained fast travel.
    kept = []
    for index, point in enumerate(points):
        if 0 < index < len(points) - 1:
            before, after = points[index - 1], points[index + 1]
            far = _distance(before, point) > max(1000.0, 4 * (before["accuracy"] + point["accuracy"]))
            returns = _distance(before, after) <= max(300.0, 2 * (before["accuracy"] + after["accuracy"]))
            brief = after["at"] - before["at"] <= timedelta(minutes=15)
            if far and returns and brief:
                continue
        kept.append(point)
    return kept


def _representative_location(
    points: list[dict[str, Any]], interval_start: datetime, interval_end: datetime
) -> dict[str, Any] | None:
    midpoint = interval_start + (interval_end - interval_start) / 2
    inside = [point for point in points if interval_start <= point["at"] < interval_end]
    if inside:
        return min(inside, key=lambda point: abs(point["at"] - midpoint))
    if not points:
        return None
    nearest = min(
        points,
        key=lambda point: min(abs(point["at"] - interval_start), abs(point["at"] - interval_end)),
    )
    gap = min(abs(nearest["at"] - interval_start), abs(nearest["at"] - interval_end))
    return nearest if gap <= LOCATION_CONTEXT_MAX_GAP else None


def _use_intervals(connection: sqlite3.Connection, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    devices = {str(row["device_id"]): str(row["platform"]) for row in connection.execute("SELECT device_id, platform FROM devices")}
    foreground: dict[str, list[tuple[datetime, datetime]]] = {}
    afk: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in connection.execute(
        """SELECT device_id, occurred_at, duration_seconds, event_type, payload_json FROM events
           WHERE event_type IN ('app.foreground', 'device.input_state')
             AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at""",
        (_utc(start - timedelta(days=1)), _utc(end)),
    ):
        at = _parse_time(row["occurred_at"])
        if at is None:
            continue
        clipped = _clip(at, at + timedelta(seconds=max(0, int(row["duration_seconds"] or 0))), start, end)
        if clipped is None:
            continue
        payload = _payload(row)
        if str(row["event_type"]) == "device.input_state":
            if payload.get("status") in {"afk", "locked"}:
                afk.setdefault(str(row["device_id"]), []).append(clipped)
            continue
        aw = payload.get("activitywatch") if isinstance(payload.get("activitywatch"), dict) else {}
        raw = aw.get("data") if isinstance(aw.get("data"), dict) else {}
        kind = str(aw.get("kind") or "window")
        device_id = str(row["device_id"])
        if kind == "afk" and raw.get("status") == "afk":
            afk.setdefault(device_id, []).append(clipped)
        elif kind not in {"afk", "web"}:
            foreground.setdefault(device_id, []).append(clipped)
    retained = []
    for device_id, intervals in foreground.items():
        exclusions = _merge(afk.get(device_id, [])) if devices.get(device_id) == "desktop" else []
        for interval in intervals:
            retained.extend(_subtract(interval, exclusions))
    return _merge(retained, grace_seconds=75)


def derive_activity_state(connection: sqlite3.Connection, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
    candidates = _device_candidates(connection, start, end)
    configured_row = connection.execute("SELECT primary_health_device_id FROM shared_settings WHERE singleton_id=1").fetchone()
    configured = str(configured_row[0]) if configured_row and configured_row[0] is not None else None
    primary = next((item for item in candidates if item["device_id"] == configured), None)
    source = "configured" if primary else "automatic"
    if primary is None:
        primary = next((item for item in candidates if item["evidence_count"] > 0), None)
    if primary is None:
        source = "unavailable"
        return {"primary_device_id": None, "selection_source": source, "devices": candidates, "current": None, "updated_at": None, "intervals": []}

    minute_count = max(1, math.ceil((end - start).total_seconds() / 60))
    bins = [{"steps": 0.0, "step_seen": False, "gps": 0.0, "gps_seen": False, "speed": 0.0, "use": False} for _ in range(minute_count)]

    def distribute(first: datetime, last: datetime, field: str, total: float, seen: str) -> None:
        duration = (last - first).total_seconds()
        if duration <= 0:
            return
        for index in range(max(0, int((first - start).total_seconds() // 60)), min(minute_count, math.ceil((last - start).total_seconds() / 60))):
            bin_start, bin_end = start + index * MINUTE, min(end, start + (index + 1) * MINUTE)
            overlap = max(0.0, (min(last, bin_end) - max(first, bin_start)).total_seconds())
            if overlap:
                bins[index][field] += total * overlap / duration
                bins[index][seen] = True

    for edge in _step_edges(connection, primary["device_id"], start, end):
        if edge["reliable"]:
            distribute(edge["start"], edge["end"], "steps", float(edge["steps"]), "step_seen")
    points = _location_points(connection, primary["device_id"], start, end)
    for before, after in zip(points, points[1:]):
        gap = after["at"] - before["at"]
        clipped = _clip(before["at"], after["at"], start, end)
        if clipped is None or gap > MAX_LOCATION_GAP:
            continue
        distance = _distance(before, after)
        uncertainty = max(15.0, before["accuracy"] + after["accuracy"])
        reliable_distance = 0.0 if distance <= uncertainty else distance
        speed = distance / max(1.0, gap.total_seconds())
        distribute(clipped[0], clipped[1], "gps", reliable_distance, "gps_seen")
        for index in range(max(0, int((clipped[0] - start).total_seconds() // 60)), min(minute_count, math.ceil((clipped[1] - start).total_seconds() / 60))):
            bins[index]["speed"] = max(bins[index]["speed"], speed)
    for use_start, use_end in _use_intervals(connection, start, end):
        for index in range(max(0, int((use_start - start).total_seconds() // 60)), min(minute_count, math.ceil((use_end - start).total_seconds() / 60))):
            bins[index]["use"] = True

    # Activity is intentionally conservative: a minute may be classified only
    # when both the step counter and accepted location observations cover it.
    # Device use is supporting evidence for stationary, never a replacement
    # for either health source.
    dual_source = [bool(item["step_seen"] and item["gps_seen"]) for item in bins]
    labels: list[str | None] = []
    for item in bins:
        cadence, speed_kmh = item["steps"], item["speed"] * 3.6
        if not (item["step_seen"] and item["gps_seen"]):
            label = None
        elif speed_kmh >= 15:
            label = "transport"
        elif cadence >= 100:
            label = "running"
        elif cadence >= 4:
            label = "walking"
        elif item["use"] and item["gps"] < 80 and cadence < 4:
            label = "stationary"
        else:
            label = None
        labels.append(label)
    # Bridge only an uncertain minute that still has both sources. A missing
    # step/location minute is an evidence boundary and must remain blank.
    for index in range(1, len(labels) - 1):
        if dual_source[index] and labels[index] is None and labels[index - 1] == labels[index + 1]:
            labels[index] = labels[index - 1]
    for index in range(1, len(labels) - 1):
        if labels[index] not in {None, "transport"} and labels[index - 1] == labels[index + 1] != labels[index]:
            labels[index] = labels[index - 1]

    intervals = []
    index = 0
    while index < len(labels):
        state = labels[index]
        if state is None:
            index += 1
            continue
        last = index + 1
        while last < len(labels) and labels[last] == state:
            last += 1
        steps = int(round(sum(item["steps"] for item in bins[index:last])))
        gps = sum(item["gps"] for item in bins[index:last])
        distance = max(gps, steps * 0.7) if state in {"walking", "running"} else (gps if state == "transport" else 0.0)
        interval_start, interval_end = start + index * MINUTE, min(end, start + last * MINUTE)
        location = _representative_location(points, interval_start, interval_end)
        intervals.append({
            "start_at": _utc(interval_start), "end_at": _utc(interval_end), "state": state,
            "duration_seconds": int((interval_end - interval_start).total_seconds()),
            "steps": steps, "distance_m": round(distance, 1),
            "distance_source": "gps_or_steps_max" if state in {"walking", "running"} else ("gps" if state == "transport" and gps else "none"),
            "confidence": "high" if state == "transport" and gps > 300 or state in {"walking", "running"} and steps >= 20 else "medium",
            "is_current": False,
            "address": location["address"] if location else None,
            "latitude": round(location["latitude"], 6) if location else None,
            "longitude": round(location["longitude"], 6) if location else None,
        })
        index = last
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current = None
    if intervals:
        latest_end = _parse_time(intervals[-1]["end_at"])
        query_contains_now = start <= current_time < end
        if query_contains_now and latest_end and current_time - latest_end <= CURRENT_STALE_AFTER:
            intervals[-1]["is_current"] = True
            current = intervals[-1]
    updated_at = max([point["at"] for point in points] + [edge["end"] for edge in _step_edges(connection, primary["device_id"], start, end)], default=None)
    return {
        "primary_device_id": primary["device_id"], "selection_source": source, "devices": candidates,
        "current": current, "updated_at": _utc(updated_at) if updated_at else None, "intervals": intervals,
    }
