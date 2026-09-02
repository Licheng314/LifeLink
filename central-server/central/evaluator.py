"""Low-frequency timeline projections derived from central facts.

The evaluator runs after a successful ingest.  Raw events remain authoritative;
every timeline projection is append-only and protected by a stable dedupe key.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any

from .read_model import usage_view
from .locations import locations_view
from .health_info import build_health_info
from .report_text import generate_evening_report, generate_morning_report, generate_periodic_report


logger = logging.getLogger(__name__)
UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
CUSTOM_EVENT_TYPES = {
    "application.started": ("system", "low"),
    "sedentary.reminder_triggered": ("device", "normal"),
}


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError:
        return None


def _business_date(now_utc: datetime, day_start_hour: int) -> date:
    local = now_utc.astimezone(SHANGHAI) - timedelta(hours=day_start_hour)
    return local.date()


def _business_day_window(business_date: date, day_start_hour: int) -> tuple[datetime, datetime]:
    start = datetime.combine(
        business_date, clock_time(day_start_hour), tzinfo=SHANGHAI,
    ).astimezone(UTC)
    return start, start + timedelta(days=1)


def _milestone_key(trigger_id: str, business_date: date, generation: str, index: int) -> str:
    """Return a retry-stable key scoped to one effective trigger configuration."""
    return f"milestone|{trigger_id}|{business_date.isoformat()}|{generation}|{index}"


def _trigger_effective_at(trigger: dict[str, Any], start: datetime, latest: datetime) -> datetime:
    """Clamp the configuration baseline to the current business-day evaluation window."""
    configured_at = _parse_utc(trigger.get("updated_at")) or _parse_utc(trigger.get("created_at"))
    if configured_at is None:
        return start
    return min(max(configured_at, start), latest)


def _trigger_generation(trigger: dict[str, Any]) -> str:
    """Use the persisted configuration timestamp so edits cannot collide with old keys."""
    return str(trigger.get("updated_at") or trigger.get("created_at") or "unknown")


def _latest_index(total_seconds: int, interval_minutes: int) -> int:
    return total_seconds // (interval_minutes * 60)


def _duration_text(minutes: int) -> str:
    """Render user-facing durations without leaving large values as raw minutes."""
    hours, remainder = divmod(max(0, int(minutes)), 60)
    if hours and remainder:
        return f"{hours}小时{remainder}分钟"
    if hours:
        return f"{hours}小时"
    return f"{remainder}分钟"


def _wish_trigger_is_effective(
    connection: Any, trigger: dict[str, Any], now_utc: datetime,
) -> bool:
    """A linked wish stops producing reminders immediately after its fixed days."""
    wish_id = trigger.get("wish_id")
    if not wish_id:
        return True
    wish = connection.execute(
        "SELECT status, starts_on, ends_on, day_start_hour FROM wishes WHERE wish_id=?",
        (wish_id,),
    ).fetchone()
    if wish is None or wish["status"] != "active":
        return False
    current_day = _business_date(now_utc, int(wish["day_start_hour"]))
    return date.fromisoformat(str(wish["starts_on"])) <= current_day <= date.fromisoformat(str(wish["ends_on"]))


def _insert_timeline(
    connection: Any,
    *,
    occurred_at: datetime,
    event_key: str,
    category: str,
    importance: str,
    title: str,
    detail: str,
    dedupe_key: str,
    source_kind: str = "central",
    source_device_id: str | None = None,
    wish_id: str | None = None,
    trigger_id: str | None = None,
    subject: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    statistics_window: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
) -> bool:
    now_text = _utc_text(datetime.now(UTC))
    cursor = connection.execute(
        """INSERT OR IGNORE INTO timeline_events(
               timeline_event_id, occurred_at, created_at, event_key, category,
               importance, title, detail, source_kind, source_device_id,
               wish_id, trigger_id, subject_json, evidence_json, statistics_window_json, delivery_json, dedupe_key
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()), _utc_text(occurred_at), now_text, event_key,
            category, importance, title, detail, source_kind, source_device_id,
            wish_id, trigger_id,
            json.dumps(subject or {}, ensure_ascii=False, sort_keys=True), json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            json.dumps(statistics_window, ensure_ascii=False, sort_keys=True) if statistics_window else None,
            json.dumps(delivery, ensure_ascii=False, sort_keys=True) if delivery else None,
            dedupe_key,
        ),
    )
    return cursor.rowcount == 1


def _app_matches(name: str, patterns: list[str]) -> bool:
    lowered = name.casefold()
    return any(pattern.casefold() in lowered for pattern in patterns)


def _domain_matches(domain: str, patterns: list[str]) -> bool:
    lowered = domain.casefold().removeprefix("www.").rstrip(".")
    return any(
        lowered == pattern.casefold() or lowered.endswith("." + pattern.casefold())
        for pattern in patterns
    )


def _usage_snapshot(connection: Any, start: datetime, end: datetime) -> dict[str, Any]:
    return usage_view(
        connection, start, end,
        is_blacklisted_app=lambda _name, _platform: False,
        is_blacklisted_site=lambda _domain, _platform: False,
    )


def _usage_evidence(snapshot: dict[str, Any], patterns_by_scope: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Persist the exact non-zero per-device app/site state behind a milestone."""
    devices = []
    for device in snapshot.get("devices", []):
        platform = str(device.get("platform") or "")
        scope = "android" if platform == "android" else "pc"
        apps = []
        for name, seconds in (device.get("apps") or {}).items():
            if int(seconds) > 0:
                black = _app_matches(str(name), (patterns_by_scope or {}).get(scope, []))
                apps.append({"name": str(name), "seconds": int(seconds), "blacklisted": black})
        sites = []
        for name, seconds in (device.get("sites") or {}).items():
            if int(seconds) > 0:
                black = _domain_matches(str(name), (patterns_by_scope or {}).get("web", []))
                sites.append({"name": str(name), "seconds": int(seconds), "blacklisted": black})
        if apps or sites:
            devices.append({"device_id": device.get("device_id"), "display_name": device.get("display_name"), "platform": platform, "apps": sorted(apps, key=lambda x: -x["seconds"]), "sites": sorted(sites, key=lambda x: -x["seconds"])})
    return {"devices": devices}


def _snapshot_detail(snapshot: dict[str, Any], patterns_by_scope: dict[str, list[str]] | None = None) -> str:
    """Blacklist top-5 detail: types not devices, zero usage omitted."""
    patterns = patterns_by_scope or {"pc": [], "android": [], "web": []}
    top = _blacklist_top_five(snapshot, patterns)
    if not top:
        return ""
    return "用量最高：" + _format_top_items(top) + "。"


def _kind_label(platform: str, kind: str) -> str:
    """Label an app/site by platform type, independent of the source device."""
    if kind == "site":
        return "网站"
    if platform == "android":
        return "手机APP"
    return "电脑应用"


def _device_top_five(device: dict[str, Any]) -> list[dict[str, Any]]:
    platform = str(device.get("platform") or "")
    entries = [
        {"name": str(name), "seconds": int(seconds), "kind": "app", "platform": platform, "blacklisted": False}
        for name, seconds in (device.get("apps") or {}).items() if int(seconds) > 0
    ] + [
        {"name": str(name), "seconds": int(seconds), "kind": "site", "platform": platform, "blacklisted": False}
        for name, seconds in (device.get("sites") or {}).items() if int(seconds) > 0
    ]
    return sorted(entries, key=lambda item: (-item["seconds"], item["name"].casefold()))[:5]


def _blacklist_top_five(usage: dict[str, Any], patterns_by_scope: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Top-5 blacklisted apps/sites, merged to the matching rule and labelled by
    type (网站/手机APP/电脑应用), not by device. Subdomains such as
    ``space.bilibili.com`` fold into their parent rule ``bilibili.com``."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for device in usage.get("devices", []):
        platform = str(device.get("platform") or "")
        scope = "android" if platform == "android" else "pc"
        for name, seconds in (device.get("apps") or {}).items():
            if int(seconds) <= 0:
                continue
            pattern = next((p for p in patterns_by_scope.get(scope, []) if _app_matches(str(name), [p])), None)
            if pattern is None:
                continue
            key = ("app", pattern)
            entry = merged.setdefault(key, {"name": pattern, "seconds": 0, "kind": "app", "platform": platform})
            entry["seconds"] += int(seconds)
        for name, seconds in (device.get("sites") or {}).items():
            if int(seconds) <= 0:
                continue
            pattern = next((p for p in patterns_by_scope.get("web", []) if _domain_matches(str(name), [p])), None)
            if pattern is None:
                continue
            key = ("site", pattern)
            entry = merged.setdefault(key, {"name": pattern, "seconds": 0, "kind": "site", "platform": platform})
            entry["seconds"] += int(seconds)
    entries = [e for e in merged.values() if int(e["seconds"]) >= 60]
    return sorted(entries, key=lambda item: (-item["seconds"], str(item["name"]).casefold()))[:5]


def _format_top_items(items: list[dict[str, Any]]) -> str:
    return "，".join(
        f"{_kind_label(str(item.get('platform') or ''), str(item.get('kind') or 'app'))} {item['name']} {_duration_text(item['seconds'] // 60)}"
        for item in items
    )


def _device_usage_detail(device: dict[str, Any], threshold_minutes: int) -> str:
    detail = f"本业务日设备使用时长已达到 {_duration_text(threshold_minutes)}。"
    top = _device_top_five(device)
    if top:
        detail += "\n用量最高：" + _format_top_items(top) + "。"
    return detail


def _blacklist_total(
    snapshot: dict[str, Any], scope: str, patterns_by_scope: dict[str, list[str]],
) -> int:
    total_seconds = 0
    for device in snapshot["devices"]:
        platform = str(device["platform"])
        if scope in {"pc", "all"} and platform == "desktop":
            patterns = patterns_by_scope.get("pc", [])
            total_seconds += sum(int(seconds) for name, seconds in device["apps"].items() if _app_matches(name, patterns))
        if scope in {"android", "all"} and platform == "android":
            patterns = patterns_by_scope.get("android", [])
            total_seconds += sum(int(seconds) for name, seconds in device["apps"].items() if _app_matches(name, patterns))
        if scope in {"web", "all"}:
            patterns = patterns_by_scope.get("web", [])
            total_seconds += sum(int(seconds) for name, seconds in device["sites"].items() if _domain_matches(name, patterns))
    return total_seconds


def _evaluate_blacklist(
    connection: Any,
    trigger: dict[str, Any],
    business_date: date,
    start: datetime,
    end: datetime,
    now_utc: datetime,
) -> int:
    scope = str(trigger["parameters"].get("platform_scope") or "")
    requested_scopes = ("pc", "android", "web") if scope == "all" else (scope,)
    patterns_by_scope: dict[str, list[str]] = {}
    for requested_scope in requested_scopes:
        rule_type = "domain" if requested_scope == "web" else "app"
        rules = connection.execute(
            """SELECT normalized_pattern FROM blacklist_rules
               WHERE enabled = 1 AND rule_type = ? AND platform_scope = ?""",
            (rule_type, requested_scope),
        ).fetchall()
        patterns_by_scope[requested_scope] = [str(row[0]) for row in rules if row[0]]
    if not any(patterns_by_scope.values()):
        return 0

    latest = min(now_utc, end)
    effective_at = _trigger_effective_at(trigger, start, latest)
    snapshot = _usage_snapshot(connection, start, latest)
    total_seconds = 0
    for device in snapshot["devices"]:
        platform = str(device["platform"])
        if scope in {"pc", "all"} and platform == "desktop":
            patterns = patterns_by_scope.get("pc", [])
            total_seconds += sum(
                int(seconds) for name, seconds in device["apps"].items()
                if _app_matches(name, patterns)
            )
        if scope in {"android", "all"} and platform == "android":
            patterns = patterns_by_scope.get("android", [])
            total_seconds += sum(
                int(seconds) for name, seconds in device["apps"].items()
                if _app_matches(name, patterns)
            )
        if scope in {"web", "all"}:
            patterns = patterns_by_scope.get("web", [])
            total_seconds += sum(
                int(seconds) for name, seconds in device["sites"].items()
                if _domain_matches(name, patterns)
            )

    baseline_seconds = _blacklist_total(
        _usage_snapshot(connection, start, effective_at), scope, patterns_by_scope,
    )
    interval = int(trigger["interval_minutes"])
    latest_index = _latest_index(total_seconds, interval)
    if latest_index <= _latest_index(baseline_seconds, interval):
        return 0
    count = 0
    for index in range(latest_index, latest_index + 1):
        minutes = index * interval
        wish = connection.execute("SELECT text FROM wishes WHERE wish_id=?", (trigger["wish_id"],)).fetchone() if trigger["wish_id"] else None
        wish_title = "心愿提醒·黑名单" if wish else "黑名单用量"
        duration = _duration_text(minutes)
        wish_detail = f"今天全平台黑名单用量已达到 {duration}，关联心愿「{wish['text']}」。" if wish else f"本业务日 {scope} 黑名单用量已达到 {duration}"
        if _insert_timeline(
            connection,
            occurred_at=now_utc,
            event_key="blacklist_usage_milestone",
            category="trigger",
            importance="high" if wish else "normal",
            title=wish_title, detail=wish_detail,
            dedupe_key=_milestone_key(trigger["trigger_id"], business_date, _trigger_generation(trigger), index),
            wish_id=trigger["wish_id"], trigger_id=trigger["trigger_id"],
            subject={"platform_scope": scope, "milestone_minutes": minutes},
            evidence={"total_seconds": total_seconds},
        ):
            count += 1
    return count


def _evaluate_device(
    connection: Any,
    trigger: dict[str, Any],
    business_date: date,
    start: datetime,
    end: datetime,
    now_utc: datetime,
) -> int:
    device_id = str(trigger["parameters"].get("device_id") or "")
    latest = min(now_utc, end)
    effective_at = _trigger_effective_at(trigger, start, latest)
    snapshot = _usage_snapshot(connection, start, latest)
    device = next((item for item in snapshot["devices"] if item["device_id"] == device_id), None)
    if device is None:
        return 0
    total_seconds = sum(int(seconds) for seconds in device["hourly"].values())
    baseline_snapshot = _usage_snapshot(connection, start, effective_at)
    baseline_device = next(
        (item for item in baseline_snapshot["devices"] if item["device_id"] == device_id), None,
    )
    baseline_seconds = sum(
        int(seconds) for seconds in (baseline_device or {"hourly": {}})["hourly"].values()
    )
    interval = int(trigger["interval_minutes"])
    latest_index = _latest_index(total_seconds, interval)
    if latest_index <= _latest_index(baseline_seconds, interval):
        return 0
    count = 0
    for index in range(latest_index, latest_index + 1):
        minutes = index * interval
        wish = connection.execute("SELECT text FROM wishes WHERE wish_id=?", (trigger["wish_id"],)).fetchone() if trigger["wish_id"] else None
        if _insert_timeline(
            connection,
            occurred_at=now_utc,
            event_key="device_usage_milestone",
            category="trigger", importance="high" if wish else "normal",
            title="心愿提醒·设备使用" if wish else f"设备使用·{device['display_name']}",
            detail=f"设备「{device['display_name']}」本业务日使用时长已达到 {_duration_text(minutes)}，关联心愿「{wish['text']}」。" if wish else _device_usage_detail(device, minutes),
            dedupe_key=_milestone_key(trigger["trigger_id"], business_date, _trigger_generation(trigger), index),
            wish_id=trigger["wish_id"], trigger_id=trigger["trigger_id"],
            subject={"device_id": device_id, "milestone_minutes": minutes},
            evidence={"total_seconds": total_seconds},
        ):
            count += 1
    return count


def _late_start(business_date: date, day_start_hour: int, value: str) -> datetime:
    hour, minute = (int(part) for part in value.split(":"))
    local_date = business_date + (timedelta(days=1) if hour < day_start_hour else timedelta())
    return datetime.combine(local_date, clock_time(hour, minute), tzinfo=SHANGHAI).astimezone(UTC)


def _online_devices_at(connection: Any, device_id: str, instant: datetime) -> list[str]:
    """Devices with a fact or heartbeat in the checkpoint's preceding 15 minutes.

    A later ``last_seen_at`` is deliberately not allowed to prove an earlier
    checkpoint: the only device-row heartbeat accepted is one timestamped in
    that checkpoint window.  Foreground facts may prove the point while their
    interval is still running.
    """
    lower = instant - timedelta(minutes=15)
    allowed = "" if device_id == "all" else "AND e.device_id = ?"
    rows = connection.execute(
        f"""SELECT e.device_id, e.occurred_at, e.updated_at, e.duration_seconds
            FROM events e JOIN devices d ON d.device_id=e.device_id
            WHERE d.retired_at IS NULL {allowed}""", ((device_id,) if device_id != "all" else ()),
    ).fetchall()
    online = set()
    for row in rows:
        occurred, updated = _parse_utc(row["occurred_at"]), _parse_utc(row["updated_at"])
        duration = int(row["duration_seconds"] or 0)
        if ((occurred is not None and lower <= occurred <= instant)
                or (updated is not None and lower <= updated <= instant)
                or (occurred is not None and duration > 0 and occurred <= instant < occurred + timedelta(seconds=duration))):
            online.add(str(row["device_id"]))
    heartbeat_filter = "" if device_id == "all" else "AND device_id = ?"
    for row in connection.execute(f"SELECT device_id, last_seen_at FROM devices WHERE retired_at IS NULL {heartbeat_filter}", ((device_id,) if device_id != "all" else ())):
        last_seen = _parse_utc(row["last_seen_at"])
        if last_seen is not None and lower <= last_seen <= instant:
            online.add(str(row["device_id"]))
    return sorted(online)


def _evaluate_late(
    connection: Any,
    trigger: dict[str, Any],
    business_date: date,
    start: datetime,
    end: datetime,
    now_utc: datetime,
    day_start_hour: int,
) -> int:
    device_id = str(trigger["parameters"].get("device_id") or "")
    start_text = str(trigger["parameters"].get("start_local_time") or "")
    late_start = _late_start(business_date, day_start_hour, start_text)
    interval = int(trigger["interval_minutes"])
    latest = min(now_utc, end)
    effective_at = _trigger_effective_at(trigger, start, latest)
    index = int((latest - late_start).total_seconds() // (interval * 60))
    if index < 1:
        return 0
    # On recovery jump directly to the latest due slot.  Never backfill a
    # configuration-era slot that had already passed when the trigger changed.
    milestone = late_start + timedelta(minutes=index * interval)
    if milestone < effective_at:
        return 0
    count = 0
    active_device_ids = _online_devices_at(connection, device_id, milestone)
    if active_device_ids:
            local_label = milestone.astimezone(SHANGHAI).strftime("%H:%M")
            device_label = "仍有设备在使用" if device_id == "all" else "仍在使用"
            wish = connection.execute("SELECT text FROM wishes WHERE wish_id=?", (trigger["wish_id"],)).fetchone() if trigger["wish_id"] else None
            if _insert_timeline(
                connection,
                occurred_at=milestone,
                event_key="late_usage_milestone",
                category="trigger", importance="high" if wish else "normal",
                title="心愿提醒·晚睡" if wish else f"晚睡提醒：{local_label} {device_label}",
                detail=f"进入睡觉时间后，已达到 {_duration_text(index * interval)}，仍有设备在线，关联心愿「{wish['text']}」。" if wish else f"设备在 {start_text} 后第 {index} 个周期时刻仍有前台使用活动",
                dedupe_key=_milestone_key(trigger["trigger_id"], business_date, _trigger_generation(trigger), index),
                wish_id=trigger["wish_id"], trigger_id=trigger["trigger_id"],
                subject={"device_id": device_id, "milestone_index": index},
                evidence={"milestone_at": _utc_text(milestone), "active_device_ids": active_device_ids},
            ):
                count += 1
    return count


def _device_effective_names(connection: Any) -> dict[str, str]:
    rows = connection.execute(
        "SELECT device_id, COALESCE(custom_name, display_name) AS effective_name FROM devices"
    ).fetchall()
    return {str(row["device_id"]): str(row["effective_name"]) for row in rows}


def _project_custom_events(connection: Any) -> int:
    count = 0
    device_names = _device_effective_names(connection)
    rows = connection.execute(
        """SELECT event_id, device_id, occurred_at, payload_json
           FROM events WHERE event_type = 'custom.event' ORDER BY occurred_at, event_id"""
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        event_key = payload.get("event_key") if isinstance(payload, dict) else None
        if event_key not in CUSTOM_EVENT_TYPES:
            continue
        occurred_at = _parse_utc(row["occurred_at"])
        if occurred_at is None:
            continue
        category, importance = CUSTOM_EVENT_TYPES[event_key]
        title = str(payload.get("title") or event_key)[:120]
        detail = str(payload.get("detail") or "")[:500]
        source_device_id = row["device_id"]
        device_name = device_names.get(str(source_device_id))
        if device_name and event_key == "sedentary.reminder_triggered":
            # Distinguish which device triggered the sedentary reminder.
            title = f"{title}·{device_name}"
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if event_key == "application.started":
            machine_name = device_name or "未知设备"
            platform_name = str(metadata.get("platform") or "Windows")[:40]
            title = f"Life Link 已启动 · {machine_name}"[:120]
            detail = (
                f"Life Link PC 客户端已在 {machine_name}（{platform_name}）启动，"
                "同步服务、状态窗口与系统托盘已就绪。"
            )[:500]
        if _insert_timeline(
            connection,
            occurred_at=occurred_at,
            event_key=event_key,
            category=category,
            importance=importance,
            title=title,
            detail=detail,
            dedupe_key=f"custom.event|{row['event_id']}",
            source_kind="device", source_device_id=source_device_id,
            evidence={
                "event_id": row["event_id"],
                **(
                    {"platform": str(metadata.get("platform") or "Windows")[:40]}
                    if event_key == "application.started"
                    else {}
                ),
            },
        ):
            count += 1
    return count


def evaluate_all_milestones(
    connection: Any,
    store: Any,
    *,
    now: datetime | None = None,
) -> None:
    """Project supported custom events and evaluate enabled current-day triggers."""
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    settings = store.get_shared_settings()
    day_start_hour = int(settings["day_start_hour"])
    business_date = _business_date(now_utc, day_start_hour)
    start, end = _business_day_window(business_date, day_start_hour)

    connection.execute("BEGIN IMMEDIATE")
    try:
        _project_custom_events(connection)
        triggers = connection.execute(
            "SELECT * FROM event_triggers WHERE enabled = 1"
        ).fetchall()
        for row in triggers:
            trigger = dict(row)
            trigger["parameters"] = json.loads(trigger.pop("parameters_json"))
            if not _wish_trigger_is_effective(connection, trigger, now_utc):
                continue
            kind = trigger["trigger_type"]
            if kind == "blacklist_usage_milestone":
                inserted = _evaluate_blacklist(connection, trigger, business_date, start, end, now_utc)
            elif kind == "device_usage_milestone":
                inserted = _evaluate_device(connection, trigger, business_date, start, end, now_utc)
            elif kind == "late_usage_milestone":
                inserted = _evaluate_late(
                    connection, trigger, business_date, start, end, now_utc, day_start_hour,
                )
            elif kind == "scheduled_reminder":
                inserted = _evaluate_scheduled_reminder(connection, trigger, now_utc)
            else:
                logger.warning("evaluator: unsupported trigger type %s", kind)
                continue
            if inserted:
                connection.execute(
                    "UPDATE event_triggers SET last_triggered_at = ? WHERE trigger_id = ?",
                    (_utc_text(now_utc), trigger["trigger_id"]),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _evaluate_scheduled_reminder(connection: Any, trigger: dict[str, Any], now_utc: datetime) -> int:
    """One reminder per fixed wish day; miss tolerance is deliberately 15 minutes."""
    if not trigger.get("wish_id"):
        return 0
    if not _wish_trigger_is_effective(connection, trigger, now_utc):
        return 0
    wish = connection.execute("SELECT * FROM wishes WHERE wish_id=? AND status='active'", (trigger["wish_id"],)).fetchone()
    if wish is None:
        return 0
    hour, minute = map(int, trigger["parameters"]["reminder_local_time"].split(":"))
    day_start_hour = int(wish["day_start_hour"])
    count = 0
    for row in connection.execute("SELECT business_date FROM wish_days WHERE wish_id=?", (trigger["wish_id"],)).fetchall():
        reminder_date = date.fromisoformat(row["business_date"]) + (timedelta(days=1) if hour < day_start_hour else timedelta())
        target = datetime.combine(reminder_date, clock_time(hour, minute), SHANGHAI).astimezone(UTC)
        age = now_utc - target
        if age < timedelta(0) or age > timedelta(minutes=15):
            continue
        key = f"wish:scheduled:{trigger['trigger_id']}:{row['business_date']}:{trigger['parameters']['reminder_local_time']}"
        if _insert_timeline(connection, occurred_at=target, event_key="wish.scheduled_reminder", category="trigger", importance="high", title=f"心愿提醒·{wish['text']}", detail=f"心愿「{wish['text']}」的定时提醒已到达。", dedupe_key=key, wish_id=trigger["wish_id"], trigger_id=trigger["trigger_id"], subject={"business_date":row["business_date"]}, evidence={"reminder_local_time":trigger["parameters"]["reminder_local_time"]}):
            count += 1
    return count


def evaluate_scheduled_system(connection: Any, store: Any, *, now: datetime | None = None) -> None:
    """Minute-loop work. Stable keys make concurrent calls/restarts harmless."""
    now_utc = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    settings = store.get_shared_settings(); day_hour = int(settings["day_start_hour"])
    business = _business_date(now_utc, day_hour); start, end = _business_day_window(business, day_hour)
    latest = min(now_utc, end); window = {"business_date":business.isoformat(), "from":_utc_text(start), "to":_utc_text(latest)}
    connection.execute("BEGIN IMMEDIATE")
    try:
        # User reminders share this same loop and cannot backfill old slots.
        for row in connection.execute("SELECT * FROM event_triggers WHERE enabled=1 AND trigger_type='scheduled_reminder'").fetchall():
            trigger = dict(row); trigger["parameters"] = json.loads(trigger.pop("parameters_json")); _evaluate_scheduled_reminder(connection, trigger, now_utc)
        usage = _usage_snapshot(connection, start, latest)
        for device in usage.get("devices", []):
            total = sum(int(v) for v in device.get("hourly", {}).values())
            checkpoint = total // 60 // 60 * 60
            if checkpoint < 60:
                continue
            top_five = _device_top_five(device)
            _insert_timeline(
                connection, occurred_at=now_utc,
                event_key="system.device_usage_milestone", category="device", importance="normal",
                title=f"设备使用·{device['display_name']}",
                detail=_device_usage_detail(device, checkpoint),
                dedupe_key=f"system:device_usage:{business}:{device['device_id']}:{checkpoint}",
                subject={"device_id": device["device_id"], "milestone_minutes": checkpoint},
                evidence={"rule":"device_usage_hourly","threshold_minutes":checkpoint,"business_date":business.isoformat(),"aggregate_scope":None,"aggregate_usage_seconds":total,"device_id":device["device_id"],"stable_place_id":None,"activity_state":None,"online_device_ids":[],"usage_snapshot":{"devices":[{"device_id":device["device_id"],"display_name":device["display_name"],"platform":device.get("platform"),"top_items":top_five}]}},
                statistics_window=window,
            )
        pattern_rows = connection.execute("SELECT rule_type, platform_scope, normalized_pattern FROM blacklist_rules WHERE enabled=1").fetchall()
        patterns: dict[str, list[str]] = {"pc": [], "android": [], "web": []}
        for rule in pattern_rows:
            patterns.get(str(rule["platform_scope"]), []).append(str(rule["normalized_pattern"]))
        black = _blacklist_total(usage, "all", patterns); black_checkpoint = black // 60 // 30 * 30
        if black_checkpoint >= 30:
            snapshot_detail = _snapshot_detail(usage, patterns)
            _insert_timeline(connection, occurred_at=now_utc, event_key="system.blacklist_usage_milestone", category="device", importance="normal", title="黑名单", detail=f"今天全平台黑名单用量已达到 {_duration_text(black_checkpoint)}。" + ("\n" + snapshot_detail if snapshot_detail else ""), dedupe_key=f"system:blacklist:{business}:{black_checkpoint}", evidence={"rule":"blacklist_usage_half_hourly","threshold_minutes":black_checkpoint,"business_date":business.isoformat(),"aggregate_scope":"all_platforms","aggregate_usage_seconds":black,"device_id":None,"stable_place_id":None,"activity_state":None,"online_device_ids":[],"usage_snapshot":_usage_evidence(usage, patterns)}, statistics_window=window)
        location = locations_view(connection, start, latest, None, now=now_utc)
        for stay in location.get("current_stays", []):
            duration = int(stay.get("duration_seconds") or 0); checkpoint = duration // 60 // 60 * 60
            place = str(stay.get("stable_place_id") or stay.get("label") or "未解析地址")
            identity = str(stay.get("segment_identity") or stay.get("event_id"))
            if checkpoint >= 60:
                _insert_timeline(connection, occurred_at=now_utc, event_key="system.location_stay_milestone", category="device", importance="normal", title=place, detail=f"已在{place}连续停留 {checkpoint // 60} 小时。", dedupe_key=f"system:location_stay:{business}:{identity}:{checkpoint}", evidence={"rule":"location_stay_hourly","threshold_minutes":checkpoint,"business_date":business.isoformat(),"aggregate_scope":None,"aggregate_usage_seconds":None,"device_id":stay.get("device_id"),"stable_place_id":place,"segment_identity":identity,"activity_state":None,"online_device_ids":[]}, statistics_window=window)
        current_activity = location.get("activity_state", {}).get("current")
        if isinstance(current_activity, dict) and current_activity.get("state") in {"walking", "running", "transport"}:
            duration = int(current_activity.get("duration_seconds") or 0); checkpoint = duration // 60 // 30 * 30
            if checkpoint >= 30:
                labels = {"walking":"步行", "running":"跑步", "transport":"乘坐交通工具"}; state = str(current_activity["state"])
                identity = str(current_activity.get("start_at") or "current")
                _insert_timeline(connection, occurred_at=now_utc, event_key="system.activity_duration_milestone", category="device", importance="normal", title=labels[state], detail=f"已连续处于{labels[state]}状态 {_duration_text(checkpoint)}。", dedupe_key=f"system:activity:{business}:{state}:{identity}:{checkpoint}", evidence={"rule":"activity_duration_half_hourly","threshold_minutes":checkpoint,"business_date":business.isoformat(),"aggregate_scope":None,"aggregate_usage_seconds":None,"device_id":location.get("activity_state", {}).get("primary_device_id"),"stable_place_id":None,"segment_identity":identity,"activity_state":state,"online_device_ids":[]}, statistics_window=window)
        # Late check is a recent fact/heartbeat test, explicitly not foreground duration.
        sleep_h, sleep_m = map(int, str(settings["sleep_local_time"]).split(":")); sleep_local = datetime.combine(business + (timedelta(days=1) if sleep_h < day_hour else timedelta()), clock_time(sleep_h, sleep_m), SHANGHAI).astimezone(UTC)
        elapsed = int((now_utc - sleep_local).total_seconds() // 60)
        checkpoint = elapsed // 30 * 30
        if checkpoint >= 30:
            slot_at = sleep_local + timedelta(minutes=checkpoint)
            ids = _online_devices_at(connection, "all", slot_at)
            if ids:
                _insert_timeline(connection, occurred_at=slot_at, event_key="system.late_online_check", category="device", importance="normal", title="晚睡", detail=f"进入睡觉时间后，已达到 {_duration_text(checkpoint)}，仍有设备在线。", dedupe_key=f"system:late_online:{business}:{checkpoint}", evidence={"rule":"late_online_half_hourly","threshold_minutes":checkpoint,"business_date":business.isoformat(),"aggregate_scope":None,"aggregate_usage_seconds":None,"device_id":None,"stable_place_id":None,"activity_state":None,"online_device_ids":ids}, statistics_window=window)
        _evaluate_reports(connection, store, settings, business, start, latest, now_utc)
        connection.commit()
    except Exception:
        connection.rollback(); raise


def _evaluate_reports(connection: Any, store: Any, settings: dict[str, Any], business: date, start: datetime, latest: datetime, now: datetime) -> None:
    sh = now.astimezone(SHANGHAI)
    end = start + timedelta(days=1)
    target = settings["ai_display_name"]
    def report_data(window: dict[str, Any], *, facts_business: date = business) -> dict[str, Any]:
        from_at, to_at = _parse_utc(window["from"]) or start, _parse_utc(window["to"]) or now
        view = _usage_snapshot(connection, from_at, to_at)
        devices = [{"display_name":d.get("display_name"), "usage_seconds":sum(int(v) for v in d.get("hourly", {}).values())} for d in view.get("devices", [])]
        total = sum(d["usage_seconds"] for d in devices)
        top_items = []
        for device in view.get("devices", []):
            for name, seconds in {**(device.get("apps") or {}), **(device.get("sites") or {})}.items():
                top_items.append({"name":name, "seconds":int(seconds), "device_name":device.get("display_name"), "blacklisted": name in (device.get("blacklist_apps") or {}) or name in (device.get("blacklist_sites") or {})})
        patterns = {"pc": [], "android": [], "web": []}
        for rule in connection.execute("SELECT rule_type, platform_scope, normalized_pattern FROM blacklist_rules WHERE enabled=1").fetchall(): patterns.get(str(rule["platform_scope"]), []).append(str(rule["normalized_pattern"]))
        black = _blacklist_total(view, "all", patterns)
        rows = connection.execute("SELECT occurred_at,title,detail,importance,wish_id,event_key,subject_json FROM timeline_events WHERE occurred_at >= ? AND occurred_at < ?", (window["from"], window["to"])).fetchall()
        events = []
        for r in rows:
            subject = json.loads(r["subject_json"] or "{}")
            # A wish fact wins over another rendering of the same occurrence.
            fact_key = f"wish:{r['wish_id']}" if r["wish_id"] else str(subject.get("fact_key") or r["event_key"])
            events.append({"occurred_at":r["occurred_at"], "title":r["title"], "detail":r["detail"], "importance":r["importance"], "wish_id":r["wish_id"], "event_key":r["event_key"], "fact_key":fact_key})
        location = locations_view(connection, from_at, to_at, None, now=now)
        intervals = list(location.get("activity_state", {}).get("intervals", []))
        wishes = [store._wish_from_connection(connection, str(r[0])) for r in connection.execute("SELECT wish_id FROM wishes WHERE status = 'active'").fetchall()]
        health = build_health_info(connection, facts_business, now=now)
        steps = health.get("steps", {}).get("devices", []) if isinstance(health, dict) else []
        return {"statistics_window":window, "business_date":facts_business.isoformat(), "from":window["from"], "to":window["to"], "usage_seconds":total, "blacklist_seconds":black, "devices":devices, "top_items":top_items, "location_activity":intervals, "wishes":[w for w in wishes if w], "important_events":events, "steps":sum(int(x.get("steps", 0)) for x in steps if isinstance(x, dict)), "sleep":health.get("sleep") if isinstance(health, dict) else None}

    def morning_body() -> tuple[dict[str, Any], str]:
        previous = business - timedelta(days=1)
        previous_start, _ = _business_day_window(previous, int(settings["day_start_hour"]))
        previous_window = {"business_date": previous.isoformat(), "from": _utc_text(previous_start), "to": _utc_text(start)}
        facts = report_data(previous_window, facts_business=previous)
        # Usage and steps belong to yesterday. Sleep is keyed by wake date, so
        # "last night" for today's report must use today's health projection.
        today = report_data({"business_date":business.isoformat(), "from":_utc_text(start), "to":_utc_text(start)})
        payload = {"business_date": business.isoformat(), "statistics_window": previous_window,
                   "yesterday": facts, "sleep": today["sleep"], "yesterday_steps": facts["steps"],
                   "wishes": today["wishes"]}
        return previous_window, generate_morning_report(payload)
    def fixed(schedule: dict[str, Any], event_key: str, title: str) -> None:
        if not schedule.get("enabled"): return
        h, m = map(int, schedule["local_time"].split(":")); planned = datetime.combine(business + (timedelta(days=1) if h < int(settings["day_start_hour"]) else timedelta()), clock_time(h,m), SHANGHAI).astimezone(UTC)
        if planned > now or not (start <= planned < end): return
        window = {"business_date":business.isoformat(), "from":_utc_text(start), "to":_utc_text(planned)}
        if event_key == "report.evening": body = generate_evening_report(report_data(window))
        else: window, body = morning_body()
        _insert_timeline(connection, occurred_at=planned, event_key=event_key, category="system", importance="high", title=title, detail=f"{title}已准备就绪。等待 {target} 接入。", dedupe_key=f"{event_key}:{business}:{_utc_text(planned)}", evidence={"report_kind":event_key.removeprefix("report."),"business_date":business.isoformat(),"statistics_window":window,"periodic_minutes":None,"body":body}, statistics_window=window, delivery={"state":"not_configured","target_display_name":target,"updated_at":_utc_text(now)})
    fixed(settings["evening_report"], "report.evening", "今日晚报")
    morning = settings["morning_report"]
    if morning.get("mode") == "fixed_time": fixed(morning, "report.morning", "今日早报")
    elif morning.get("enabled") and morning.get("mode") == "after_first_usage":
        first = connection.execute("SELECT MIN(occurred_at) FROM events WHERE event_type='app.foreground' AND occurred_at >= ? AND occurred_at < ?", (_utc_text(start), _utc_text(latest))).fetchone()[0]
        if first:
            first_at = _parse_utc(first)
            due = first_at + timedelta(minutes=int(morning.get("delay_minutes", 60))) if first_at else None
            if due and now >= due:
                window, body = morning_body()
                _insert_timeline(connection, occurred_at=due, event_key="report.morning", category="system", importance="high", title="今日早报", detail=f"今日早报已准备就绪。等待 {target} 接入。", dedupe_key=f"report.morning:{business}", evidence={"report_kind":"morning","business_date":business.isoformat(),"statistics_window":window,"periodic_minutes":None,"body":body}, statistics_window=window, delivery={"state":"not_configured","target_display_name":target,"updated_at":_utc_text(now)})
    periodic = settings["periodic_summary"]
    if not periodic.get("enabled"):
        return
    interval = int(periodic["interval_minutes"])
    def local_at(value: str) -> datetime:
        h, m = map(int, value.split(":")); day = business + (timedelta(days=1) if h < int(settings["day_start_hour"]) else timedelta())
        return datetime.combine(day, clock_time(h, m), SHANGHAI).astimezone(UTC)
    range_start, range_end = local_at(periodic["start_local_time"]), local_at(periodic["end_local_time"])
    if range_end < range_start:
        range_end += timedelta(days=1)
    # The first report fires at start_local_time itself, then every interval,
    # while the slot stays at or before end_local_time (inclusive).
    span_seconds = int((range_end - range_start).total_seconds())
    if span_seconds <= 0:
        return
    slot_count = span_seconds // (interval * 60) + 1
    slots = [range_start + timedelta(minutes=interval * index) for index in range(0, slot_count)]
    eligible = [slot for slot in slots if slot <= now and start <= slot < end]
    if not eligible:
        return
    to = eligible[-1]
    previous_slot = connection.execute("SELECT MAX(occurred_at) FROM timeline_events WHERE event_key='report.periodic' AND occurred_at >= ? AND occurred_at < ?", (_utc_text(start), _utc_text(to))).fetchone()[0]
    if previous_slot:
        from_ = _parse_utc(previous_slot)
    else:
        first = connection.execute("SELECT MIN(occurred_at) FROM events WHERE event_type='app.foreground' AND occurred_at >= ? AND occurred_at < ?", (_utc_text(start), _utc_text(to))).fetchone()[0]
        from_ = _parse_utc(first) if first else None
    if from_ is None or from_ >= to:
        return
    window = {"business_date":business.isoformat(), "from":_utc_text(from_), "to":_utc_text(to)}
    label = f"{from_.astimezone(SHANGHAI):%H:%M}–{to.astimezone(SHANGHAI):%H:%M}"
    _insert_timeline(connection, occurred_at=to, event_key="report.periodic", category="system", importance="high", title="定时总结", detail=f"{label} 定时总结已准备就绪。等待 {target} 接入。", dedupe_key=f"report:periodic:{business}:{_utc_text(to)}", evidence={"report_kind":"periodic","business_date":business.isoformat(),"statistics_window":window,"periodic_minutes":interval,"body":generate_periodic_report(report_data(window))}, statistics_window=window, delivery={"state":"not_configured","target_display_name":target,"updated_at":_utc_text(to)})
