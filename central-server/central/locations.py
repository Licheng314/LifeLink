"""Read-only location projections for the central dashboard API.

This ports the deterministic observation/segment derivation that previously
lived only in the PC dashboard, but reads from the central SQLite store so
every client (Web, AI, future mobile readers) gets the same location view.

Location storage contract:
- location.observation : immutable point observations from the device.
- location.sample/stay : mutable, possibly active segments; the latest row wins
  by event_id and may overlap the requested window even when occurred_at is
  earlier (an active stay that began yesterday is still "now").
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .activity_state import derive_activity_state

try:
    DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # pragma: no cover - defensive
    DISPLAY_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

ONLINE_WINDOW_SECONDS = 600
LOCATION_CLUSTER_RADIUS_METERS = 150
LOCATION_STAY_SECONDS = 15 * 60


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _payload(event_type: str, row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    # Legacy Android events wrapped their body under legacy_data.
    legacy = payload.get("legacy_data")
    if isinstance(legacy, dict):
        return legacy
    return payload


def _observed_at(event_type: str, occurred_at: str, payload: dict[str, Any]) -> datetime | None:
    if event_type == "location.observation":
        return (
            _parse_utc(payload.get("observed_at"))
            or _parse_utc(payload.get("location_time"))
            or _parse_utc(occurred_at)
        )
    return (
        _parse_utc(payload.get("latest_observed_at"))
        or _parse_utc(payload.get("observed_until"))
        or _parse_utc(occurred_at)
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _label(payload: dict[str, Any]) -> str:
    place = payload.get("place") if isinstance(payload.get("place"), dict) else {}
    for value in (place.get("display_label"), place.get("full_address")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts = [place.get(key) for key in ("city", "district", "road_or_poi")]
    text = " · ".join(str(v).strip() for v in parts if isinstance(v, str) and v.strip())
    return text or "未解析地址"


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}:{minutes:02d}"
    return f"{minutes} 分钟"


def _distance_meters(first: dict[str, Any], second: dict[str, Any]) -> float:
    lat1 = math.radians(float(first["latitude"]))
    lat2 = math.radians(float(second["latitude"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(second["longitude"]) - float(first["longitude"]))
    haversine = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(haversine)))


def _connection_status(last_seen_at: str | None, now: datetime) -> str:
    last_seen = _parse_utc(last_seen_at)
    if last_seen is None:
        return "disconnected"
    age = (now - last_seen).total_seconds()
    return "connected" if 0 <= age <= ONLINE_WINDOW_SECONDS else "disconnected"


def _derive_segments(observations: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        grouped[str(observation["device_key"])].append(observation)

    segments: list[dict[str, Any]] = []
    for device_observations in grouped.values():
        ordered = sorted(device_observations, key=lambda item: item["observed_at"])
        clusters: list[list[dict[str, Any]]] = []
        for observation in ordered:
            if not clusters or _distance_meters(clusters[-1][0], observation) > LOCATION_CLUSTER_RADIUS_METERS:
                clusters.append([observation])
            else:
                clusters[-1].append(observation)

        for cluster in clusters:
            first, latest = cluster[0], cluster[-1]
            started = _parse_utc(first["observed_at"])
            observed = _parse_utc(latest["observed_at"])
            duration = max(0, int((observed - started).total_seconds())) if started and observed else 0
            active = bool(observed and ((now or _utc_now()) - observed <= timedelta(minutes=12)))
            motion_trigger_count = sum(int(item.get("motion_trigger_count", 0) or 0) for item in cluster)
            segments.append({
                "event_id": f"derived:{first['event_id']}",
                "segment_identity": f"derived:{first['event_id']}",
                "device_key": first["device_key"],
                "device_id": first.get("device_id"),
                "device_name": first["device_name"],
                "status": latest["status"],
                "kind": "stay" if duration >= LOCATION_STAY_SECONDS else "sample",
                "is_active": active,
                "label": latest["label"],
                "latitude": sum(float(item["latitude"]) for item in cluster) / len(cluster),
                "longitude": sum(float(item["longitude"]) for item in cluster) / len(cluster),
                "accuracy_m": latest["accuracy_m"],
                "occurred_at": first["occurred_at"],
                "observed_at": latest["observed_at"],
                "duration_seconds": duration,
                "duration_label": _format_duration(duration),
                "received_at": max((str(item.get("received_at") or "") for item in cluster), default=""),
                "observation_count": len(cluster),
                "motion_trigger_count": motion_trigger_count,
                "source_observation_ids": [item["event_id"] for item in cluster],
                "derived_on_central": True,
            })
    return segments


def _dedupe_stays(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop derived stays that duplicate an authoritative client stay.

    A device reports both an explicit ``location.stay`` and ongoing
    ``location.observation`` samples; the central service derives a second stay
    from those samples.  When the two represent the same place, keep the client
    stay and drop the derived one so the evaluator does not emit duplicate
    ``location_stay_milestone`` events.
    """
    raw_stays = [
        segment for segment in segments
        if segment.get("kind") == "stay" and not segment.get("derived_on_central")
    ]
    if not raw_stays:
        return segments
    kept: list[dict[str, Any]] = []
    for segment in segments:
        if segment.get("kind") != "stay" or not segment.get("derived_on_central"):
            kept.append(segment)
            continue
        if _duplicates_any_stay(segment, raw_stays):
            continue
        kept.append(segment)
    return kept


def _duplicates_any_stay(derived: dict[str, Any], raw_stays: list[dict[str, Any]]) -> bool:
    for raw in raw_stays:
        if raw.get("device_key") != derived.get("device_key"):
            continue
        raw_lat, raw_lon = _number(raw.get("latitude")), _number(raw.get("longitude"))
        der_lat, der_lon = _number(derived.get("latitude")), _number(derived.get("longitude"))
        if raw_lat is not None and raw_lon is not None and der_lat is not None and der_lon is not None:
            if _distance_meters(raw, derived) < LOCATION_CLUSTER_RADIUS_METERS:
                return True
        elif raw.get("label") and raw.get("label") == derived.get("label"):
            return True
    return False


def locations_view(
    connection: sqlite3.Connection,
    start: datetime,
    end: datetime,
    local_device_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return observations and derived segments within [start, end)."""
    start_text, end_text = _utc_text(start), _utc_text(end)
    now = (now or _utc_now()).astimezone(timezone.utc)
    generated = _utc_text(now)

    devices: dict[str, dict[str, Any]] = {
        str(row["device_id"]): {
            "device_key": str(row["device_id"]),
            "device_id": str(row["device_id"]),
            "platform": str(row["platform"]),
            "display_name": str(row["effective_name"]),
            "is_local": str(row["device_id"]) == local_device_id,
            "status": _connection_status(str(row["last_seen_at"]), now),
            "last_synced_at": str(row["last_seen_at"]),
            "segment_count": 0,
            "observation_count": 0,
        }
        for row in connection.execute(
            """SELECT device_id, platform,
                      COALESCE(custom_name, display_name) AS effective_name,
                      last_seen_at
               FROM devices"""
        )
    }

    rows = connection.execute(
        """
        SELECT event_id, device_id, occurred_at, event_type, duration_seconds,
               payload_json, updated_at
        FROM events
        WHERE (event_type LIKE 'location.%' OR event_type = 'location.visit')
          AND occurred_at >= ? AND occurred_at < ?
        UNION ALL
        SELECT event_id, device_id, occurred_at, event_type, duration_seconds,
               payload_json, updated_at
        FROM events
        WHERE (event_type LIKE 'location.%' OR event_type = 'location.visit')
          AND occurred_at < ?
          AND (
              julianday(occurred_at) + COALESCE(duration_seconds, 0) / 86400.0 > julianday(?)
              OR json_extract(payload_json, '$.is_active') = 1
              OR json_extract(payload_json, '$.legacy_data.is_active') = 1
              OR (json_extract(payload_json, '$.observed_at') >= ? AND json_extract(payload_json, '$.observed_at') < ?)
              OR (json_extract(payload_json, '$.location_time') >= ? AND json_extract(payload_json, '$.location_time') < ?)
              OR (json_extract(payload_json, '$.latest_observed_at') >= ? AND json_extract(payload_json, '$.latest_observed_at') < ?)
              OR (json_extract(payload_json, '$.observed_until') >= ? AND json_extract(payload_json, '$.observed_until') < ?)
              OR (json_extract(payload_json, '$.legacy_data.latest_observed_at') >= ? AND json_extract(payload_json, '$.legacy_data.latest_observed_at') < ?)
              OR (json_extract(payload_json, '$.legacy_data.observed_until') >= ? AND json_extract(payload_json, '$.legacy_data.observed_until') < ?)
          )
        ORDER BY device_id, occurred_at, event_id
        """,
        (
            start_text, end_text, start_text, start_text,
            start_text, end_text, start_text, end_text,
            start_text, end_text, start_text, end_text,
            start_text, end_text, start_text, end_text,
        ),
    ).fetchall()

    observations: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    for row in rows:
        device_id = str(row["device_id"])
        device = devices.get(device_id)
        if device is None:
            continue
        event_type = str(row["event_type"])
        payload = _payload(event_type, row)
        occurred_at = str(row["occurred_at"])
        observed = _observed_at(event_type, occurred_at, payload)
        received_at = str(row["updated_at"])

        # Immutable observations are included when their observation time sits in
        # the window. Active mutable segments may have begun earlier yet still
        # represent "where the device is now", so include them by latest time.
        is_active = payload.get("is_active") is True
        in_window = observed is not None and start <= observed < end
        is_legacy_segment = event_type == "location.visit" and payload.get("kind") in {"sample", "stay"}
        if not in_window and not (is_active and observed is not None and observed < end):
            continue
        if event_type not in {"location.observation", "location.sample", "location.stay"} and not is_legacy_segment:
            continue

        if event_type == "location.observation" or payload.get("kind") == "observation":
            latitude = _number(payload.get("latitude"))
            longitude = _number(payload.get("longitude"))
            if latitude is None or longitude is None or observed is None:
                continue
            motion_window = payload.get("motion_window") if isinstance(payload.get("motion_window"), dict) else {}
            observations.append({
                "event_id": str(row["event_id"]),
                "device_key": device_id,
                "device_id": device_id,
                "device_name": device["display_name"],
                "status": _connection_status(received_at, now),
                "label": _label(payload),
                "latitude": latitude,
                "longitude": longitude,
                "accuracy_m": _number(payload.get("accuracy_m")),
                "occurred_at": occurred_at,
                "observed_at": _utc_text(observed),
                "received_at": received_at,
                "motion_window": motion_window,
                "motion_triggered": motion_window.get("motion_triggered") is True,
                "motion_trigger_count": int(motion_window.get("trigger_count", 0) or 0),
            })
            continue

        latitude = payload.get("current_latitude") if is_active else None
        longitude = payload.get("current_longitude") if is_active else None
        latitude = payload.get("latitude") if latitude is None else latitude
        longitude = payload.get("longitude") if longitude is None else longitude
        duration = int(row["duration_seconds"] or 0)
        segments.append({
            "event_id": str(row["event_id"]),
            "segment_identity": str(row["event_id"]),
            "device_key": device_id,
            "device_id": device_id,
            "device_name": device["display_name"],
            "status": _connection_status(received_at, now),
            "kind": "stay" if event_type == "location.stay" or payload.get("kind") == "stay" else "sample",
            "is_active": is_active,
            "label": _label(payload),
            "latitude": _number(latitude),
            "longitude": _number(longitude),
            "accuracy_m": payload.get("current_accuracy_m") if is_active else payload.get("accuracy_m"),
            "occurred_at": occurred_at,
            "observed_at": _utc_text(observed) if observed else occurred_at,
            "duration_seconds": duration,
            "duration_label": _format_duration(duration),
            "received_at": received_at,
        })

    derived_segments = _derive_segments(observations, now=now)
    segments.extend(derived_segments)
    segments = _dedupe_stays(segments)
    segments.sort(key=lambda item: item.get("observed_at") or "", reverse=True)
    observations.sort(key=lambda item: item.get("observed_at") or "", reverse=True)

    for segment in segments:
        key = str(segment["device_key"])
        current = devices.get(key)
        if current is None:
            continue
        current["segment_count"] += 1
        if (segment["received_at"] or "") > (current["last_synced_at"] or ""):
            current["last_synced_at"] = segment["received_at"]
            current["status"] = segment["status"]
    for observation in observations:
        key = str(observation["device_key"])
        current = devices.get(key)
        if current is None:
            continue
        current["observation_count"] += 1

    present_devices = [
        device for device in devices.values()
        if device["segment_count"] or device["observation_count"]
    ]
    stays = sorted(
        (segment for segment in segments if segment["kind"] == "stay"),
        key=lambda item: item["duration_seconds"],
        reverse=True,
    )[:3]

    activity_state = derive_activity_state(connection, start, end, now=now)
    return {
        "from": start_text,
        "to": end_text,
        "window": {"from": start_text, "to": end_text},
        "generated_at": generated,
        "timezone": str(DISPLAY_TIMEZONE),
        "devices": sorted(present_devices, key=lambda item: item["display_name"].casefold()),
        "observations": observations,
        "segments": segments,
        "latest_observation": observations[0] if observations else None,
        "latest": segments[0] if segments else None,
        "longest_stays": stays,
        # The evaluator must never turn an old longest stay into a new current
        # milestone.  These are the only segments still continuously supported
        # by fresh evidence at the requested clock.
        "current_stays": [
            item for item in segments
            if item["kind"] == "stay" and item.get("is_active")
            and (_parse_utc(item.get("observed_at")) is not None)
            and now - _parse_utc(item["observed_at"]) <= timedelta(minutes=15)
        ],
        "activity_state": activity_state,
        "ai_summary": _ai_summary(present_devices, observations, segments, stays, start, activity_state),
    }


def _ai_summary(
    devices: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    stays: list[dict[str, Any]],
    start: datetime,
    activity_state: dict[str, Any],
) -> str:
    date_str = start.astimezone(DISPLAY_TIMEZONE).date().isoformat()
    source_names = [device["display_name"] for device in devices]
    lines = [
        f"【位置轨迹 · {date_str}】",
        f"来源设备: {'、'.join(source_names) if source_names else '暂无'}",
        f"原始位置观察: {len(observations)} 条",
        f"位置段: {len(segments)} 个",
    ]
    intervals = activity_state.get("intervals") if isinstance(activity_state.get("intervals"), list) else []
    current = activity_state.get("current")
    if intervals:
        lines.extend(["", "活动状态（中央派生）："])
        if isinstance(current, dict):
            lines.append(f"当前：{current.get('state')}，最近证据 {current.get('end_at') or '未知'}")
        for interval in intervals[-12:]:
            lines.append(
                f"{interval.get('start_at')} → {interval.get('end_at')} | {interval.get('state')} | "
                f"{interval.get('steps', 0)} 步 | {interval.get('distance_m', 0)} 米"
            )
        lines.append("")
    if segments:
        latest = segments[0]
        coordinate = (
            f"{latest['latitude']:.4f}, {latest['longitude']:.4f}"
            if latest.get("latitude") is not None and latest.get("longitude") is not None
            else "未提供"
        )
        latest_time = _parse_utc(latest["observed_at"])
        lines.append(
            f"最近所在位置：{latest['label']} | 经纬度 {coordinate} | 更新时间点 "
            f"{latest_time.astimezone(DISPLAY_TIMEZONE).strftime('%H:%M') if latest_time else '--:--'} "
            f"| 共停留 {latest['duration_label']}"
        )
    else:
        lines.append("最近所在位置：暂无已同步位置")
    lines.extend(["", "今日停留时间最长位置（前三）："])
    if stays:
        for segment in stays:
            coordinate = (
                f"{segment['latitude']:.4f}, {segment['longitude']:.4f}"
                if segment.get("latitude") is not None and segment.get("longitude") is not None
                else "未提供"
            )
            observed = _parse_utc(segment["observed_at"])
            lines.append(
                f"{segment['label']} | 经纬度 {coordinate} | 更新时间点 "
                f"{observed.astimezone(DISPLAY_TIMEZONE).strftime('%H:%M') if observed else '--:--'} "
                f"| 共停留 {segment['duration_label']}"
            )
    else:
        lines.append("暂无已达到停留阈值的位置")
    return "\n".join(lines)
