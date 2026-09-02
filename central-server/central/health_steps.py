"""Pure derivation for ``health.steps_observation`` events.

This module deliberately knows nothing about SQLite, HTTP, or authentication.  The
caller supplies already-authorised events and is responsible for parsing their
wire representation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Iterable
from uuid import UUID


@dataclass(frozen=True)
class StepDeviceResult:
    device_id: str
    status: str
    steps: int | None
    hourly_steps: tuple[int, ...]
    sample_count: int
    first_sample_at: str | None
    last_sample_at: str | None
    warnings: tuple[str, ...] = field(default_factory=tuple)


try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Asia/Shanghai has no DST transitions in our supported history.  This keeps
    # the pure module usable on Windows Python installations without tzdata.
    SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _local_date(occurred_at: object) -> str | None:
    value = _local_datetime(occurred_at)
    return value.date().isoformat() if value is not None else None


def _local_datetime(occurred_at: object) -> datetime | None:
    if not isinstance(occurred_at, str):
        return None
    try:
        value = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(SHANGHAI)


def derive_steps_by_device(events: Iterable[dict[str, Any]], date: str) -> list[StepDeviceResult]:
    """Derive per-device steps for an ISO local date.

    A delta belongs entirely to the local date and hour of its later observation.
    Only immediately adjacent valid samples in the same counter session are
    differenced; a session change or invalid observation breaks the baseline.
    Devices are intentionally never combined.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("event_type") != "health.steps_observation":
            continue
        device_id = event.get("device_id")
        if isinstance(device_id, str) and device_id:
            grouped[device_id].append(event)

    results: list[StepDeviceResult] = []
    for device_id, observations in sorted(grouped.items()):
        observations.sort(key=lambda item: str(item.get("occurred_at", "")))
        day_samples = [item for item in observations if _local_date(item.get("occurred_at")) == date]
        if not day_samples:
            continue
        warnings: list[str] = []
        hourly_steps = [0] * 24
        valid_delta = False
        previous: dict[str, Any] | None = None
        for observation in observations:
            payload = observation.get("payload")
            if not isinstance(payload, dict):
                if _local_date(observation.get("occurred_at")) == date:
                    warnings.append("invalid_payload")
                previous = None
                continue
            value = payload.get("counter_value")
            session = payload.get("counter_session_id")
            if (
                not isinstance(value, int) or isinstance(value, bool) or value < 0 or
                not isinstance(session, str) or
                not run_uuid(session)
            ):
                if _local_date(observation.get("occurred_at")) == date:
                    warnings.append("invalid_observation")
                previous = None
                continue
            local_observed_at = _local_datetime(observation.get("occurred_at"))
            if previous is not None and previous["session"] == session:
                delta = value - previous["value"]
                if local_observed_at is not None and local_observed_at.date().isoformat() == date:
                    if delta >= 0:
                        hourly_steps[local_observed_at.hour] += delta
                        valid_delta = True
                    else:
                        warnings.append("negative_counter_delta")
            previous = {"session": session, "value": value}
        total = sum(hourly_steps)
        results.append(StepDeviceResult(
            device_id=device_id,
            status="available" if valid_delta else "insufficient_samples",
            steps=total if valid_delta else None,
            hourly_steps=tuple(hourly_steps),
            sample_count=len(day_samples),
            first_sample_at=str(day_samples[0].get("occurred_at")) or None,
            last_sample_at=str(day_samples[-1].get("occurred_at")) or None,
            warnings=tuple(dict.fromkeys(warnings)),
        ))
    return results


def run_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False
