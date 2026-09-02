"""HTTP surface for the independent central context service."""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, urlparse

from .ai_readers import (
    AIReaderCursorExpired,
    AIReaderCursorInvalid,
    AIReaderCursorSuperseded,
    AIReaderInvalidToken,
    AIReaderNotFound,
    AIReaderPairingAlreadyClaimed,
    AIReaderPairingExpired,
    AIReaderPairingInvalid,
    AIReaderTokenExpired,
    is_loopback_address,
    validate_claim_payload as validate_ai_reader_claim_payload,
)
from .config import CentralConfig
from .domain import BatchValidationError, canonical_json, content_hash, utc_timestamp, validate_batch_envelope
from .invitations import (
    EnrollmentConfigurationError,
    claim_invitation,
    remove_persistent_device_credentials,
    validate_claim_payload,
)
from .read_model import parse_read_range
from .media import MediaManager, MediaSettings
from .storage import (
    CentralStore,
    DeviceIdentityConflict,
    IdempotencyConflict,
    InvitationAlreadyClaimed,
    InvitationExpired,
    InvitationInvalid,
    FutureWishDay,
    TriggerConfigurationConflict,
    WishDayNotFound,
    WishDaysIncomplete,
    WishDeleted,
    WishLimitReached,
    WishNotCancellable,
    WishNotCompletable,
)
from .scheduler import MinuteScheduler


class MissingDeviceTokenError(RuntimeError):
    pass


class CentralHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], config: CentralConfig) -> None:
        if not config.token_bindings and not config.allow_empty_tokens:
            raise MissingDeviceTokenError(
                "no device token is configured; run "
                "`python central_server.py init --device-id <stable-device-id>` first"
            )
        self.config = config
        self.store = CentralStore(config.database_path, config.token_bindings)
        # Production startup makes the on-disk config authoritative: tokens that
        # were removed from the config are revoked. Construction itself never
        # does this, so tests/diagnostics that open the DB with an empty binding
        # map cannot accidentally wipe registered devices.
        self.store.reconcile_credentials(config.token_bindings)
        self.scheduler = MinuteScheduler(self.store)
        self.media = MediaManager(MediaSettings.from_config(config))
        super().__init__(server_address, CentralRequestHandler)
        self.scheduler.start()

    def server_close(self) -> None:
        self.scheduler.stop()
        super().server_close()


class CentralRequestHandler(BaseHTTPRequestHandler):
    server: CentralHTTPServer
    server_version = "LifeRadioCentral/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, status_code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # Central responses contain private life-log metadata.  They must not
        # be retained by browsers, proxies, or a future HTTPS tunnel cache.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def send_empty(self, status_code: int) -> None:
        """Send a no-content response without serializing an empty JSON object."""
        self.send_response(status_code)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json_bytes(self, status_code: int, body: bytes) -> None:
        """Send an already-rendered audited JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def send_conditional_json(self, payload: Any) -> None:
        """Send private JSON with an ETag so trusted clients can avoid its body."""
        etag = f'"{content_hash(payload)}"'
        if self.headers.get("If-None-Match", "").strip() == etag:
            self.send_response(304)
            self.send_header("Cache-Control", "no-store")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = canonical_json(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def read_json_body(self) -> tuple[Any | None, str | None]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Content-Length must be an integer"
        if content_length < 1:
            return None, "request body is required"
        if content_length > self.server.config.max_body_bytes:
            return None, "request body is too large"
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "request body must be UTF-8 JSON"

    def bearer_token(self) -> str | None:
        supplied = self.headers.get("Authorization", "")
        if not supplied.startswith("Bearer "):
            return None
        token = supplied.removeprefix("Bearer ")
        return token if token else None

    def _authorize_registered_device(self) -> bool:
        """Allow any registered device credential to manage shared configuration.

        Blacklist rules and shared cross-day settings are the user's own
        global configuration. Any enrolled device may modify them; no new
        token or registration step is required.
        """
        token = self.bearer_token()
        bound_device_id = self.server.store.device_for_token(token) if token else None
        if bound_device_id is None:
            self.send_json(
                401,
                {"error": "invalid_device_token", "message": "missing or invalid device Bearer token"},
            )
            return False
        return True

    def _authorize_read(self) -> bool:
        configured_token = self.server.config.read_token
        if configured_token is None:
            self.send_json(
                503,
                {
                    "error": "read_access_not_configured",
                    "message": "central read access is not configured",
                },
            )
            return False
        supplied_token = self.bearer_token()
        if supplied_token is not None and hmac.compare_digest(supplied_token, configured_token):
            return True
        if (
            supplied_token is not None
            and self.server.store.device_for_token(supplied_token) is not None
        ):
            self.send_json(
                403,
                {
                    "error": "read_access_forbidden",
                    "message": "device upload credentials do not grant read access",
                },
            )
            return False
        self.send_json(
            401,
            {
                "error": "invalid_read_token",
                "message": "missing or invalid read Bearer token",
            },
        )
        return False

    def _authorize_resource_read(self) -> str | None:
        """Authorize v1.7 personal resources without widening /v1/read/* access."""
        token = self.bearer_token()
        if token is None:
            self.send_json(401, {"error": "missing_token", "message": "missing Bearer token"})
            return None
        if self.server.config.read_token is not None and hmac.compare_digest(token, self.server.config.read_token):
            return ""
        device_id = self.server.store.device_for_token(token)
        if device_id is not None:
            return device_id
        self.send_json(401, {"error": "invalid_token", "message": "missing or invalid Bearer token"})
        return None

    def _authorize_device(self) -> str | None:
        token = self.bearer_token()
        bound_device_id = self.server.store.device_for_token(token) if token else None
        if bound_device_id is None:
            self.send_json(
                401,
                {"error": "invalid_token", "message": "missing or invalid Bearer token"},
            )
            return None
        return bound_device_id

    def _handle_media_items(self) -> None:
        if not self._authorize_read():
            return
        self.send_json(200, {"items": self.server.media.list_items()})

    def _handle_media_jobs_get(self) -> None:
        if self._authorize_device() is None:
            return
        self.send_json(200, {"jobs": self.server.media.list_jobs()})

    def _handle_media_job_submit(self) -> None:
        if self._authorize_device() is None:
            return
        payload, body_error = self.read_json_body()
        if body_error or not isinstance(payload, dict):
            self.send_json(400, {"error": "invalid_request", "message": body_error or "request body must be a JSON object"})
            return
        url = payload.get("url")
        if not isinstance(url, str):
            self.send_json(400, {"error": "invalid_request", "message": "url must be a string"})
            return
        job, error, status = self.server.media.submit(url)
        if error is not None:
            self.send_json(status or 400, {"error": "media_job_rejected", "message": error})
            return
        self.send_json(202, {"job": job})

    def _handle_media_open_folder(self) -> None:
        if self._authorize_device() is None:
            return
        ok, error = self.server.media.open_folder()
        if not ok:
            self.send_json(500, {"error": "open_folder_failed", "message": error})
            return
        self.send_json(200, {"status": "opened"})

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/v1/devices/"):
            self._handle_device_rename(path)
            return
        if path.startswith("/v1/event-triggers/"):
            self._handle_trigger_patch(path)
            return
        if path.startswith("/v1/wishes/") and "/days/" in path:
            self.send_json(405, {"error": "method_not_allowed", "message": "use PUT for wish day assessment"})
            return
        if path.startswith("/v1/wishes/"):
            self._handle_wish_patch(path)
            return
        if path == "/v1/settings/shared":
            self._handle_shared_settings_update()
            return
        if path.startswith("/v1/settings/blacklist-rules/"):
            self._handle_blacklist_rules_update(path)
            return
        self.send_json(405, {"error": "method_not_allowed", "message": "PATCH not supported"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/v1/ai-readers/"):
            self._handle_ai_reader_revoke(path)
            return
        if path.startswith("/v1/devices/"):
            self._handle_device_retire(path)
            return
        if path.startswith("/v1/event-triggers/"):
            self._handle_trigger_delete(path)
            return
        if path.startswith("/v1/settings/blacklist-rules/"):
            self._handle_blacklist_rules_delete(path)
            return
        if path.startswith("/v1/wishes/"):
            self._handle_wish_delete(path)
            return
        self.send_json(405, {"error": "method_not_allowed", "message": "DELETE not supported"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/read/ai/context":
            self._handle_ai_reader_context(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            return
        if parsed.path == "/v1/read/ai/updates":
            self._handle_ai_reader_updates(parse_qs(parsed.query, keep_blank_values=True))
            return
        if parsed.path == "/v1/ai-readers":
            self._handle_ai_readers_list()
            return
        if parsed.path.startswith("/v1/ai-readers/") and parsed.path.endswith(
            "/process-status"
        ):
            self._handle_ai_reader_process_status(parsed.path)
            return
        if parsed.path.startswith("/v1/ai-readers/") and parsed.path.endswith(
            "/context-preview"
        ):
            self._handle_ai_reader_context_preview(
                parsed.path, parse_qs(parsed.query, keep_blank_values=True)
            )
            return
        if parsed.path.startswith("/v1/ai-readers/") and parsed.path.endswith(
            "/access-logs"
        ):
            self._handle_ai_reader_access_logs(
                parsed.path, parse_qs(parsed.query)
            )
            return
        if parsed.path in {"/v1/read/devices", "/v1/read/usage", "/v1/read/locations"}:
            self._handle_read(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path in {"/v1/read/ai/usage.md", "/v1/read/ai/location.md"}:
            self._handle_read_ai(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path == "/v1/media/items":
            self._handle_media_items()
            return
        if parsed.path == "/v1/media/jobs":
            self._handle_media_jobs_get()
            return
        if parsed.path == "/v1/settings/blacklist-rules":
            self._handle_blacklist_rules_get()
            return
        if parsed.path == "/v1/settings/shared":
            self._handle_shared_settings_get()
            return
        if parsed.path == "/v1/event-background":
            self._handle_event_background(parse_qs(parsed.query))
            return
        if parsed.path == "/v1/calendar-days":
            self._handle_calendar_days(parse_qs(parsed.query, keep_blank_values=True))
            return
        if parsed.path == "/v1/devices":
            self._handle_devices_list()
            return
        if parsed.path == "/v1/wishes":
            self._handle_wishes_list(parse_qs(parsed.query)); return
        if parsed.path.startswith("/v1/wishes/"):
            self._handle_wish_get(parsed.path); return
        if parsed.path == "/v1/timeline-events":
            self._handle_timeline_get(parse_qs(parsed.query)); return
        if parsed.path == "/v1/trigger-types":
            self._handle_trigger_types(); return
        if parsed.path == "/v1/event-triggers":
            self._handle_trigger_list(); return
        if parsed.path == "/v1/health-info":
            self._handle_health_info(parse_qs(parsed.query)); return
        if parsed.path != "/v1/health":
            self.send_json(404, {"error": "not_found", "message": "endpoint not found"})
            return
        try:
            self.server.store.ping()
        except sqlite3.Error:
            self.send_json(503, {"status": "unavailable", "api_version": "v1"})
            return
        self.send_json(
            200,
            {
                "status": "ok",
                "server_time": utc_timestamp(),
                "api_version": "v1",
                "role": "central",
                "capabilities": {
                    "legacy_push": False,
                    "activitywatch_collection": False,
                    "tailscale_discovery": False,
                    "outbound_replication": False,
                    "client_enrollment_claim": True,
                    "ai_reader_passive_read": True,
                },
            },
        )

    def _handle_health_info(self, params: dict[str, list[str]]) -> None:
        if self._authorize_resource_read() is None:
            return
        raw_date = params.get("date", [None])[0]
        try:
            if not isinstance(raw_date, str) or len(raw_date) != 10:
                raise ValueError
            target_date = date.fromisoformat(raw_date)
            if target_date.isoformat() != raw_date:
                raise ValueError
            payload = self.server.store.read_health_info(target_date)
        except ValueError as error:
            message = str(error) or "date must be a non-future YYYY-MM-DD value"
            self.send_json(400, {"error": "invalid_date", "message": message})
            return
        except sqlite3.Error:
            self.send_json(500, {"error": "storage_error", "message": "health information query failed"})
            return
        self.send_json(200, payload)

    def _handle_event_background(self, params: dict[str, list[str]]) -> None:
        if self._authorize_resource_read() is None:
            return
        raw = params.get("business_date", [None])[0]
        try:
            if raw is not None and (not isinstance(raw, str) or date.fromisoformat(raw).isoformat() != raw):
                raise ValueError("business_date must be YYYY-MM-DD")
            self.send_json(200, self.server.store.event_background(raw))
        except ValueError as error:
            self.send_json(400, {"error": "invalid_business_date", "message": str(error)})

    def _handle_calendar_days(self, params: dict[str, list[str]]) -> None:
        if self._authorize_resource_read() is None:
            return
        if set(params) != {"from", "to"} or any(len(values) != 1 for values in params.values()):
            self.send_json(400, {"error": "invalid_calendar_range", "message": "from and to are required exactly once"})
            return
        from_date, to_date = params["from"][0], params["to"][0]
        try:
            for value in (from_date, to_date):
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError("from and to must be YYYY-MM-DD")
            self.send_json(200, self.server.store.calendar_days(from_date, to_date))
        except ValueError as error:
            self.send_json(400, {"error": "invalid_calendar_range", "message": str(error)})
        except sqlite3.Error:
            self.send_json(500, {"error": "storage_error", "message": "calendar query failed"})

    def _handle_read(self, path: str, params: dict[str, list[str]]) -> None:
        if not self._authorize_read():
            return
        try:
            start, end = parse_read_range(
                params.get("from", [None])[0],
                params.get("to", [None])[0],
            )
            local_device_id = params.get("local_device_id", [None])[0]
            if path == "/v1/read/devices":
                payload = self.server.store.read_devices(start, end, local_device_id)
            elif path == "/v1/read/locations":
                payload = self.server.store.read_locations(start, end, local_device_id)
            else:
                payload = self.server.store.read_usage(start, end, local_device_id)
        except ValueError as error:
            self.send_json(
                400,
                {"error": "invalid_range", "message": str(error)},
            )
            return
        except sqlite3.Error:
            self.send_json(
                500,
                {"error": "storage_error", "message": "central read operation failed"},
            )
            return
        self.send_json(200, payload)

    def _handle_read_ai(self, path: str, params: dict[str, list[str]]) -> None:
        if not self._authorize_read():
            return
        try:
            start, end = parse_read_range(
                params.get("from", [None])[0],
                params.get("to", [None])[0],
            )
            local_device_id = params.get("local_device_id", [None])[0]
            if path == "/v1/read/ai/usage.md":
                payload = self.server.store.read_usage(start, end, local_device_id)
                markdown = payload.get("ai_summary") if isinstance(payload, dict) else None
            else:
                payload = self.server.store.read_locations(start, end, local_device_id)
                markdown = payload.get("ai_summary") if isinstance(payload, dict) else None
        except ValueError as error:
            self.send_json(
                400,
                {"error": "invalid_range", "message": str(error)},
            )
            return
        except sqlite3.Error:
            self.send_json(
                500,
                {"error": "storage_error", "message": "central read operation failed"},
            )
            return
        if not isinstance(markdown, str):
            self.send_json(
                500,
                {"error": "summary_unavailable", "message": "central projection did not produce an AI summary"},
            )
            return
        body = markdown.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/ai-readers/pairings/claim":
            self._handle_ai_reader_claim()
            return
        if path == "/v1/ai-readers/pairings":
            self._handle_ai_reader_pairing_create()
            return
        if path.startswith("/v1/ai-readers/") and path.endswith(
            "/clear-reading-progress"
        ):
            self._handle_ai_reader_clear_progress(path)
            return
        if path.startswith("/v1/devices/") and path.endswith("/delete"):
            self._handle_device_retire(path.removesuffix("/delete")); return
        if path.startswith("/v1/devices/"):
            # Transport-compatible alias for HTTPS tunnels that reject PATCH.
            self._handle_device_rename(path); return
        if path == "/v1/wishes":
            self._handle_wish_create(); return
        if path.startswith("/v1/wishes/") and path.endswith("/cancel"):
            self._handle_wish_cancel(path); return
        if path.startswith("/v1/wishes/") and path.endswith("/delete"):
            self._handle_wish_delete(path.removesuffix("/delete")); return
        if path.startswith("/v1/wishes/") and path.endswith("/complete"):
            self._handle_wish_complete(path); return
        if path.startswith("/v1/wishes/"):
            # Transport-compatible alias for HTTPS tunnels that reject PATCH.
            self._handle_wish_patch(path); return
        if path == "/v1/event-triggers":
            self._handle_trigger_create(); return
        if path.startswith("/v1/event-triggers/") and path.endswith("/delete"):
            self._handle_trigger_delete(path.removesuffix("/delete")); return
        if path.startswith("/v1/event-triggers/"):
            # Transport-compatible alias for HTTPS tunnels that reject PATCH.
            self._handle_trigger_patch(path); return
        if path == "/v1/settings/shared":
            # Some deployed HTTPS tunnels reject PATCH even though the central
            # service supports it. POST is the transport-compatible write path.
            self._handle_shared_settings_update()
            return
        if path == "/v1/enrollments/claim":
            self._handle_enrollment_claim()
            return
        if path == "/v1/media/jobs":
            self._handle_media_job_submit()
            return
        if path == "/v1/media/open-folder":
            self._handle_media_open_folder()
            return
        if path == "/v1/settings/blacklist-rules":
            self._handle_blacklist_rules_create()
            return
        if path.startswith("/v1/settings/blacklist-rules/"):
            # Transport-compatible alias for HTTPS tunnels that reject PATCH.
            self._handle_blacklist_rules_update(path)
            return
        if path != "/v1/events/batches":
            self.send_json(404, {"error": "not_found", "message": "endpoint not found"})
            return

        token = self.bearer_token()
        bound_device_id = self.server.store.device_for_token(token) if token else None
        if bound_device_id is None:
            self.send_json(
                401,
                {"error": "invalid_token", "message": "missing or invalid Bearer token"},
            )
            return

        payload, body_error = self.read_json_body()
        if body_error:
            self.send_json(400, {"error": "invalid_request", "message": body_error})
            return
        try:
            batch = validate_batch_envelope(
                payload,
                self.headers.get("Idempotency-Key"),
                max_events=self.server.config.max_events_per_batch,
            )
        except BatchValidationError as error:
            self.send_json(400, {"error": "invalid_batch", "message": str(error)})
            return

        if batch.device.device_id != bound_device_id:
            self.send_json(
                403,
                {
                    "error": "device_token_mismatch",
                    "message": "Bearer token is not bound to device.device_id",
                },
            )
            return

        request_hash = content_hash(batch.request_payload)
        try:
            acknowledgement = self.server.store.ingest(batch, request_hash)
        except IdempotencyConflict as error:
            self.send_json(
                409,
                {"error": "idempotency_conflict", "message": str(error)},
            )
            return
        except DeviceIdentityConflict as error:
            self.send_json(
                409,
                {"error": "device_identity_conflict", "message": str(error)},
            )
            return
        except sqlite3.Error:
            self.send_json(
                500,
                {"error": "storage_error", "message": "central storage operation failed"},
            )
            return
        self.send_json(200, acknowledgement)

    @staticmethod
    def _ai_reader_id_from_path(path: str, suffix: str = "") -> str | None:
        prefix = "/v1/ai-readers/"
        if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
            return None
        reader_id = path[len(prefix):]
        if suffix:
            reader_id = reader_id[: -len(suffix)]
        if not reader_id or "/" in reader_id:
            return None
        try:
            parsed = uuid.UUID(reader_id)
        except ValueError:
            return None
        return reader_id if reader_id == str(parsed) else None

    def _handle_ai_reader_claim(self) -> None:
        if not is_loopback_address(str(self.client_address[0])):
            self.send_json(
                403,
                {
                    "error": "ai_reader_claim_loopback_required",
                    "message": "AI reader pairing claims are accepted only from loopback",
                },
            )
            return
        pairing_token = self.bearer_token()
        if not pairing_token:
            self.send_json(
                401,
                {
                    "error": "invalid_ai_reader_pairing",
                    "message": "missing or invalid pairing Bearer token",
                },
            )
            return
        payload, body_error = self.read_json_body()
        if body_error:
            self.send_json(
                400, {"error": "invalid_ai_reader_claim", "message": body_error}
            )
            return
        try:
            claim = validate_ai_reader_claim_payload(payload)
            profile = self.server.store.ai_readers.claim_pairing(
                pairing_token=pairing_token, claim=claim
            )
        except ValueError as error:
            self.send_json(
                400,
                {"error": "invalid_ai_reader_claim", "message": str(error)},
            )
            return
        except AIReaderPairingInvalid as error:
            self.send_json(401, {"error": error.error_code, "message": str(error)})
            return
        except AIReaderPairingExpired as error:
            self.send_json(410, {"error": error.error_code, "message": str(error)})
            return
        except AIReaderPairingAlreadyClaimed as error:
            self.send_json(409, {"error": error.error_code, "message": str(error)})
            return
        except sqlite3.Error:
            self.send_json(
                500,
                {
                    "error": "ai_reader_storage_error",
                    "message": "AI reader pairing could not be completed",
                },
            )
            return
        self.send_json(200, profile)

    def _handle_ai_reader_pairing_create(self) -> None:
        if not self._authorize_registered_device():
            return
        try:
            created = self.server.store.ai_readers.create_pairing(
                claim_url=(
                    f"http://127.0.0.1:{self.server.server_address[1]}"
                    "/v1/ai-readers/pairings/claim"
                ),
                central_display_name="Life Link Central",
            )
        except (ValueError, sqlite3.Error) as error:
            self.send_json(
                500,
                {"error": "ai_reader_pairing_create_failed", "message": str(error)},
            )
            return
        self.send_json(
            201,
            {
                "pairing_text": created.text,
                "expires_at": created.expires_at,
                "central_instance_id": created.central_instance_id,
            },
        )

    def _handle_ai_reader_clear_progress(self, path: str) -> None:
        if not self._authorize_registered_device():
            return
        reader_id = self._ai_reader_id_from_path(
            path, "/clear-reading-progress"
        )
        if reader_id is None:
            self.send_json(404, {"error": "ai_reader_not_found"})
            return
        try:
            reader = self.server.store.ai_readers.clear_reading_progress(reader_id)
        except AIReaderNotFound as error:
            self.send_json(404, {"error": error.error_code, "message": str(error)})
            return
        self.send_json(200, {"reader": reader})

    def _handle_ai_reader_context(self, params: dict[str, list[str]]) -> None:
        unsupported = set(params) - {
            "business_date", "cursor", "understanding_version", "view"
        }
        if (
            unsupported
            or any(len(values) != 1 for values in params.values())
            or any(values == [""] for values in params.values())
        ):
            self.send_json(
                400,
                {
                    "error": "invalid_ai_context_request",
                    "message": "only one business_date, cursor, understanding_version and view are supported",
                },
            )
            return
        try:
            requested_business_date = params.get("business_date", [None])[0]
            if requested_business_date is not None:
                if date.fromisoformat(requested_business_date).isoformat() != requested_business_date:
                    raise ValueError("business_date must be YYYY-MM-DD")
            requested_cursor = params.get("cursor", [None])[0]
            if requested_cursor is not None and len(requested_cursor) > 2048:
                raise ValueError("cursor is too long")
            understanding_version = params.get("understanding_version", [None])[0]
            if understanding_version is not None and len(understanding_version) > 100:
                raise ValueError("understanding_version is too long")
            view = params.get("view", ["compact"])[0]
            if view not in {"full", "compact"}:
                raise ValueError("view must be full or compact")
            reader = self.server.store.ai_readers.authenticate(self.bearer_token())
            served = self.server.store.ai_readers.serve_context(
                reader,
                cursor=requested_cursor,
                business_date=requested_business_date,
                known_understanding_version=understanding_version,
                view=view,
            )
        except ValueError as error:
            self.send_json(
                400,
                {"error": "invalid_ai_context_request", "message": str(error)},
            )
            return
        except (AIReaderInvalidToken, AIReaderTokenExpired) as error:
            self.send_json(401, {"error": error.error_code, "message": str(error)})
            return
        except AIReaderCursorInvalid as error:
            self.send_json(400, {"error": error.error_code, "message": str(error)})
            return
        except AIReaderCursorExpired as error:
            self.send_json(410, {"error": error.error_code, "message": str(error)})
            return
        except AIReaderCursorSuperseded as error:
            self.send_json(409, {"error": error.error_code, "message": str(error)})
            return
        except sqlite3.Error:
            self.send_json(
                500,
                {
                    "error": "ai_reader_storage_error",
                    "message": "AI context could not be prepared and audited",
                },
            )
            return
        self.send_json_bytes(200, served.body)

    def _handle_ai_reader_updates(self, params: dict[str, list[str]]) -> None:
        if set(params) - {"cursor"} or any(len(values) != 1 for values in params.values()):
            self.send_json(400, {"error": "invalid_ai_updates_request"})
            return
        cursor = params.get("cursor", [None])[0]
        try:
            reader = self.server.store.ai_readers.authenticate(self.bearer_token(), touch=False)
            payload = self.server.store.ai_readers.check_updates(reader, cursor=cursor)
        except (AIReaderInvalidToken, AIReaderTokenExpired) as error:
            self.send_json(401, {"error": error.error_code, "message": str(error)}); return
        except AIReaderCursorInvalid as error:
            self.send_json(400, {"error": error.error_code, "message": str(error)}); return
        except AIReaderCursorExpired as error:
            self.send_json(410, {"error": error.error_code, "message": str(error)}); return
        except AIReaderCursorSuperseded as error:
            self.send_json(409, {"error": error.error_code, "message": str(error)}); return
        self.send_json(200, payload)

    def _handle_ai_readers_list(self) -> None:
        if not self._authorize_registered_device():
            return
        self.send_json(
            200,
            {
                "central_instance_id": self.server.store.ai_readers.central_instance_id(),
                "readers": self.server.store.ai_readers.list_readers(),
            },
        )

    def _handle_ai_reader_process_status(self, path: str) -> None:
        if not self._authorize_registered_device():
            return
        reader_id = self._ai_reader_id_from_path(path, "/process-status")
        if reader_id is None:
            self.send_json(404, {"error": "ai_reader_not_found"})
            return
        try:
            status = self.server.store.ai_readers.process_status(reader_id)
        except AIReaderNotFound as error:
            self.send_json(404, {"error": error.error_code, "message": str(error)})
            return
        self.send_json(200, status)

    def _handle_ai_reader_context_preview(
        self, path: str, params: dict[str, list[str]]
    ) -> None:
        if not self._authorize_registered_device():
            return
        reader_id = self._ai_reader_id_from_path(path, "/context-preview")
        if reader_id is None:
            self.send_json(404, {"error": "ai_reader_not_found"})
            return
        if (
            set(params) - {"view"}
            or any(len(values) != 1 for values in params.values())
            or any(values == [""] for values in params.values())
        ):
            self.send_json(
                400,
                {
                    "error": "invalid_ai_context_preview_request",
                    "message": "only one non-empty view is supported",
                },
            )
            return
        view = params.get("view", ["compact"])[0]
        try:
            payload = self.server.store.ai_readers.preview_next_context(
                reader_id, view=view
            )
        except ValueError as error:
            self.send_json(
                400,
                {
                    "error": "invalid_ai_context_preview_request",
                    "message": str(error),
                },
            )
            return
        except AIReaderNotFound as error:
            self.send_json(404, {"error": error.error_code, "message": str(error)})
            return
        except (AIReaderInvalidToken, AIReaderTokenExpired) as error:
            self.send_json(409, {"error": error.error_code, "message": str(error)})
            return
        self.send_json(200, payload)

    def _handle_ai_reader_access_logs(
        self, path: str, params: dict[str, list[str]]
    ) -> None:
        if not self._authorize_registered_device():
            return
        reader_id = self._ai_reader_id_from_path(path, "/access-logs")
        if reader_id is None:
            self.send_json(404, {"error": "ai_reader_not_found"})
            return
        if set(params) - {"limit"} or len(params.get("limit", [None])) != 1:
            self.send_json(
                400, {"error": "invalid_limit", "message": "invalid limit"}
            )
            return
        try:
            limit = int(params.get("limit", ["100"])[0])
            logs = self.server.store.ai_readers.list_access_logs(
                reader_id, limit=limit
            )
        except (TypeError, ValueError) as error:
            self.send_json(400, {"error": "invalid_limit", "message": str(error)})
            return
        except AIReaderNotFound as error:
            self.send_json(404, {"error": error.error_code, "message": str(error)})
            return
        self.send_json(200, {"reader_id": reader_id, "logs": logs})

    def _handle_ai_reader_revoke(self, path: str) -> None:
        if not self._authorize_registered_device():
            return
        reader_id = self._ai_reader_id_from_path(path)
        if reader_id is None or not self.server.store.ai_readers.revoke_reader(
            reader_id
        ):
            self.send_json(404, {"error": "ai_reader_not_found"})
            return
        self.send_empty(204)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/v1/wishes/") and "/days/" in path:
            self._handle_wish_assessment(path); return
        self.send_json(405, {"error": "method_not_allowed", "message": "PUT not supported"})

    @staticmethod
    def _uuid(value: Any) -> bool:
        try:
            uuid.UUID(str(value)); return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _device_id_from_path(path: str) -> str | None:
        device_id = path.removeprefix("/v1/devices/")
        if not device_id or "/" in device_id or len(device_id) > 200:
            return None
        return device_id

    def _handle_devices_list(self) -> None:
        source_device_id = self._authorize_resource_read()
        if source_device_id is None:
            return
        devices = self.server.store.list_managed_devices()
        for device in devices:
            device["is_current"] = bool(source_device_id) and device["device_id"] == source_device_id
        self.send_json(200, {"devices": devices})

    def _handle_device_rename(self, path: str) -> None:
        source_device_id = self._authorize_device()
        if source_device_id is None:
            return
        device_id = self._device_id_from_path(path)
        payload, error = self.read_json_body()
        if (
            device_id is None or error or not isinstance(payload, dict)
            or set(payload) != {"display_name"}
        ):
            self.send_json(400, {"error": "invalid_device_update", "message": error or "body must contain only display_name"})
            return
        display_name = payload.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 100:
            self.send_json(400, {"error": "invalid_display_name", "message": "display_name must contain 1 to 100 characters"})
            return
        result = self.server.store.rename_device(device_id, display_name)
        if result is None:
            self.send_json(404, {"error": "device_not_found", "message": "active device not found"})
            return
        result["is_current"] = result["device_id"] == source_device_id
        self.send_json(200, result)

    def _handle_device_retire(self, path: str) -> None:
        source_device_id = self._authorize_device()
        if source_device_id is None:
            return
        device_id = self._device_id_from_path(path)
        if device_id is None:
            self.send_json(404, {"error": "device_not_found", "message": "device not found"})
            return
        if device_id == source_device_id:
            self.send_json(409, {"error": "cannot_delete_current_device", "message": "the requesting device cannot delete itself"})
            return
        if self.server.config.config_path is None:
            self.send_json(409, {"error": "credential_source_not_mutable", "message": "device deletion requires the central external config file"})
            return
        retired = self.server.store.retire_device(device_id)
        if retired is None:
            self.send_json(404, {"error": "device_not_found", "message": "device not found"})
            return
        if retired is False:
            # Device already retired; keep credentials removal idempotent.
            try:
                remove_persistent_device_credentials(self.server.config, device_id)
            except (EnrollmentConfigurationError, OSError, ValueError):
                pass
            self.send_empty(204)
            return
        try:
            remove_persistent_device_credentials(self.server.config, device_id)
        except (EnrollmentConfigurationError, OSError, ValueError):
            # SQLite retirement and revocation are deliberately retained. A
            # retry from another active device can finish config cleanup.
            self.send_json(500, {"error": "credential_cleanup_failed", "message": "device was retired but persistent credential cleanup must be retried"})
            return
        self.send_empty(204)

    def _handle_wishes_list(self, params: dict[str, list[str]]) -> None:
        if self._authorize_resource_read() is None: return
        raw = params.get("include_archived", ["false"])[0]
        if raw not in {"true", "false"}:
            self.send_json(400, {"error": "invalid_request", "message": "include_archived must be boolean"}); return
        self.send_json(200, {"wishes": self.server.store.list_wishes(include_archived=raw == "true")})

    def _handle_wish_get(self, path: str) -> None:
        if self._authorize_resource_read() is None: return
        wish_id = path.rsplit("/", 1)[-1]
        if not self._uuid(wish_id): self.send_json(404, {"error": "wish_not_found"}); return
        wish = self.server.store.get_wish(wish_id)
        if wish is None: self.send_json(404, {"error": "wish_not_found"}); return
        self.send_json(200, wish)

    def _handle_wish_create(self) -> None:
        source = self._authorize_device()
        if source is None: return
        payload, error = self.read_json_body()
        allowed = {"request_id", "text", "duration_days", "ai_tracking_enabled"}
        if error or not isinstance(payload, dict) or set(payload) - allowed or not {"request_id", "text"} <= set(payload):
            self.send_json(400, {"error": "invalid_wish", "message": error or "invalid wish body"}); return
        request_id, text = payload["request_id"], payload["text"]
        duration, ai = payload.get("duration_days", 3), payload.get("ai_tracking_enabled", False)
        if not self._uuid(request_id) or not isinstance(text, str) or not text.strip() or len(text.strip()) > 30 or duration not in {3, 7} or not isinstance(ai, bool):
            self.send_json(400, {"error": "invalid_wish", "message": "invalid request_id, text, duration_days, or ai_tracking_enabled"}); return
        normalized = {"request_id": request_id, "text": text.strip(), "duration_days": duration, "ai_tracking_enabled": ai}
        try:
            wish = self.server.store.create_wish(request_id=request_id, request_hash=content_hash(normalized), text=text.strip(), duration_days=duration, ai_tracking_enabled=ai, source_device_id=source)
        except WishLimitReached as exc: self.send_json(409, {"error": "unarchived_wish_limit_reached", "message": str(exc)}); return
        except WishDeleted as exc: self.send_json(410, {"error": "wish_deleted", "message": str(exc)}); return
        except IdempotencyConflict as exc: self.send_json(409, {"error": "idempotency_conflict", "message": str(exc)}); return
        self.send_json(201, wish)

    def _handle_wish_patch(self, path: str) -> None:
        if self._authorize_device() is None: return
        wish_id = path.removeprefix("/v1/wishes/")
        if "/" in wish_id or not self._uuid(wish_id):
            self.send_json(404, {"error": "wish_not_found"}); return
        payload, error = self.read_json_body()
        if error or not isinstance(payload, dict) or set(payload) != {"text"}:
            self.send_json(400, {"error": "invalid_wish", "message": error or "PATCH body must contain only text"}); return
        text = payload["text"]
        if not isinstance(text, str) or not (1 <= len(text.strip()) <= 30):
            self.send_json(400, {"error": "invalid_wish", "message": "text must contain 1 to 30 characters after trimming"}); return
        wish = self.server.store.patch_wish_text(wish_id, text.strip())
        if wish is None: self.send_json(404, {"error": "wish_not_found"}); return
        self.send_json(200, wish)

    def _handle_wish_delete(self, path: str) -> None:
        if self._authorize_device() is None: return
        wish_id = path.removeprefix("/v1/wishes/")
        if "/" in wish_id or not self._uuid(wish_id): self.send_json(404, {"error": "wish_not_found"}); return
        deleted = self.server.store.delete_wish(wish_id)
        if deleted is None: self.send_json(404, {"error": "wish_not_found"}); return
        self.send_empty(204)

    def _handle_wish_cancel(self, path: str) -> None:
        source = self._authorize_device()
        if source is None: return
        wish_id = path.removeprefix("/v1/wishes/").removesuffix("/cancel")
        if not self._uuid(wish_id): self.send_json(404, {"error": "wish_not_found"}); return
        try: wish = self.server.store.cancel_wish(wish_id, source_device_id=source)
        except WishNotCancellable as exc: self.send_json(409, {"error": "wish_not_cancellable", "message": str(exc)}); return
        if wish is None: self.send_json(404, {"error": "wish_not_found"}); return
        self.send_json(200, wish)

    def _handle_wish_complete(self, path: str) -> None:
        source = self._authorize_device()
        if source is None: return
        wish_id = path.removeprefix("/v1/wishes/").removesuffix("/complete")
        if not self._uuid(wish_id): self.send_json(404, {"error": "wish_not_found"}); return
        try:
            wish = self.server.store.complete_wish(wish_id, source_device_id=source)
        except WishDaysIncomplete as exc:
            self.send_json(409, {
                "error": "wish_days_incomplete",
                "message": "all wish days must be assessed before manual completion",
                "missing_business_dates": exc.missing_business_dates,
            }); return
        except WishNotCompletable as exc:
            self.send_json(409, {"error": "wish_not_completable", "message": str(exc)}); return
        if wish is None: self.send_json(404, {"error": "wish_not_found"}); return
        self.send_json(200, wish)

    def _handle_wish_assessment(self, path: str) -> None:
        source = self._authorize_device()
        if source is None: return
        parts = path.split("/")
        if len(parts) != 6 or not self._uuid(parts[3]): self.send_json(404, {"error": "wish_not_found"}); return
        wish_id, business_date = parts[3], parts[5]
        payload, error = self.read_json_body()
        if error or not isinstance(payload, dict) or set(payload) != {"evaluation"} or payload.get("evaluation") not in {"completed", "not_completed"}:
            self.send_json(400, {"error": "invalid_assessment", "message": error or "evaluation must be completed or not_completed"}); return
        try: day = self.server.store.assess_wish_day(wish_id=wish_id, business_date=business_date, evaluation=payload["evaluation"], source_device_id=source)
        except FutureWishDay as exc: self.send_json(409, {"error": "future_wish_day", "message": str(exc)}); return
        except WishDayNotFound as exc: self.send_json(400, {"error": "invalid_assessment", "message": str(exc)}); return
        except ValueError as exc: self.send_json(400, {"error": "invalid_assessment", "message": str(exc)}); return
        if day is None:
            self.send_json(404, {"error": "wish_not_found"}); return
        self.send_json(200, day)

    def _handle_timeline_get(self, params: dict[str, list[str]]) -> None:
        if self._authorize_resource_read() is None: return
        try: start, end = parse_read_range(params.get("from", [None])[0], params.get("to", [None])[0])
        except ValueError as exc: self.send_json(400, {"error": "invalid_range", "message": str(exc)}); return
        category, wish_id, importance = params.get("category", [None])[0], params.get("wish_id", [None])[0], params.get("importance", [None])[0]
        if category not in {None, "wish", "trigger", "device", "user", "system"} or importance not in {None, "low", "normal", "high"} or (wish_id is not None and not self._uuid(wish_id)):
            self.send_json(400, {"error": "invalid_filter", "message": "invalid timeline filter"}); return
        payload = self.server.store.list_timeline(
            start, end, category=category, wish_id=wish_id, importance=importance,
        )
        self.send_conditional_json(payload)

    def _handle_trigger_types(self) -> None:
        if self._authorize_resource_read() is None: return
        self.send_json(200, {"trigger_types": self.server.store.trigger_types()})

    def _handle_trigger_list(self) -> None:
        if self._authorize_resource_read() is None: return
        self.send_json(200, {"triggers": self.server.store.list_triggers()})

    def _handle_trigger_create(self) -> None:
        if self._authorize_device() is None: return
        payload, error = self.read_json_body(); allowed = {"request_id", "wish_id", "trigger_type", "config_version", "parameters", "interval_minutes", "enabled"}
        if error or not isinstance(payload, dict) or set(payload) - allowed or not {"request_id", "trigger_type", "config_version", "parameters", "interval_minutes"} <= set(payload) or not self._uuid(payload.get("request_id")) or (payload.get("wish_id") is not None and not self._uuid(payload.get("wish_id"))) or not isinstance(payload.get("enabled", True), bool):
            self.send_json(400, {"error": "invalid_trigger", "message": error or "invalid trigger body"}); return
        normalized = {key: payload.get(key, True if key == "enabled" else None) for key in ("request_id", "wish_id", "trigger_type", "config_version", "parameters", "interval_minutes", "enabled")}
        try: result = self.server.store.create_trigger(request_id=payload["request_id"], request_hash=content_hash(normalized), wish_id=payload.get("wish_id"), trigger_type=payload["trigger_type"], config_version=payload["config_version"], parameters=payload["parameters"], interval_minutes=payload["interval_minutes"], enabled=payload.get("enabled", True))
        except KeyError: self.send_json(404, {"error": "wish_not_found"}); return
        except IdempotencyConflict as exc: self.send_json(409, {"error": "idempotency_conflict", "message": str(exc)}); return
        except TriggerConfigurationConflict as exc: self.send_json(409, {"error": "trigger_configuration_conflict", "message": str(exc)}); return
        except (TypeError, ValueError) as exc: self.send_json(400, {"error": "invalid_trigger", "message": str(exc)}); return
        self.send_json(201, result)

    def _handle_trigger_patch(self, path: str) -> None:
        if self._authorize_device() is None: return
        trigger_id = path.removeprefix("/v1/event-triggers/"); payload, error = self.read_json_body()
        allowed = {"wish_id", "parameters", "interval_minutes", "enabled"}
        if "/" in trigger_id or not self._uuid(trigger_id) or error or not isinstance(payload, dict) or not payload or set(payload) - allowed or ("wish_id" in payload and payload["wish_id"] is not None and not self._uuid(payload["wish_id"])):
            self.send_json(400, {"error": "invalid_trigger", "message": error or "invalid trigger patch"}); return
        try: result = self.server.store.patch_trigger(trigger_id, payload)
        except KeyError: self.send_json(404, {"error": "wish_not_found"}); return
        except TriggerConfigurationConflict as exc: self.send_json(409, {"error": "trigger_configuration_conflict", "message": str(exc)}); return
        except (TypeError, ValueError) as exc: self.send_json(400, {"error": "invalid_trigger", "message": str(exc)}); return
        if result is None: self.send_json(404, {"error": "trigger_not_found"}); return
        self.send_json(200, result)

    def _handle_trigger_delete(self, path: str) -> None:
        if self._authorize_device() is None: return
        trigger_id = path.removeprefix("/v1/event-triggers/")
        if "/" in trigger_id or not self._uuid(trigger_id) or not self.server.store.delete_trigger(trigger_id): self.send_json(404, {"error": "trigger_not_found"}); return
        self.send_empty(204)

    def _handle_enrollment_claim(self) -> None:
        invitation_token = self.bearer_token()
        if invitation_token is None:
            self.send_json(
                401,
                {
                    "error": "invalid_invitation",
                    "message": "missing or invalid invitation Bearer token",
                },
            )
            return
        payload, body_error = self.read_json_body()
        if body_error:
            self.send_json(
                400,
                {"error": "invalid_claim", "message": body_error},
            )
            return
        try:
            claim = validate_claim_payload(payload)
        except ValueError as error:
            self.send_json(
                400,
                {"error": "invalid_claim", "message": str(error)},
            )
            return
        try:
            profile = claim_invitation(
                self.server.store,
                self.server.config,
                invitation_token=invitation_token,
                claim=claim,
            )
        except InvitationInvalid:
            self.send_json(
                401,
                {
                    "error": "invalid_invitation",
                    "message": "invitation is missing or invalid",
                },
            )
            return
        except InvitationExpired:
            self.send_json(
                410,
                {
                    "error": "invitation_expired",
                    "message": "invitation has expired",
                },
            )
            return
        except InvitationAlreadyClaimed:
            self.send_json(
                409,
                {
                    "error": "invitation_already_claimed",
                    "message": "invitation was already claimed by another device",
                },
            )
            return
        except EnrollmentConfigurationError:
            self.send_json(
                503,
                {
                    "error": "enrollment_not_configured",
                    "message": "central enrollment is not available",
                },
            )
            return
        except (OSError, sqlite3.Error, ValueError):
            self.send_json(
                500,
                {
                    "error": "enrollment_storage_error",
                    "message": "client enrollment could not be completed",
                },
            )
            return
        self.send_json(200, profile)

    # ── Blacklist rules CRUD ──────────────────────────────────────

    def _handle_shared_settings_get(self) -> None:
        # Shared settings may be read by an independent read token or any
        # registered dashboard device credential, but never anonymously.
        token = self.bearer_token()
        if not token:
            self.send_json(401, {"error": "missing_token", "message": "missing Bearer token"})
            return
        configured_read = self.server.config.read_token
        if (
            configured_read is None or not hmac.compare_digest(token, configured_read)
        ) and self.server.store.device_for_token(token) is None:
            self.send_json(401, {"error": "invalid_token", "message": "missing or invalid Bearer token"})
            return
        self.send_json(200, self.server.store.get_shared_settings())

    def _handle_shared_settings_update(self) -> None:
        # Reuse the existing registered-device authorization boundary.
        if not self._authorize_registered_device():
            return
        payload, body_error = self.read_json_body()
        if body_error or not isinstance(payload, dict):
            self.send_json(400, {"error": "invalid_request", "message": body_error or "request body must be a JSON object"})
            return
        allowed = {"day_start_hour", "primary_health_device_id", "sleep_local_time", "morning_report", "evening_report", "periodic_summary"}
        if not payload or not set(payload) <= allowed:
            self.send_json(
                400,
                {
                    "error": "invalid_settings_patch",
                    "message": "body contains unsupported shared settings",
                },
            )
            return
        try:
            self.send_json(200, self.server.store.update_shared_settings(payload))
        except ValueError as error:
            self.send_json(400, {"error": "invalid_shared_setting", "message": str(error)})

    def _handle_blacklist_rules_get(self) -> None:
        # Accept read token or any registered device credential.
        token = self.bearer_token()
        if not token:
            self.send_json(401, {"error": "missing_token", "message": "missing Bearer token"})
            return
        # Try read token first, then device token.
        configured_read = self.server.config.read_token
        if configured_read is not None and hmac.compare_digest(token, configured_read):
            pass  # authorised via read token
        elif self.server.store.device_for_token(token) is not None:
            pass  # authorised via device token
        else:
            self.send_json(401, {"error": "invalid_token", "message": "missing or invalid Bearer token"})
            return
        rules = self.server.store.list_blacklist_rules()
        self.send_json(200, {"rules": rules})

    def _handle_blacklist_rules_create(self) -> None:
        if not self._authorize_registered_device():
            return
        payload, body_error = self.read_json_body()
        if body_error or not isinstance(payload, dict):
            self.send_json(400, {"error": "invalid_request", "message": body_error or "request body must be a JSON object"})
            return
        rule_type = payload.get("rule_type")
        pattern = payload.get("pattern")
        label = payload.get("label")
        enabled = payload.get("enabled", True)
        platform_scope = payload.get("platform_scope")
        if rule_type not in {"app", "domain"}:
            self.send_json(400, {"error": "invalid_rule_type", "message": "rule_type must be app or domain"})
            return
        if not isinstance(pattern, str) or not pattern.strip():
            self.send_json(400, {"error": "invalid_pattern", "message": "pattern must not be empty"})
            return
        if not isinstance(label, str) or not label.strip():
            self.send_json(400, {"error": "invalid_label", "message": "label must not be empty"})
            return
        # Default platform_scope: app->pc, domain->web
        if platform_scope is None:
            platform_scope = "pc" if rule_type == "app" else "web"
        elif platform_scope not in {"pc", "android", "web"}:
            self.send_json(400, {"error": "invalid_platform_scope", "message": "platform_scope must be pc, android, or web"})
            return
        # Validate allowed combinations
        if rule_type == "app" and platform_scope not in {"pc", "android"}:
            self.send_json(400, {"error": "invalid_platform_scope", "message": "app rules require platform_scope of pc or android"})
            return
        if rule_type == "domain" and platform_scope != "web":
            self.send_json(400, {"error": "invalid_platform_scope", "message": "domain rules require platform_scope of web"})
            return
        try:
            rule = self.server.store.create_blacklist_rule(
                rule_type, pattern.strip(), label.strip(),
                enabled=bool(enabled), platform_scope=platform_scope,
            )
        except sqlite3.IntegrityError:
            self.send_json(400, {"error": "duplicate_rule", "message": "this pattern already exists"})
            return
        self.send_json(201, rule)

    def _handle_blacklist_rules_update(self, path: str) -> None:
        if not self._authorize_registered_device():
            return
        path_match = re.fullmatch(r"/v1/settings/blacklist-rules/([^/]+)", path)
        if path_match is None:
            self.send_json(404, {"error": "not_found", "message": "endpoint not found"})
            return
        rule_id = path_match.group(1)
        payload, body_error = self.read_json_body()
        if body_error or not isinstance(payload, dict):
            self.send_json(400, {"error": "invalid_request", "message": body_error or "request body must be a JSON object"})
            return
        if "platform_scope" in payload:
            self.send_json(400, {"error": "immutable_platform_scope", "message": "platform_scope is set on creation and cannot be changed by an update"})
            return
        unknown_fields = set(payload) - {"label", "enabled"}
        if not payload or unknown_fields:
            self.send_json(400, {"error": "invalid_request", "message": "request must contain only label and/or enabled"})
            return
        label = payload.get("label")
        enabled = payload.get("enabled")
        if "label" in payload and (
            not isinstance(label, str) or not label.strip() or len(label.strip()) > 100
        ):
            self.send_json(400, {"error": "invalid_label", "message": "label must contain 1 to 100 characters"})
            return
        if "enabled" in payload and not isinstance(enabled, bool):
            self.send_json(400, {"error": "invalid_enabled", "message": "enabled must be a boolean"})
            return
        rule = self.server.store.update_blacklist_rule(
            rule_id, label.strip() if isinstance(label, str) else None, enabled,
        )
        if rule is None:
            self.send_json(404, {"error": "not_found", "message": "rule not found"})
            return
        self.send_json(200, rule)

    def _handle_blacklist_rules_delete(self, path: str) -> None:
        if not self._authorize_registered_device():
            return
        path_match = re.fullmatch(r"/v1/settings/blacklist-rules/([^/]+)", path)
        if path_match is None:
            self.send_json(404, {"error": "not_found", "message": "endpoint not found"})
            return
        rule_id = path_match.group(1)
        deleted = self.server.store.delete_blacklist_rule(rule_id)
        if not deleted:
            self.send_json(404, {"error": "not_found", "message": "rule not found"})
            return
        self.send_json(204, {"status": "deleted"})


def create_server(
    config: CentralConfig,
    address: tuple[str, int] | None = None,
) -> CentralHTTPServer:
    return CentralHTTPServer(address or (config.host, config.port), config)
