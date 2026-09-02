"""Pure Chinese prose builders for v1.13 report delivery bodies.

The scheduler supplies one dictionary assembled from central read models.  This
module deliberately neither reads SQLite nor knows about delivery state, so a
report can be regenerated deterministically from its evidence.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def generate_morning_report(data: Mapping[str, Any] | None = None, **kwargs: Any) -> str:
    """Build a morning body from ``business_date``, yesterday facts and wishes."""
    values = _values(data, kwargs)
    business = _date(values.get("business_date"))
    lines = [f"【今日早报 · {business}】"]
    health: list[str] = []
    sleep = values.get("sleep") or values.get("sleep_reference") or {}
    if isinstance(sleep, Mapping):
        # health_sleep.py uses the explicit estimated_* names.  Keep start/end
        # as a compatibility fallback for older report fixtures.
        sleep_start = sleep.get("estimated_start") or sleep.get("start")
        sleep_end = sleep.get("estimated_end") or sleep.get("end")
        if sleep_start and sleep_end:
            seconds = int(
                sleep.get("rest_seconds")
                or sleep.get("interval_seconds")
                or sleep.get("seconds")
                or _seconds_between(sleep_start, sleep_end)
            )
            health.append(f"睡眠参考区间：{_clock(sleep_start)}–{_clock(sleep_end)}，约 {_duration(seconds)}。")
        elif sleep.get("status") in {"estimating", "insufficient_data"}:
            health.append("睡眠参考区间：当前数据不足，暂无法估算。")
    steps = values.get("yesterday_steps", values.get("steps"))
    if steps is not None:
        health.append(f"昨日步数：{int(steps)} 步。")
    _section(lines, "昨夜健康", health)
    _section(lines, "心愿", _wish_lines(values.get("wishes", []), morning=True, business_date=values.get("business_date")))
    yesterday = values.get("yesterday") if isinstance(values.get("yesterday"), Mapping) else values
    other = _usage_lines(yesterday, top_limit=5, prefix="")
    late = yesterday.get("late_online_checks") if isinstance(yesterday, Mapping) else None
    if late:
        other.append(f"晚睡检查点触发 {int(late)} 次。")
    _section(lines, "昨日其他需要关注", other)
    lines += ["", "请生成一份简短早报：如果存在早于当前业务日的待填写心愿日期，先提醒用户填写结果；当前业务日尚未填写只表示今天的进度，不要提醒。再概括睡眠、步数和昨日最值得关注的一项变化。不要推断没有记录的原因。"]
    return "\n".join(lines)


def generate_evening_report(data: Mapping[str, Any] | None = None, **kwargs: Any) -> str:
    """Build the current-business-day evening report, bounded by ``to`` if set."""
    values = _values(data, kwargs)
    business = _date(values.get("business_date"))
    lines = [f"【今日晚报 · {business}】"]
    _section(lines, "设备与应用", _usage_lines(values, top_limit=5, prefix=""))
    health: list[str] = []
    if values.get("steps") is not None:
        health.append(f"今日步数：{int(values['steps'])} 步。")
    health.extend(_location_lines(values.get("location_activity", values.get("intervals", [])), values.get("from"), values.get("to")))
    _section(lines, "位置、活动与健康", health)
    _section(lines, "心愿", _wish_lines(values.get("wishes", []), business_date=values.get("business_date")))
    _section(lines, "重要事件", _important_event_lines(values.get("important_events", values.get("events", [])), values.get("from"), values.get("to")))
    lines += ["", "请生成一份简短晚报：概括今天的设备使用、全天主要行程与活动、健康、心愿结果和重要事件；最多指出两项值得关注的事实，不评价用户品格。"]
    return "\n".join(lines)


def generate_periodic_report(data: Mapping[str, Any] | None = None, **kwargs: Any) -> str:
    """Build a report for the strict half-open ``[from, to)`` scheduler window."""
    values = _values(data, kwargs)
    start, end = values.get("from"), values.get("to")
    if not start or not end:
        raise ValueError("periodic report requires from and to")
    label = f"{_clock(start)}–{_clock(end)}"
    lines = [f"【定时总结 · {label}】", "", f"统计区间：{_date(values.get('business_date'))} {label}（Life Link 业务日）"]
    changes = _usage_lines(values, top_limit=5, prefix="新增")
    changes.extend(_location_lines(values.get("location_activity", values.get("intervals", [])), start, end))
    _section(lines, "区间变化", changes)
    _section(lines, "本区间事件", _important_event_lines(values.get("important_events", values.get("events", [])), start, end))
    lines += ["", "请只总结该区间内的新变化；不要重复早于 " + _clock(start) + " 的事件，也不要把当前累计值误写成区间新增值。"]
    return "\n".join(lines)


def _values(data: Mapping[str, Any] | None, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(data or {})
    result.update(kwargs)
    window = result.get("statistics_window")
    if isinstance(window, Mapping):
        for name in ("business_date", "from", "to"):
            result.setdefault(name, window.get(name))
    return result


def _section(lines: list[str], title: str, items: list[str]) -> None:
    if items:
        lines.extend(["", f"{title}：", *[f"- {item}" for item in items]])


def _usage_lines(data: Mapping[str, Any], *, top_limit: int, prefix: str) -> list[str]:
    total = data.get("usage_seconds", data.get("total_usage_seconds"))
    blacklist = data.get("blacklist_seconds", data.get("blacklist_usage_seconds"))
    lines: list[str] = []
    if total is not None:
        text = f"所有设备{prefix}使用 {_duration(int(total))}"
        if blacklist is not None:
            text += f"；黑名单{prefix}用量 {_duration(int(blacklist))}"
        lines.append(text + "。")
    devices = data.get("devices", [])
    device_bits = []
    for item in devices if isinstance(devices, list) else []:
        if isinstance(item, Mapping) and item.get("usage_seconds") is not None and int(item["usage_seconds"]) > 0:
            device_bits.append(f"{item.get('display_name', item.get('name', '设备'))} {_duration(int(item['usage_seconds']))}")
    if device_bits:
        lines.append("其中" + "、".join(device_bits) + "。")
    items = [x for x in data.get("top_items", data.get("apps", [])) if isinstance(x, Mapping)]
    items.sort(key=lambda x: int(x.get("seconds", x.get("usage_seconds", 0))), reverse=True)
    if items:
        heading = f"用量较多的 {min(top_limit, len(items))} 个应用或网站："
        lines.append(heading)
        for index, item in enumerate(items[:top_limit], 1):
            seconds = int(item.get("seconds", item.get("usage_seconds", 0)))
            device = item.get("device_name", item.get("device"))
            suffix = " ⚠️" if item.get("blacklisted") else ""
            owner = f"（{device}）" if device else ""
            lines.append(f"  {index}. {item.get('name', '未命名项目')}{owner}：{_duration(seconds)}{suffix}")
    return lines


def _location_lines(items: Any, start: Any, end: Any) -> list[str]:
    clipped = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw); left, right = _clip(item.get("from", item.get("start")), item.get("to", item.get("end")), start, end)
        if left is None or right is None or right <= left:
            continue
        item["_left"], item["_right"] = left, right
        item["_seconds"] = int((right - left).total_seconds())
        item["_distance"] = float(item.get("distance_m", item.get("distance", 0)) or 0)
        clipped.append(item)
    # Score before display order: large movement first, otherwise long derived interval.
    chosen = sorted(clipped, key=lambda x: (x["_distance"], x["_seconds"]), reverse=True)[:5]
    chosen.sort(key=lambda x: x["_left"])
    return [_format_interval(item, end is not None and _parse(item.get("to", item.get("end"))) and item["_right"] < _parse(item.get("to", item.get("end")))) for item in chosen]


def _format_interval(item: Mapping[str, Any], truncated: bool) -> str:
    when = f"{_clock(item['_left'])}–{_clock(item['_right'])}"
    state = str(item.get("activity_state", item.get("state", "")))
    place, origin, destination = item.get("place"), item.get("from_place"), item.get("to_place")
    if origin and destination:
        text = f"{when} 从{origin}前往{destination}"
    elif place:
        text = f"{when} 稳定停留在{place}"
    elif state:
        text = f"{when} 处于{_state(state)}状态"
    else:
        return when
    if state and (origin or destination): text += f"，主要处于{_state(state)}状态"
    if item["_distance"] > 0: text += f"，位移约 {item['_distance'] / 1000:.1f} 公里"
    if item["_seconds"] >= 60 and not item["_distance"]: text += f"，持续 {_duration(item['_seconds'])}"
    if truncated or item.get("is_active"): text += "（截至报告时间）"
    return text + "。"


def _important_event_lines(events: Any, start: Any, end: Any) -> list[str]:
    candidates = []
    for raw in events if isinstance(events, list) else []:
        if not isinstance(raw, Mapping): continue
        occurred = _parse(raw.get("occurred_at", raw.get("at")))
        left, _ = _clip(occurred, occurred, start, end, point=True)
        if occurred is None or left is None: continue
        candidates.append((raw, occurred))
    candidates.sort(key=lambda pair: (pair[0].get("importance") == "high", _is_wish(pair[0]), pair[1]), reverse=True)
    selected, seen = [], set()
    for event, occurred in candidates:
        key = str(event.get("fact_key", event.get("dedupe_group", event.get("subject", event.get("title", "")))))
        if key in seen: continue
        seen.add(key); selected.append((event, occurred))
        if len(selected) == 5: break
    selected.sort(key=lambda pair: pair[1])
    return [f"{_clock(occurred)} {event.get('title', '重要事件')}{('：' + str(event['detail'])) if event.get('detail') else ''}" for event, occurred in selected]


def _wish_lines(wishes: Any, morning: bool = False, business_date: Any = None) -> list[str]:
    """Render active wishes in the same prose shape as the background summary.

    Keep reached-but-failed, reached-but-unfilled, and future days explicit.
    The completed numerator counts only explicit ``completed`` assessments.
    """
    lines = []
    today = _date(business_date) if business_date else None
    for wish in wishes if isinstance(wishes, list) else []:
        if isinstance(wish, str): lines.append(wish); continue
        if isinstance(wish, Mapping):
            text = wish.get("display_text") or wish.get("summary")
            if text: lines.append(str(text)); continue
            name = wish.get("text", "心愿")
            status = wish.get("status")
            if status != "active":
                continue
            days = wish.get("wish_days") or []
            today_iso = today if today else ""
            if not today_iso:
                today_iso = str(wish.get("ends_on") or "")
            completed = [d for d in days if d.get("evaluation") == "completed"]
            not_completed = [d for d in days if d.get("evaluation") == "not_completed" and str(d.get("business_date") or "") <= today_iso]
            pending_prior = [d for d in days if d.get("evaluation") is None and str(d.get("business_date") or "") < today_iso]
            pending_today = [d for d in days if d.get("evaluation") is None and str(d.get("business_date") or "") == today_iso]
            pending = pending_prior + pending_today
            future = [d for d in days if str(d.get("business_date") or "") > today_iso]
            def dates(items):
                return "、".join(str(item.get("business_date"))[5:] for item in items)
            duration = int(wish.get("duration_days") or len(days))
            state = "待完结" if pending and today_iso > str(wish.get("ends_on") or "") else "进行中"
            parts = [f"周期进度：已完成 {len(completed)}/{duration} 天（仅统计已完成）"]
            if completed: parts.append(f"已完成：{dates(completed)}")
            if not_completed: parts.append(f"已到达但未完成：{dates(not_completed)}（不需要提醒）")
            if pending_prior: parts.append(f"待填写：{dates(pending_prior)}（需要提醒用户填写结果）")
            if pending_today: parts.append(f"待填写：{dates(pending_today)}（今天的进度。不需要提醒）")
            if future: parts.append(f"尚未到达：{dates(future)}（不计入进度，不需要提醒）")
            lines.append(f"「{name}」{state}，" + "；".join(parts) + "。")
    return lines


def _clip(left: Any, right: Any, start: Any, end: Any, point: bool = False):
    left_dt, right_dt, start_dt, end_dt = _parse(left), _parse(right), _parse(start), _parse(end)
    if left_dt is None: return None, None
    if point:
        if (start_dt and left_dt < start_dt) or (end_dt and left_dt >= end_dt): return None, None
        return left_dt, left_dt
    if right_dt is None: right_dt = end_dt
    if right_dt is None: return None, None
    return max(left_dt, start_dt) if start_dt else left_dt, min(right_dt, end_dt) if end_dt else right_dt


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime): return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str): return None
    try: return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError: return None


def _clock(value: Any) -> str:
    parsed = _parse(value)
    return parsed.astimezone(SHANGHAI).strftime("%H:%M") if parsed else str(value)[11:16]


def _date(value: Any) -> str:
    if isinstance(value, date): return value.isoformat()
    return str(value or "未指定业务日")


def _seconds_between(start: Any, end: Any) -> int:
    return int((_parse(end) - _parse(start)).total_seconds()) if _parse(start) and _parse(end) else 0


def _duration(seconds: int) -> str:
    minutes = max(0, seconds) // 60
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} 分钟"
    return f"{hours} 小时" if not minutes else f"{hours} 小时 {minutes:02d} 分钟"


def _state(value: str) -> str:
    return {"walking": "步行", "running": "跑步", "transport": "乘坐交通工具", "stationary": "静止", "still": "静止"}.get(value, value)


def _is_wish(event: Mapping[str, Any]) -> bool:
    return bool(event.get("wish_id")) or str(event.get("event_key", "")).startswith("wish.") or str(event.get("title", "")).startswith("心愿提醒·")
