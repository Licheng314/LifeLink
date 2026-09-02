"""Minimal loopback receiver for the official aw-watcher-web extension.

It intentionally implements only the few ActivityWatch endpoints the browser
extension needs.  Received page URLs are reduced to a canonical domain before
they leave the request handler; neither URLs nor titles are retained.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit


HOST = "127.0.0.1"
DEFAULT_PORT = 5600
MAX_BODY_BYTES = 8 * 1024
MAX_PULSE_SECONDS = 300
# The official browser extension appends the local device name to the bucket
# (for example ``aw-watcher-web-chrome_联想小A``). Keep the protocol prefix and
# browser slug strict, while allowing Unicode word characters in that suffix.
_BUCKET_RE = re.compile(r"^aw-watcher-web-[a-z0-9][\w.-]{0,95}$", re.UNICODE)
_CHROME_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}$")
_FIREFOX_ORIGIN_RE = re.compile(
    r"^moz-extension://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_EVENT_NAMESPACE = uuid.UUID("2b0bbf3e-73b9-5fd4-8770-5600f4ef9116")
_BROWSER_APPS = {
    "chrome": {"display_name": "Google Chrome", "process_name": "chrome.exe"},
    "edge": {"display_name": "Microsoft Edge", "process_name": "msedge.exe"},
    "msedge": {"display_name": "Microsoft Edge", "process_name": "msedge.exe"},
    "firefox": {"display_name": "Mozilla Firefox", "process_name": "firefox.exe"},
    "brave": {"display_name": "Brave", "process_name": "brave.exe"},
}


def canonical_domain(raw_url: Any) -> str | None:
    """Return the privacy-minimized domain accepted from a browser URL."""
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 4096:
        return None
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if not host or len(host) > 253 or "." not in host:
        return None
    # Keep one canonical spelling for the common equivalent web host form.
    return host[4:] if host.startswith("www.") else host


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _origin_allowed(origin: str | None) -> bool:
    return bool(origin and (_CHROME_ORIGIN_RE.fullmatch(origin) or _FIREFOX_ORIGIN_RE.fullmatch(origin)))


def _iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _browser_app(bucket_id: str) -> dict[str, str] | None:
    """Infer only a known browser process identity from the public bucket ID."""
    suffix = bucket_id.removeprefix("aw-watcher-web-").split("-", 1)[0].split("_", 1)[0]
    app = _BROWSER_APPS.get(suffix)
    return dict(app) if app else None


@dataclass
class _ActiveEvent:
    domain: str
    started_at: datetime
    ends_at: datetime
    event_id: str
    revision: int


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AWWebCompatReceiver:
    """Lifecycle-managed receiver whose callback gets privacy-minimized events."""

    def __init__(self, event_callback: Callable[[dict[str, Any]], None], host: str = HOST, port: int = DEFAULT_PORT):
        if host != HOST:
            raise ValueError("AW compatibility receiver may bind only 127.0.0.1")
        self._callback = event_callback
        self._host = host
        self._requested_port = port
        self._server: _ThreadedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._buckets: dict[str, dict[str, Any]] = {}
        self._active: dict[str, _ActiveEvent] = {}
        self._lock = threading.Lock()

    @property
    def port(self) -> int | None:
        return self._server.server_port if self._server else None

    def start(self) -> dict[str, Any]:
        if self._server is not None:
            return {"status": "already_started", "host": self._host, "port": self.port}
        try:
            server = _ThreadedHTTPServer((self._host, self._requested_port), _Handler)
        except OSError as error:
            return {
                "status": "port_in_use" if self._requested_port else "bind_failed",
                "host": self._host,
                "port": self._requested_port,
                "error": str(error),
            }
        server.receiver = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="aw-web-compat", daemon=True)
        self._thread.start()
        return {"status": "started", "host": self._host, "port": server.server_port}

    def stop(self) -> dict[str, Any]:
        server, thread = self._server, self._thread
        if server is None:
            return {"status": "stopped"}
        self._server = None
        self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        return {"status": "stopped"}

    def create_bucket(self, bucket_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not _BUCKET_RE.fullmatch(bucket_id) or payload.get("type") != "web.tab.current":
            return None
        # Return the standard AW metadata shape without retaining the real host
        # or extension-provided labels. Some extension views parse these fields
        # even though heartbeat delivery itself does not require them.
        bucket = {
            "id": bucket_id,
            "name": "",
            "type": "web.tab.current",
            "client": "aw-client-web",
            "hostname": "life-link",
            "created": _iso_timestamp(datetime.now(timezone.utc)),
            "data": {},
        }
        with self._lock:
            self._buckets[bucket_id] = bucket
        return bucket

    def get_bucket(self, bucket_id: str) -> dict[str, Any] | None:
        if not _BUCKET_RE.fullmatch(bucket_id):
            return None
        with self._lock:
            bucket = self._buckets.get(bucket_id)
            return dict(bucket) if bucket else None

    def list_buckets(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {bucket_id: dict(bucket) for bucket_id, bucket in self._buckets.items()}

    def heartbeat(self, bucket_id: str, payload: dict[str, Any], pulse_seconds: int) -> bool:
        if self.get_bucket(bucket_id) is None:
            return False
        timestamp = _parse_timestamp(payload.get("timestamp"))
        duration = payload.get("duration", 0)
        data = payload.get("data")
        if timestamp is None or not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return False
        if duration < 0 or duration > MAX_PULSE_SECONDS or not isinstance(data, dict):
            return False
        incognito = data.get("incognito", False)
        if not isinstance(incognito, bool):
            return False
        if incognito:
            return True
        domain = canonical_domain(data.get("url"))
        if domain is None:
            # Browser-internal pages, local addresses and unsupported schemes
            # are valid watcher observations but intentionally not Life Link
            # website facts. Acknowledge them so the extension does not mistake
            # privacy filtering for a disconnected ActivityWatch server.
            return isinstance(data.get("url"), str)
        end = timestamp + timedelta(seconds=max(float(duration), pulse_seconds))
        with self._lock:
            previous = self._active.get(bucket_id)
            if previous and previous.domain == domain and timestamp <= previous.ends_at + timedelta(seconds=pulse_seconds):
                previous.ends_at = max(previous.ends_at, end)
                previous.revision += 1
                active = previous
            else:
                event_id = str(uuid.uuid5(
                    _EVENT_NAMESPACE, f"{bucket_id}:{domain}:{_iso_timestamp(timestamp)}"
                ))
                active = _ActiveEvent(domain, timestamp, end, event_id, 0)
                self._active[bucket_id] = active
            web_payload: dict[str, Any] = {"domain": active.domain}
            browser_app = _browser_app(bucket_id)
            if browser_app is not None:
                web_payload["browser_app"] = browser_app
            event = {
                "event_type": "web.foreground",
                "source": {"kind": "desktop", "collector": "browser_extension", "reliability": "observed"},
                "event_id": active.event_id,
                "revision": active.revision,
                "occurred_at": _iso_timestamp(active.started_at),
                "duration_seconds": max(0, int(round((active.ends_at - active.started_at).total_seconds()))),
                "payload": web_payload,
            }
        try:
            self._callback(event)
        except Exception:
            return False
        return True


class _Handler(BaseHTTPRequestHandler):
    server: _ThreadedHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def receiver(self) -> AWWebCompatReceiver:
        return self.server.receiver  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any], origin: str | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if _origin_allowed(origin) else None

    def _read_json(self) -> dict[str, Any] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return None
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 1 <= size <= MAX_BODY_BYTES:
            return None
        try:
            body = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def do_OPTIONS(self) -> None:
        origin = self._origin()
        method = self.headers.get("Access-Control-Request-Method", "")
        headers = {item.strip().lower() for item in self.headers.get("Access-Control-Request-Headers", "").split(",") if item.strip()}
        if not origin or method not in {"GET", "POST"} or not headers.issubset({"content-type"}):
            self._send_json(403, {"error": "cors_denied"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:
        origin = self._origin()
        if not origin:
            self._send_json(403, {"error": "cors_denied"})
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/api/0/info":
            self._send_json(200, {"hostname": "life-link", "version": "0.1", "testing": False}, origin)
            return
        if path.rstrip("/") == "/api/0/buckets":
            self._send_json(200, self.receiver.list_buckets(), origin)
            return
        if path.startswith("/api/0/buckets/"):
            bucket = self.receiver.get_bucket(path.removeprefix("/api/0/buckets/"))
            self._send_json(200, bucket, origin) if bucket else self._send_json(404, {"error": "not_found"}, origin)
            return
        self._send_json(404, {"error": "not_found"}, origin)

    def do_POST(self) -> None:
        origin = self._origin()
        if not origin:
            self._send_json(403, {"error": "cors_denied"})
            return
        payload = self._read_json()
        if payload is None:
            self._send_json(400, {"error": "invalid_payload"}, origin)
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path.startswith("/api/0/buckets/") and path.endswith("/heartbeat"):
            bucket_id = path.removeprefix("/api/0/buckets/").removesuffix("/heartbeat").rstrip("/")
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                pulse_values = query.get("pulsetime", ["30"])
                if len(pulse_values) != 1:
                    raise ValueError
                pulse = int(pulse_values[0])
            except ValueError:
                pulse = 0
            if not 1 <= pulse <= MAX_PULSE_SECONDS or not self.receiver.heartbeat(bucket_id, payload, pulse):
                self._send_json(400, {"error": "invalid_heartbeat"}, origin)
                return
            self._send_json(200, {
                "timestamp": payload.get("timestamp"),
                "duration": payload.get("duration", 0),
                "data": {},
            }, origin)
            return
        if path.startswith("/api/0/buckets/"):
            bucket = self.receiver.create_bucket(path.removeprefix("/api/0/buckets/"), payload)
            if bucket is None:
                self._send_json(400, {"error": "invalid_bucket"}, origin)
                return
            self._send_json(200, bucket, origin)
            return
        self._send_json(404, {"error": "not_found"}, origin)
