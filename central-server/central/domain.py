"""Contract validation and deterministic event normalization."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


PLATFORMS = {"android", "desktop", "web"}
EVENT_TYPES = {
    "app.foreground",
    "web.foreground",
    "device.input_state",
    "custom.event",
    "calendar.event",
    "task.update",
    "location.observation",
    "location.sample",
    "location.stay",
    "location.visit",
    "manual.note",
    "health.steps_observation",
}
SOURCE_KINDS = {"android", "desktop", "web", "manual"}
COLLECTORS = {
    "accessibility_service",
    "usage_stats",
    "activitywatch",
    "windows_native",
    "fused_location",
    "browser_extension",
    "calendar_import",
    "life_radio_app",
    "manual_entry",
    "step_counter",
}
RELIABILITY_VALUES = {"observed", "inferred", "user_confirmed"}


class BatchValidationError(ValueError):
    """The batch envelope cannot be processed safely."""


@dataclass(frozen=True)
class Device:
    device_id: str
    platform: str
    display_name: str


@dataclass(frozen=True)
class BatchEnvelope:
    schema_version: str
    batch_id: str
    device: Device
    sent_at: str
    events: tuple[Any, ...]
    request_payload: dict[str, Any]


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    occurred_at: str
    event_type: str
    source: dict[str, str]
    duration_seconds: int | None
    revision: int
    payload: dict[str, Any]
    document: dict[str, Any]
    content_hash: str
    mutable: bool


@dataclass(frozen=True)
class EventRejection:
    event_id: str
    code: str
    message: str


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_batch_envelope(
    payload: Any,
    idempotency_key: str | None,
    *,
    max_events: int,
) -> BatchEnvelope:
    if not isinstance(payload, dict):
        raise BatchValidationError("request body must be an object")
    if payload.get("schema_version") != "v1":
        raise BatchValidationError("schema_version must be v1")

    batch_id = payload.get("batch_id")
    if not is_uuid(batch_id):
        raise BatchValidationError("batch_id must be a UUID")
    if not is_uuid(idempotency_key):
        raise BatchValidationError("Idempotency-Key must be a UUID")
    if idempotency_key != batch_id:
        raise BatchValidationError("Idempotency-Key must match batch_id")

    raw_device = payload.get("device")
    if not isinstance(raw_device, dict):
        raise BatchValidationError("device must be an object")
    device_id = raw_device.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 200:
        raise BatchValidationError("device.device_id must contain 1 to 200 characters")
    platform = raw_device.get("platform")
    if platform not in PLATFORMS:
        raise BatchValidationError("device.platform is not supported")
    display_name = raw_device.get("display_name", device_id)
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 100:
        raise BatchValidationError("device.display_name must contain 1 to 100 characters")

    sent_at = payload.get("sent_at")
    if not is_utc_timestamp(sent_at):
        raise BatchValidationError("sent_at must be a UTC ISO-8601 timestamp ending in Z")
    events = payload.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= max_events:
        raise BatchValidationError(f"events must contain between 1 and {max_events} items")

    event_ids: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            raise BatchValidationError("every event must be an object")
        event_id = event.get("event_id")
        if not is_uuid(event_id):
            raise BatchValidationError("every event.event_id must be a UUID")
        event_ids.append(event_id)
    if len(event_ids) != len(set(event_ids)):
        raise BatchValidationError("events must not repeat an event_id within one batch")

    return BatchEnvelope(
        schema_version="v1",
        batch_id=batch_id,
        device=Device(
            device_id=device_id.strip(),
            platform=platform,
            display_name=display_name.strip(),
        ),
        sent_at=sent_at,
        events=tuple(events),
        request_payload=payload,
    )


def _event_id_for_error(event: Any) -> str:
    if isinstance(event, dict):
        value = event.get("event_id")
        if isinstance(value, str):
            return value
    return ""


def normalize_event(event: Any) -> tuple[NormalizedEvent | None, EventRejection | None]:
    event_id = _event_id_for_error(event)
    if not isinstance(event, dict):
        return None, EventRejection("", "invalid_event", "event must be an object")
    if not is_uuid(event_id):
        return None, EventRejection(event_id, "invalid_event_id", "event_id must be a UUID")

    occurred_at = event.get("occurred_at")
    if not is_utc_timestamp(occurred_at):
        return None, EventRejection(
            event_id,
            "invalid_occurred_at",
            "occurred_at must be a UTC ISO-8601 timestamp ending in Z",
        )
    event_type = event.get("event_type")
    if event_type not in EVENT_TYPES:
        return None, EventRejection(event_id, "unsupported_event_type", "event_type is not supported")

    raw_source = event.get("source")
    if not isinstance(raw_source, dict):
        return None, EventRejection(event_id, "invalid_source", "source must be an object")
    kind = raw_source.get("kind")
    collector = raw_source.get("collector")
    reliability = raw_source.get("reliability", "observed")
    if kind not in SOURCE_KINDS:
        return None, EventRejection(event_id, "invalid_source_kind", "source.kind is not supported")
    if collector not in COLLECTORS:
        return None, EventRejection(event_id, "invalid_collector", "source.collector is not supported")
    if reliability not in RELIABILITY_VALUES:
        return None, EventRejection(event_id, "invalid_reliability", "source.reliability is not supported")

    raw_duration = event.get("duration_seconds")
    if raw_duration is not None and (
        isinstance(raw_duration, bool) or not isinstance(raw_duration, int) or raw_duration < 0
    ):
        return None, EventRejection(
            event_id,
            "invalid_duration",
            "duration_seconds must be a non-negative integer",
        )
    raw_revision = event.get("revision", 0)
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 0:
        return None, EventRejection(event_id, "invalid_revision", "revision must be a non-negative integer")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None, EventRejection(event_id, "invalid_payload", "payload must be an object")
    if event_type == "app.foreground" and collector == "windows_native":
        app = payload.get("app")
        if set(payload) != {"app"} or not _valid_app_identity(app):
            return None, EventRejection(
                event_id,
                "invalid_app_payload",
                "native app.foreground requires only a valid payload.app identity",
            )
    if event_type == "device.input_state":
        threshold = payload.get("idle_threshold_seconds")
        if (
            collector != "windows_native"
            or kind != "desktop"
            or not set(payload).issubset({"status", "idle_threshold_seconds"})
            or payload.get("status") not in {"active", "afk", "locked"}
            or (threshold is not None and (
                isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1
            ))
        ):
            return None, EventRejection(
                event_id,
                "invalid_input_state_payload",
                "device.input_state requires windows_native active, afk, or locked state",
            )
    if event_type == "web.foreground":
        domain = payload.get("domain")
        browser_app = payload.get("browser_app")
        if (
            collector != "browser_extension"
            or kind != "desktop"
            or not set(payload).issubset({"domain", "browser_app"})
            or not isinstance(domain, str)
            or not domain.strip()
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{1,251}[a-z0-9])?", domain) is None
            or "." not in domain
            or (browser_app is not None and not _valid_app_identity(browser_app))
        ):
            return None, EventRejection(
                event_id,
                "invalid_web_payload",
                "web.foreground requires a domain-only payload and optional browser identity",
            )
    if event_type == "health.steps_observation":
        counter_value = payload.get("counter_value")
        counter_session_id = payload.get("counter_session_id")
        sensor_type = payload.get("sensor_type")
        if kind != "android" or collector != "step_counter":
            return None, EventRejection(
                event_id,
                "invalid_steps_source",
                "step observations require android step_counter source",
            )
        if isinstance(counter_value, bool) or not isinstance(counter_value, int) or counter_value < 0:
            return None, EventRejection(
                event_id,
                "invalid_steps_payload",
                "payload.counter_value must be a non-negative integer",
            )
        if not is_uuid(counter_session_id) or sensor_type != "android.step_counter":
            return None, EventRejection(
                event_id,
                "invalid_steps_payload",
                "step observations require a UUID counter_session_id and android.step_counter sensor_type",
            )

    source = {"kind": kind, "collector": collector, "reliability": reliability}
    document: dict[str, Any] = {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "event_type": event_type,
        "source": source,
        "revision": raw_revision,
        "payload": payload,
    }
    if raw_duration is not None:
        document["duration_seconds"] = raw_duration

    activitywatch_payload = payload.get("activitywatch")
    is_activitywatch = (
        event_type == "app.foreground"
        and collector in {"activitywatch", "browser_extension"}
        and isinstance(activitywatch_payload, dict)
    )
    is_native_live_fact = (
        (event_type in {"app.foreground", "device.input_state"} and collector == "windows_native")
        or (event_type == "web.foreground" and collector == "browser_extension")
    )
    mutable = is_activitywatch or is_native_live_fact or event_type in {"location.sample", "location.stay"}
    if event_type == "location.observation":
        mutable = False

    return (
        NormalizedEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            event_type=event_type,
            source=source,
            duration_seconds=raw_duration,
            revision=raw_revision,
            payload=payload,
            document=document,
            content_hash=content_hash(document),
            mutable=mutable,
        ),
        None,
    )


def _valid_app_identity(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    allowed = {"display_name", "package_name", "activity_name", "process_name"}
    if not set(value).issubset(allowed):
        return False
    display_name = value.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 200:
        return False
    for key in ("package_name", "activity_name"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item or len(item) > 255):
            return False
    process_name = value.get("process_name")
    if process_name is not None and (
        not isinstance(process_name, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}(?:\.exe)?", process_name) is None
    ):
        return False
    return True
