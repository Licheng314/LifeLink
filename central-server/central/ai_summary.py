"""AI-facing Markdown summaries generated on the central service.

Both the usage and location AI contexts are produced here so every reader
(Web, AI, future mobile) sees the same text. They are deterministic
projections over the central SQLite store and can be regenerated at any
time; they are not a separate source of truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # pragma: no cover - defensive
    DISPLAY_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def format_ai_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} 时 {minutes:02d} 分"
    return f"{minutes} 分"


def usage_ai_summary(
    devices: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    is_blacklisted_app: Callable[[str, str], bool],
    is_blacklisted_site: Callable[[str, str], bool],
) -> str:
    """Render the device-oriented Markdown usage context from usage_view output.

    Reuses per-device aggregates already computed by read_model.usage_view.
    ``hourly`` is the AFK-trimmed foreground interval union for desktop and
    the foreground interval union for mobile.

    The "recent" window is the whole hour preceding the current display hour,
    between one and two hours long depending on how far into the current hour
    we are. It mirrors the legacy PC dashboard semantics.
    """
    date_str = start.astimezone(DISPLAY_TIMEZONE).date().isoformat()
    now_local = end.astimezone(DISPLAY_TIMEZONE)
    recent_end_local = now_local.replace(minute=0, second=0, microsecond=0)
    recent_start_local = recent_end_local - timedelta(hours=1)
    start_local = start.astimezone(DISPLAY_TIMEZONE)
    if recent_start_local <= start_local:
        recent_start_local = start_local
        recent_end_local = now_local
    recent_range = (
        f"{recent_start_local.strftime('%H:%M')}~{recent_end_local.strftime('%H:%M')}"
    )

    device_rows: list[dict[str, Any]] = []
    for device in devices:
        platform = str(device.get("platform", "")).casefold()
        is_mobile = platform in {"android", "ios"}
        platform_scope = "android" if platform == "android" else "pc"
        hourly_usage = device.get("hourly") or {}
        usage_seconds = (
            int(sum(hourly_usage.values())) if isinstance(hourly_usage, dict) else 0
        )

        apps_today: dict[str, int] = {}
        for name, seconds in (device.get("apps") or {}).items():
            apps_today[str(name)] = apps_today.get(str(name), 0) + int(seconds or 0)
        sites_today: dict[str, int] = {}
        for name, seconds in (device.get("sites") or {}).items():
            sites_today[str(name)] = sites_today.get(str(name), 0) + int(seconds or 0)

        recent_hours: list[str] = []
        if isinstance(device.get("hourly_apps"), dict):
            for hour_text in device["hourly_apps"].keys():
                try:
                    hour_int = int(hour_text)
                except (TypeError, ValueError):
                    continue
                if recent_start_local.hour <= hour_int <= recent_end_local.hour:
                    recent_hours.append(hour_text)
        recent_apps: dict[str, int] = {}
        for hour_text in recent_hours:
            for name, seconds in (device.get("hourly_apps") or {}).get(hour_text, {}).items():
                recent_apps[str(name)] = recent_apps.get(str(name), 0) + int(seconds or 0)
        recent_sites: dict[str, int] = {}
        for hour_text in recent_hours:
            for name, seconds in (device.get("hourly_sites") or {}).get(hour_text, {}).items():
                recent_sites[str(name)] = recent_sites.get(str(name), 0) + int(seconds or 0)

        black_today = sum(s for n, s in apps_today.items() if is_blacklisted_app(n, platform_scope))
        black_today += sum(s for n, s in sites_today.items() if is_blacklisted_site(n, platform_scope))
        black_recent = sum(s for n, s in recent_apps.items() if is_blacklisted_app(n, platform_scope))
        black_recent += sum(s for n, s in recent_sites.items() if is_blacklisted_site(n, platform_scope))

        today_usage = usage_seconds
        recent_usage = sum(
            int((device.get("hourly") or {}).get(hour_text, 0) or 0)
            for hour_text in recent_hours
        )

        latest_name: str | None = None
        latest_hour: int | None = None
        for hour_text, items in (device.get("hourly_apps") or {}).items():
            try:
                hour_int = int(hour_text)
            except (TypeError, ValueError):
                continue
            if not items:
                continue
            if latest_hour is None or hour_int > latest_hour:
                top_in_hour = max(items.items(), key=lambda pair: pair[1])
                latest_hour = hour_int
                latest_name = str(top_in_hour[0])

        if today_usage <= 0:
            continue
        device_rows.append({
            "is_mobile": is_mobile,
            "display_name": str(device.get("display_name") or ""),
            "usage": today_usage,
            "recent_usage": recent_usage,
            "black_usage": min(today_usage, black_today),
            "recent_black_usage": min(recent_usage, black_recent),
            "apps": apps_today,
            "recent_apps": recent_apps,
            "sites": sites_today,
            "recent_sites": recent_sites,
            "latest_name": latest_name,
            "latest_hour": latest_hour,
        })

    device_rows.sort(key=lambda item: item["usage"], reverse=True)

    lines = [
        f"【应用使用总览 · {date_str}】",
        "",
        "统计口径：",
        "- 设备使用时长：PC 为前台应用时间剪去明确 AFK 区间后的时间；手机为前台应用使用时间。",
        "- 黑名单时长：用户希望避免过度沉迷的特定应用或网站的使用时长。",
        f"- 近期：{recent_range}（从当前整点的前一整点到当前时间，时长介于 1 小时至不足 2 小时）。不同近期窗口的黑名单时长需结合窗口长度判断。",
        "- Chrome 不参与用量排行；已识别的网站时长代替 Chrome 作为排行单元，未被网址标记覆盖的 Chrome 时间不会进入排行。",
    ]
    if not device_rows:
        lines.extend(["", "今日暂无设备使用时长大于 0 的设备。"])
        return "\n".join(lines)

    for index, item in enumerate(device_rows, start=1):
        platform_label = "手机" if item["is_mobile"] else "PC"
        lines.extend([
            "",
            f"{index}. {item['display_name']}（{platform_label}）",
            f"设备今日使用时长：{format_ai_duration(item['usage'])}",
        ])
        if item["latest_name"] and item["latest_hour"] is not None:
            lines.extend([
                "（1）最近使用：",
                f"{item['latest_name']} | 更新时间点 {item['latest_hour']:02d}:00",
            ])
        lines.extend([
            f"（2）近期用量信息（时间区间：{recent_range}）：",
            f"用量时长：{format_ai_duration(item['recent_usage'])} | 黑名单时长：{format_ai_duration(item['recent_black_usage'])}",
        ])
        recent_ranked_values = {
            name: seconds
            for name, seconds in item["recent_apps"].items()
            if "chrome" not in name.casefold()
        }
        for name, seconds in item["recent_sites"].items():
            recent_ranked_values[name] = recent_ranked_values.get(name, 0) + seconds
        recent_ranked = sorted(
            recent_ranked_values.items(),
            key=lambda pair: pair[1],
            reverse=True,
        )[:3]
        lines.extend(
            [f"{rank}. {name}：{format_ai_duration(seconds)}" for rank, (name, seconds) in enumerate(recent_ranked, start=1)]
            or ["无"]
        )
        lines.extend([
            "（3）今日用量信息：",
            f"用量时长：{format_ai_duration(item['usage'])} | 黑名单时长：{format_ai_duration(item['black_usage'])}",
        ])
        today_ranked_values = {
            name: seconds
            for name, seconds in item["apps"].items()
            if "chrome" not in name.casefold()
        }
        for name, seconds in item["sites"].items():
            today_ranked_values[name] = today_ranked_values.get(name, 0) + seconds
        today_ranked = sorted(
            today_ranked_values.items(),
            key=lambda pair: pair[1],
            reverse=True,
        )[:3]
        lines.extend(
            [f"{rank}. {name}：{format_ai_duration(seconds)}" for rank, (name, seconds) in enumerate(today_ranked, start=1)]
            or ["无"]
        )
    return "\n".join(lines)
