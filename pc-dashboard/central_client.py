"""Reliable HTTP uploader from one Life Link client to one central server."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from outbox import Outbox


OpenFunction = Callable[..., Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validated_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("central_base_url must be an absolute HTTP(S) URL")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise ValueError(
            "central_base_url must use HTTPS unless it targets local loopback"
        )
    return base_url.rstrip("/")


def _retry_after(
    value: str | None, *, now: datetime, fallback_seconds: int,
) -> datetime:
    if value:
        try:
            return now + timedelta(seconds=max(0, int(value)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(now, parsed.astimezone(timezone.utc))
            except (TypeError, ValueError, OverflowError):
                pass
    return now + timedelta(seconds=fallback_seconds)


class CentralClient:
    """Upload persisted outbox batches without changing them during retries."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 15,
        opener: OpenFunction = urlopen,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not token:
            raise ValueError("central Bearer token must not be empty")
        self.base_url = _validated_base_url(base_url)
        self.endpoint = self.base_url + "/v1/events/batches"
        self.token = token
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.opener = opener
        self.clock = clock

    def get_shared_settings(self) -> dict[str, Any]:
        """Read shared settings with this registered device credential."""
        return self._shared_settings_request("GET")

    def update_shared_settings(self, changes: int | dict[str, Any]) -> dict[str, Any]:
        """Persist a supported shared setting with this device credential."""
        body = {"day_start_hour": changes} if isinstance(changes, int) and not isinstance(changes, bool) else changes
        allowed = {
            "day_start_hour", "primary_health_device_id", "sleep_local_time",
            "morning_report", "evening_report", "periodic_summary",
        }
        if not isinstance(body, dict) or not body or not set(body) <= allowed:
            raise ValueError("shared settings contain unsupported fields")
        hour = body.get("day_start_hour")
        if "day_start_hour" in body and (isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23):
            raise ValueError("day_start_hour must be an integer from 0 to 23")
        primary = body.get("primary_health_device_id")
        if "primary_health_device_id" in body and primary is not None and (not isinstance(primary, str) or not primary):
            raise ValueError("primary_health_device_id must be a non-empty string or null")
        if "sleep_local_time" in body and not _is_local_clock_time(body["sleep_local_time"]):
            raise ValueError("sleep_local_time must use HH:mm")
        for key in ("morning_report", "evening_report", "periodic_summary"):
            if key in body and not _is_valid_report_schedule(key, body[key]):
                raise ValueError(f"{key} is invalid")
        return self._shared_settings_request("POST", body)

    def _shared_settings_request(
        self, method: str, body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + "/v1/settings/shared", data=data,
            headers=headers, method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw = response.read()
        except HTTPError as error:
            status_code = int(error.code)
            if status_code in {401, 403}:
                category = "auth_error"
            elif status_code == 429:
                category = "rate_limited"
            elif status_code >= 500:
                category = "central_unavailable"
            else:
                category = "central_rejected"
            raise CentralReadError(
                category, f"central shared settings returned HTTP {status_code}",
                http_status=status_code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CentralReadError(
                "central_unavailable", "central shared settings connection failed",
            ) from error
        if status_code != 200:
            raise CentralReadError(
                "central_unavailable" if status_code >= 500 else "central_rejected",
                f"central shared settings returned HTTP {status_code}",
                http_status=status_code,
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CentralReadError(
                "invalid_response", "central shared settings returned invalid JSON",
            ) from error
        return _validate_shared_settings(payload)

    @staticmethod
    def _backoff_seconds(attempt_count: int) -> int:
        return min(3600, max(5, 2 ** min(11, attempt_count + 1)))

    def _record_http_failure(
        self,
        outbox: Outbox,
        batch: dict[str, Any],
        error: HTTPError,
        now: datetime,
    ) -> dict[str, Any]:
        status_code = int(error.code)
        if status_code in {401, 403}:
            category = "auth_error"
            retry_at = now + timedelta(hours=1)
        elif status_code == 429:
            category = "rate_limited"
            retry_at = _retry_after(
                error.headers.get("Retry-After") if error.headers else None,
                now=now,
                fallback_seconds=self._backoff_seconds(batch["attempt_count"]),
            )
        elif 500 <= status_code <= 599:
            category = "server_error"
            retry_at = now + timedelta(
                seconds=self._backoff_seconds(batch["attempt_count"]),
            )
        else:
            category = "client_error"
            retry_at = now + timedelta(hours=6)
        message = f"central server returned HTTP {status_code}"
        outbox.record_attempt(
            batch["batch_id"], error=message, retry_at=retry_at, now=now,
        )
        return {
            "status": category,
            "batch_id": batch["batch_id"],
            "http_status": status_code,
            "error": message,
            "retry_at": retry_at.isoformat().replace("+00:00", "Z"),
        }

    def sync_once(
        self,
        outbox: Outbox,
        device: dict[str, Any],
        *,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        batch = outbox.prepare_batch(
            device, now=now, force_retry=force_retry,
        )
        if batch is None:
            status = outbox.status()
            return {
                "status": "idle" if status["active_batch"] is None else "deferred",
                "batch_id": None,
                "outbox": status,
            }

        body = json.dumps(
            batch["payload"], ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": batch["batch_id"],
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                response_body = response.read()
                status_code = int(getattr(response, "status", 200))
        except HTTPError as error:
            return self._record_http_failure(outbox, batch, error, now)
        except (URLError, TimeoutError, OSError) as error:
            retry_at = now + timedelta(
                seconds=self._backoff_seconds(batch["attempt_count"]),
            )
            message = f"central connection failed: {error}"
            outbox.record_attempt(
                batch["batch_id"], error=message, retry_at=retry_at, now=now,
            )
            return {
                "status": "network_error",
                "batch_id": batch["batch_id"],
                "error": message,
                "retry_at": retry_at.isoformat().replace("+00:00", "Z"),
            }

        if status_code != 200:
            retry_at = now + timedelta(
                seconds=self._backoff_seconds(batch["attempt_count"]),
            )
            message = f"central server returned HTTP {status_code}"
            outbox.record_attempt(
                batch["batch_id"], error=message, retry_at=retry_at, now=now,
            )
            return {
                "status": "server_error" if status_code >= 500 else "client_error",
                "batch_id": batch["batch_id"],
                "http_status": status_code,
                "error": message,
            }
        try:
            acknowledgement = json.loads(response_body.decode("utf-8"))
            if not isinstance(acknowledgement, dict):
                raise ValueError("acknowledgement must be an object")
            if acknowledgement.get("batch_id") != batch["batch_id"]:
                raise ValueError("acknowledgement batch_id does not match")
            confirmed_ids = acknowledgement.get("confirmed_event_ids")
            if not isinstance(confirmed_ids, list) or not all(
                isinstance(event_id, str) for event_id in confirmed_ids
            ):
                raise ValueError("confirmed_event_ids must be a string array")
            batch_event_ids = {
                event["event_id"] for event in batch["payload"]["events"]
            }
            if not set(confirmed_ids) <= batch_event_ids:
                raise ValueError("confirmed_event_ids contains an event outside this batch")
            outbox.record_attempt(batch["batch_id"], now=now)
            summary = outbox.acknowledge(
                batch["batch_id"], acknowledgement, now=now,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError) as error:
            # record_attempt may already have changed inflight to inflight; it
            # remains safe to turn that same immutable batch into a retry.
            retry_at = now + timedelta(
                seconds=self._backoff_seconds(batch["attempt_count"]),
            )
            message = f"invalid central acknowledgement: {error}"
            outbox.record_attempt(
                batch["batch_id"], error=message, retry_at=retry_at, now=now,
            )
            return {
                "status": "invalid_ack",
                "batch_id": batch["batch_id"],
                "error": message,
                "retry_at": retry_at.isoformat().replace("+00:00", "Z"),
            }
        return {
            "status": "ok",
            "batch_id": batch["batch_id"],
            **summary,
            "outbox": outbox.status(),
        }


class CentralReadError(RuntimeError):
    """A central read failed without exposing its credential."""

    def __init__(
        self, category: str, message: str, *, http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status


def _is_local_clock_time(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 5 and value[2] == ":" and value[:2].isdigit() and value[3:].isdigit() and 0 <= int(value[:2]) <= 23 and 0 <= int(value[3:]) <= 59


def _is_valid_report_schedule(kind: str, value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        return False
    if kind == "morning_report":
        mode = value.get("mode")
        return set(value) == {"enabled", "mode", "delay_minutes", "local_time"} and mode in {"after_first_usage", "fixed_time"} and isinstance(value.get("delay_minutes"), int) and not isinstance(value.get("delay_minutes"), bool) and 1 <= value["delay_minutes"] <= 720 and ((mode == "after_first_usage" and value.get("local_time") is None) or (mode == "fixed_time" and _is_local_clock_time(value.get("local_time"))))
    if kind == "evening_report":
        return set(value) == {"enabled", "local_time"} and _is_local_clock_time(value.get("local_time"))
    return set(value) == {"enabled", "start_local_time", "end_local_time", "interval_minutes"} and _is_local_clock_time(value.get("start_local_time")) and _is_local_clock_time(value.get("end_local_time")) and value.get("interval_minutes") in {30, 60, 120, 180, 240}


def _validate_shared_settings(payload: Any) -> dict[str, Any]:
    """Validate the complete, central-authoritative SharedSettings response."""
    required = {"timezone", "day_start_hour", "primary_health_device_id", "sleep_local_time", "ai_display_name", "morning_report", "evening_report", "periodic_summary", "settings_version", "updated_at"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise CentralReadError(
            "invalid_response", "central shared settings response has invalid fields",
        )
    hour = payload["day_start_hour"]
    version = payload["settings_version"]
    updated_at = payload["updated_at"]
    primary = payload["primary_health_device_id"]
    if (
        payload["timezone"] != "Asia/Shanghai"
        or isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23
        or isinstance(version, bool) or not isinstance(version, int) or version < 1
        or primary is not None and (not isinstance(primary, str) or not primary)
        or not _is_local_clock_time(payload["sleep_local_time"])
        or not isinstance(payload["ai_display_name"], str) or not payload["ai_display_name"].strip() or len(payload["ai_display_name"]) > 80
        or not _is_valid_report_schedule("morning_report", payload["morning_report"])
        or not _is_valid_report_schedule("evening_report", payload["evening_report"])
        or not _is_valid_report_schedule("periodic_summary", payload["periodic_summary"])
        or not isinstance(updated_at, str) or not updated_at.endswith("Z")
    ):
        raise CentralReadError(
            "invalid_response", "central shared settings response has invalid values",
        )
    try:
        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise CentralReadError(
            "invalid_response", "central shared settings updated_at is invalid",
        ) from error
    return payload


class CentralReadClient:
    """Authenticated server-side reader for Dashboard compatibility views."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 15,
        opener: OpenFunction = urlopen,
    ) -> None:
        if not token:
            raise ValueError("central read token must not be empty")
        self.base_url = _validated_base_url(base_url)
        self.token = token
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.opener = opener

    def read_view(
        self,
        view: str,
        *,
        from_utc: str,
        to_utc: str,
        local_device_id: str | None = None,
    ) -> dict[str, Any]:
        if view not in {"devices", "usage", "locations"}:
            raise ValueError("central read view is not supported")
        return self._read_view(view, from_utc, to_utc, local_device_id)

    def read_ai_summary(
        self,
        kind: str,
        *,
        from_utc: str,
        to_utc: str,
        local_device_id: str | None = None,
    ) -> str:
        """Fetch a central AI summary Markdown document.

        kind is "usage" or "location"; the central endpoint path is
        /v1/read/ai/{kind}.md.
        """
        if kind not in {"usage", "location"}:
            raise ValueError("central AI summary kind must be usage or location")
        return self._read_text(f"ai/{kind}.md", from_utc, to_utc, local_device_id)

    def read_health_info(self, date: str) -> dict[str, Any]:
        """Read one central health-information snapshot by local date."""
        if not isinstance(date, str):
            raise ValueError("health-info date must use YYYY-MM-DD")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError("health-info date must use YYYY-MM-DD") from error
        request = Request(
            f"{self.base_url}/v1/health-info?{urlencode({'date': date})}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                body = response.read()
        except HTTPError as error:
            status_code = int(error.code)
            category = "auth_error" if status_code in {401, 403} else (
                "rate_limited" if status_code == 429 else "central_unavailable" if status_code >= 500 else "central_rejected"
            )
            raise CentralReadError(category, f"central health-info returned HTTP {status_code}", http_status=status_code) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CentralReadError("central_unavailable", "central health-info connection failed") from error
        if status_code != 200:
            raise CentralReadError("central_unavailable" if status_code >= 500 else "central_rejected", f"central health-info returned HTTP {status_code}", http_status=status_code)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CentralReadError("invalid_response", "central health-info returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise CentralReadError("invalid_response", "central health-info response must be an object")
        return payload

    def read_calendar_days(self, *, from_date: str, to_date: str) -> dict[str, Any]:
        """Read central business-day availability and logical-size summaries."""
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d").date()
            end = datetime.strptime(to_date, "%Y-%m-%d").date()
        except (TypeError, ValueError) as error:
            raise ValueError("calendar-days from and to must use YYYY-MM-DD") from error
        if end < start:
            raise ValueError("calendar-days to must not precede from")
        if (end - start).days + 1 > 42:
            raise ValueError("calendar-days range must not exceed 42 days")
        request = Request(
            f"{self.base_url}/v1/calendar-days?{urlencode({'from': from_date, 'to': to_date})}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                body = response.read()
        except HTTPError as error:
            status_code = int(error.code)
            category = "auth_error" if status_code in {401, 403} else (
                "rate_limited" if status_code == 429 else "central_unavailable" if status_code >= 500 else "central_rejected"
            )
            raise CentralReadError(category, f"central calendar-days returned HTTP {status_code}", http_status=status_code) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CentralReadError("central_unavailable", "central calendar-days connection failed") from error
        if status_code != 200:
            raise CentralReadError("central_unavailable" if status_code >= 500 else "central_rejected", f"central calendar-days returned HTTP {status_code}", http_status=status_code)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CentralReadError("invalid_response", "central calendar-days returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise CentralReadError("invalid_response", "central calendar-days response must be an object")
        return payload

    def read_blacklist_rules(self, *, token: str | None = None) -> list[dict[str, Any]]:
        """Fetch the current blacklist rules from the central service.

        Uses /v1/settings/blacklist-rules — not the /v1/read/ prefix used by
        devices/usage/locations/AI summary calls.

        If *token* is given it is used for this request; otherwise the
        instance's read token is used.
        """
        effective_token = token or self.token
        if not effective_token:
            raise CentralReadError("auth_error", "no credential available for blacklist read")
        request = Request(
            f"{self.base_url}/v1/settings/blacklist-rules",
            headers={
                "Authorization": f"Bearer {effective_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                body = response.read()
        except HTTPError as error:
            status_code = int(error.code)
            if status_code in {401, 403}:
                category = "auth_error"
            elif status_code >= 500:
                category = "central_unavailable"
            else:
                category = "central_rejected"
            raise CentralReadError(
                category,
                f"central blacklist read returned HTTP {status_code}",
                http_status=status_code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CentralReadError(
                "central_unavailable", "central blacklist read connection failed",
            ) from error
        if status_code != 200:
            raise CentralReadError(
                "central_unavailable" if status_code >= 500 else "central_rejected",
                f"central blacklist read returned HTTP {status_code}",
                http_status=status_code,
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CentralReadError(
                "invalid_response", "central blacklist read returned invalid JSON",
            ) from error
        if not isinstance(payload, dict):
            raise CentralReadError(
                "invalid_response", "central blacklist read response must be an object",
            )
        rules = payload.get("rules")
        if not isinstance(rules, list):
            raise CentralReadError("invalid_response", "blacklist-rules response must include a rules array")
        return rules

    def _read_view(
        self,
        suffix: str,
        from_utc: str,
        to_utc: str,
        local_device_id: str | None,
    ) -> dict[str, Any]:
        payload = self._read_payload(suffix, from_utc, to_utc, local_device_id)
        if not isinstance(payload, dict):
            raise CentralReadError(
                "invalid_response", "central read response must be an object",
            )
        return payload

    def _read_text(
        self,
        suffix: str,
        from_utc: str,
        to_utc: str,
        local_device_id: str | None,
    ) -> str:
        payload = self._read_payload(suffix, from_utc, to_utc, local_device_id)
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            return payload
        raise CentralReadError(
            "invalid_response", "central AI summary response must be text",
        )

    def _read_payload(
        self,
        suffix: str,
        from_utc: str,
        to_utc: str,
        local_device_id: str | None,
    ) -> Any:
        query_parameters = {"from": from_utc, "to": to_utc}
        if local_device_id:
            query_parameters["local_device_id"] = local_device_id
        query = urlencode(query_parameters)
        request = Request(
            f"{self.base_url}/v1/read/{suffix}?{query}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json, text/markdown; charset=utf-8",
            },
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                body = response.read()
        except HTTPError as error:
            status_code = int(error.code)
            if status_code in {401, 403}:
                category = "auth_error"
            elif status_code == 404:
                category = "unsupported_view"
            elif status_code == 429:
                category = "rate_limited"
            elif status_code >= 500:
                category = "central_unavailable"
            else:
                category = "central_rejected"
            raise CentralReadError(
                category,
                f"central read returned HTTP {status_code}",
                http_status=status_code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CentralReadError(
                "central_unavailable", "central read connection failed",
            ) from error
        if status_code != 200:
            raise CentralReadError(
                "central_unavailable" if status_code >= 500 else "central_rejected",
                f"central read returned HTTP {status_code}",
                http_status=status_code,
            )
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CentralReadError(
                "invalid_response", "central read response must be UTF-8",
            ) from error
        # If the response looks like JSON, parse it; otherwise treat it as a
        # raw text/markdown body. The AI summary endpoints return markdown.
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError as error:
                raise CentralReadError(
                    "invalid_response", "central read returned invalid JSON",
                ) from error
        return text


class CentralDeviceClient:
    """Authenticated writer for wishes, triggers, and timeline reads using a
    registered device credential (upload token).

    Read endpoints use device token via /v1/ resource auth.
    Write endpoints (POST/PUT/PATCH/DELETE) use the same device token.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 15,
        opener: OpenFunction = urlopen,
    ) -> None:
        if not token:
            raise ValueError("device token must not be empty")
        self.base_url = _validated_base_url(base_url)
        self.token = token
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.opener = opener

    # -----------------------------------------------------------------
    # Wishes
    # -----------------------------------------------------------------
    def list_wishes(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        qs = "?include_archived=true" if include_archived else ""
        return self._request("GET", f"/v1/wishes{qs}")["wishes"]

    def get_wish(self, wish_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/wishes/{wish_id}")

    def create_wish(self, *, request_id: str, text: str,
                    duration_days: int = 3, ai_tracking_enabled: bool = False) -> dict[str, Any]:
        """Create a wish."""
        return self._request("POST", "/v1/wishes", {
            "request_id": request_id,
            "text": text,
            "duration_days": duration_days,
            "ai_tracking_enabled": ai_tracking_enabled,
        })

    def cancel_wish(self, wish_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/wishes/{wish_id}/cancel")

    def complete_wish(self, wish_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/wishes/{wish_id}/complete")

    def assess_wish_day(self, wish_id: str, business_date: str,
                        evaluation: str) -> dict[str, Any]:
        return self._request(
            "PUT", f"/v1/wishes/{wish_id}/days/{business_date}",
            {"evaluation": evaluation},
        )

    # -----------------------------------------------------------------
    # Timeline
    # -----------------------------------------------------------------
    def list_timeline(self, *, from_iso: str | None = None,
                      to_iso: str | None = None, category: str | None = None,
                      wish_id: str | None = None,
                      importance: str | None = None) -> dict[str, Any]:
        params = []
        if from_iso:
            params.append(f"from={from_iso}")
        if to_iso:
            params.append(f"to={to_iso}")
        if category:
            params.append(f"category={category}")
        if wish_id:
            params.append(f"wish_id={wish_id}")
        if importance:
            params.append(f"importance={importance}")
        qs = "?" + "&".join(params) if params else ""
        return self._request("GET", f"/v1/timeline-events{qs}")

    # -----------------------------------------------------------------
    # Triggers
    # -----------------------------------------------------------------
    def list_trigger_types(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/trigger-types")["trigger_types"]

    def list_triggers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/event-triggers")["triggers"]

    def create_trigger(self, *, wish_id: str | None = None,
                       trigger_type: str, parameters: dict[str, Any],
                       interval_minutes: int = 60,
                       enabled: bool = True) -> dict[str, Any]:
        body = {
            "request_id": str(uuid.uuid4()),
            "trigger_type": trigger_type,
            "config_version": 1,
            "parameters": parameters,
            "interval_minutes": interval_minutes,
            "enabled": enabled,
        }
        if wish_id:
            body["wish_id"] = wish_id
        return self._request("POST", "/v1/event-triggers", body)

    def patch_trigger(self, trigger_id: str,
                      patch: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/v1/event-triggers/{trigger_id}", patch)

    def delete_trigger(self, trigger_id: str) -> None:
        self._request("DELETE", f"/v1/event-triggers/{trigger_id}")

    # -----------------------------------------------------------------
    # internal
    # -----------------------------------------------------------------
    def _request_with_status(self, method: str, path: str,
                            body: dict[str, Any] | None = None) -> tuple[Any, int]:
        """Like _request but returns (result, http_status) tuple.
        Does NOT raise on HTTP 4xx/5xx — returns them for the caller to handle."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path, data=data, headers=headers, method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw = response.read()
        except HTTPError as error:
            status_code = int(error.code)
            raw = error.read()
        except (URLError, TimeoutError, OSError) as error:
            raise CentralReadError(
                "central_unavailable",
                f"central {method} {path} connection failed",
            ) from error
        if status_code >= 400:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {
                    "error": "central_rejected",
                    "message": f"central {method} {path} returned HTTP {status_code}",
                }
            return payload, status_code
        if method == "DELETE":
            return None, status_code
        if not raw or not raw.strip():
            # Central can answer a successful mutating action (e.g. a POST
            # compatibility entry for a delete) with an empty 204 body. There
            # is no JSON to parse; the success is the status code itself.
            return None, status_code
        try:
            return json.loads(raw.decode("utf-8")), status_code
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CentralReadError(
                "invalid_response", f"central {method} {path} returned invalid JSON",
            ) from error

    def _request(self, method: str, path: str,
                 body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path, data=data, headers=headers, method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw = response.read()
        except HTTPError as error:
            status_code = int(error.code)
            raise CentralReadError(
                "central_rejected",
                f"central {method} {path} returned HTTP {status_code}",
                http_status=status_code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CentralReadError(
                "central_unavailable",
                f"central {method} {path} connection failed",
            ) from error
        if status_code >= 400:
            raise CentralReadError(
                "central_rejected",
                f"central {method} {path} returned HTTP {status_code}",
                http_status=status_code,
            )
        if method == "DELETE":
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CentralReadError(
                "invalid_response", f"central {method} {path} returned invalid JSON",
            ) from error
