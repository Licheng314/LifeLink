"""Loopback-only management service for the Life Link central server.

This is deliberately separate from the data API: it is an unauthenticated
*local UI*, protected by a fixed loopback bind plus strict Host/Origin and a
per-process CSRF value.  It never serializes central credentials to clients.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time as monotonic_time
import uuid
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

from .ai_connection_package import SKILL_FILE, create_connection_package, mcp_config_template
from .config import CentralConfig, _read_external_config
from .domain import canonical_json, content_hash
from .invitations import create_invitation
from .operations import _secure_atomic_write_json
from .read_model import parse_read_range
from .storage import (
    FutureWishDay,
    IdempotencyConflict,
    WishDayNotFound,
    WishDaysIncomplete,
    WishDeleted,
    WishLimitReached,
    WishNotCompletable,
)
import central_windows_startup
from configure_tailscale_endpoint import TailscaleSetupError, detect_https_endpoint


MANAGEMENT_HOST = "127.0.0.1"
MANAGEMENT_PORT = 8092
PROVIDERS = {"public_domain", "tailscale", "https_tunnel"}
STATIC_ASSETS = {
    "assets/images/life-link-logo.png": "image/png",
    "assets/scripts/app.js": "application/javascript; charset=utf-8",
    "assets/scripts/central-management.js": "application/javascript; charset=utf-8",
    "assets/scripts/central-bridge.js": "application/javascript; charset=utf-8",
    "assets/scripts/devices.js": "application/javascript; charset=utf-8",
    "assets/scripts/health-info.js": "application/javascript; charset=utf-8",
    "assets/scripts/location.js": "application/javascript; charset=utf-8",
    "assets/scripts/shared-ui.js": "application/javascript; charset=utf-8",
    "assets/scripts/tools.js": "application/javascript; charset=utf-8",
    "assets/scripts/usage.js": "application/javascript; charset=utf-8",
    "assets/scripts/wishes-events.js": "application/javascript; charset=utf-8",
    "assets/styles/base.css": "text/css; charset=utf-8",
    "assets/styles/central-management.css": "text/css; charset=utf-8",
    "assets/styles/components.css": "text/css; charset=utf-8",
    "assets/styles/tools.css": "text/css; charset=utf-8",
    "assets/styles/wishes-events.css": "text/css; charset=utf-8",
    "assets/vendor/leaflet/leaflet.css": "text/css; charset=utf-8",
    "assets/vendor/leaflet/leaflet.js": "application/javascript; charset=utf-8",
    "assets/vendor/leaflet/leaflet.js.map": "application/json; charset=utf-8",
    "assets/vendor/chart.umd.min.js": "application/javascript; charset=utf-8",
    "assets/vendor/lucide.js": "application/javascript; charset=utf-8",
}


def _asset(name: str) -> bytes:
    return (Path(__file__).resolve().parents[1] / "management-web" / name).read_bytes()


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _json(request: Request, timeout: float) -> dict[str, Any]:
    client = build_opener(ProxyHandler({}), _NoRedirectHandler())
    with client.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("服务返回了无效响应")
    return payload


def normalize_https_origin(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("外部地址必须是 HTTPS 地址")
    parsed = urlparse(value.strip())
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in {"", "/"} or parsed.query
            or parsed.fragment):
        raise ValueError("外部地址必须是不带路径、账号或参数的 HTTPS 地址")
    return f"https://{parsed.netloc.lower()}"


def verify_public_endpoint(base_url: str, server: Any, config: CentralConfig) -> dict[str, Any]:
    """Verify a candidate endpoint without changing the persisted config."""
    normalized = normalize_https_origin(base_url)
    health = _json(Request(f"{normalized}/v1/health"), 15)
    if health.get("status") != "ok" or health.get("role") != "central":
        raise ValueError("该地址不是可用的 Life Link 中央服务")
    token = next(iter(config.token_bindings), None)
    if not token:
        raise ValueError("中央服务没有可用于验证的设备凭据")
    remote = _json(Request(f"{normalized}/v1/ai-readers", headers={"Authorization": f"Bearer {token}"}), 15)
    local_id = server.store.ai_readers.central_instance_id()
    if remote.get("central_instance_id") != local_id:
        raise ValueError("该地址指向另一套 Life Link 中央服务")
    return {"base_url": normalized, "central_instance_id": local_id}


class ManagementHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], data_server: Any, config: CentralConfig,
                 shutdown_callback: Callable[[], None] | None = None) -> None:
        self.data_server = data_server
        self.config = config
        self.csrf_token = secrets.token_urlsafe(32)
        self.shutdown_callback = shutdown_callback
        self.ai_package_lock = threading.Lock()
        # Only public Tianditu PNG responses live here.  Personal location
        # data never enters this cache; the browser still obtains it through
        # the ordinary no-store API route.
        self.map_tile_lock = threading.Lock()
        self.map_tiles: dict[str, tuple[float, bytes, str]] = {}
        super().__init__(address, ManagementRequestHandler)


class ManagementRequestHandler(BaseHTTPRequestHandler):
    server: ManagementHTTPServer
    server_version = "LifeLinkManagement/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _allowed_host(self) -> bool:
        host = self.headers.get("Host", "")
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return hmac.compare_digest(host, expected)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_address[1]}"
        return origin is not None and hmac.compare_digest(origin, expected)

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The copied PC Dashboard uses controlled inline style attributes for
        # chart widths, event colours and timeline marker placement.  Allow
        # inline CSS only; scripts remain restricted to same-origin files.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_conditional_json(self, payload: Any) -> None:
        """Send private live data without repeating an unchanged JSON body.

        ``no-store`` remains deliberate: the browser must not retain a personal
        timeline as an HTTP cache entry.  The ETag only lets the currently open
        page prove that its in-memory snapshot still matches the central view.
        """
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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_attachment(self, filename: str, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_tile(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _reject_host(self) -> bool:
        if self._allowed_host():
            return False
        self._send(400, {"error": "invalid_host", "message": "management is available only at 127.0.0.1"})
        return True

    def _body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 64 * 1024:
                raise ValueError("request body too large")
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid_request", "message": "请求必须是 JSON 对象"})
            return None
        if not isinstance(value, dict):
            self._send(400, {"error": "invalid_request", "message": "请求必须是 JSON 对象"})
            return None
        return value

    def _protected(self) -> bool:
        if not self._same_origin() or not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf_token):
            self._send(403, {"error": "csrf_rejected", "message": "请求来源验证失败"})
            return False
        return True

    def do_GET(self) -> None:
        if self._reject_host(): return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            page = _asset("index.html").decode("utf-8").replace("__CSRF_TOKEN__", self.server.csrf_token).encode("utf-8")
            self._send(200, page, "text/html; charset=utf-8"); return
        asset = path.removeprefix("/")
        if asset in STATIC_ASSETS:
            self._send(200, _asset(asset), STATIC_ASSETS[asset]); return
        if path == "/api/status":
            self._send(200, self._status()); return
        if path == "/api/identity":
            self._send(200, {"service": "life-link-management", "version": 1,
                             "management_url": f"http://127.0.0.1:{self.server.server_address[1]}",
                             "data_api_port": self.server.config.port}); return
        if path == "/api/ai-reader-skill":
            try:
                self._send(200, {"skill": SKILL_FILE.read_text(encoding="utf-8")})
            except OSError:
                self._send(500, {"error": "ai_reader_skill_unavailable", "message": "AI Reader Skill 不可用"})
            return
        if path == "/api/ai-connection-mcp-config":
            # This is the same non-secret template included in every package.
            # It intentionally has no pairing token, reader identity or path.
            self._send(200, {"mcp_config": mcp_config_template()})
            return
        if path == "/api/runtime/login-startup":
            self._central_login_startup_status()
            return
        if path.startswith("/map-tiles/"):
            self._proxy_tianditu_tile(path); return
        if path in {
            "/api/dashboard/calendar-days",
            "/api/dashboard/timeline-events",
            "/api/dashboard/usage",
            "/api/dashboard/locations",
            "/api/dashboard/health-info",
            "/api/dashboard/devices",
        }:
            self._dashboard_read(path, parse_qs(parsed.query, keep_blank_values=True)); return
        if path in {
            "/api/calendar-days", "/api/timeline-events", "/api/usage", "/api/locations",
            "/api/health-info", "/api/settings", "/api/devices", "/api/device-management",
            "/api/wishes", "/api/event-triggers", "/api/trigger-types", "/api/event-background",
            "/api/ai-readers", "/api/blacklist/rules", "/api/live-usage", "/api/central-health",
        }:
            self._pc_read_compat(path, parse_qs(parsed.query, keep_blank_values=True)); return
        if path == "/api/sync/central":
            self._send(200, {"state": "idle", "configured": True, "central_base_url": "本机中央服务"}); return
        if path.startswith("/api/ai-readers/"):
            self._ai_reader_read_compat(path, parse_qs(parsed.query, keep_blank_values=True)); return
        self._send(404, {"error": "not_found"})

    def _proxy_tianditu_tile(self, path: str) -> None:
        """Serve the shared Tianditu base map without exposing its key to JS."""
        parts = path.strip("/").split("/")
        if len(parts) != 5 or parts[0] != "map-tiles" or parts[1] not in {"vec", "cva"} or not parts[4].endswith(".png"):
            self._send(404, {"error": "not_found"}); return
        try:
            z, x, y = (int(parts[2]), int(parts[3]), int(parts[4][:-4]))
            if not 0 <= z <= 18 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
                raise ValueError
        except ValueError:
            self._send(400, {"error": "invalid_map_tile"}); return
        cache_key = f"{parts[1]}/{z}/{x}/{y}"
        now = monotonic_time.monotonic()
        with self.server.map_tile_lock:
            cached = self.server.map_tiles.get(cache_key)
            if cached and cached[0] > now:
                self._send_tile(cached[1], cached[2])
                return
            if cached:
                self.server.map_tiles.pop(cache_key, None)
        layer = {"vec": "vec_w", "cva": "cva_w"}[parts[1]]
        endpoint = f"https://t{x % 8}.tianditu.gov.cn/DataServer?T={layer}&x={x}&y={y}&l={z}&tk={self.server.config.tianditu_key}"
        try:
            # Tianditu rejects non-browser user agents.  This is the same
            # compatibility header used by the existing PC tile proxy; it
            # carries no user identifier or credential.
            request = Request(endpoint, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            with urlopen(request, timeout=10) as response:
                payload = response.read(2 * 1024 * 1024 + 1)
                content_type = response.headers.get("Content-Type", "image/png")
            if len(payload) > 2 * 1024 * 1024 or not content_type.lower().startswith("image/"):
                raise ValueError("invalid tile response")
        except Exception:
            self._send(502, {"error": "map_tile_unavailable", "message": "天地图瓦片暂不可用"}); return
        with self.server.map_tile_lock:
            # Bound the process cache.  Entries are replaceable public map
            # imagery, so simple oldest-expiry eviction is sufficient.
            if len(self.server.map_tiles) >= 512:
                oldest = min(self.server.map_tiles, key=lambda key: self.server.map_tiles[key][0])
                self.server.map_tiles.pop(oldest, None)
            self.server.map_tiles[cache_key] = (now + 86_400, payload, content_type)
        self._send_tile(payload, content_type)

    def _pc_read_compat(self, path: str, params: dict[str, list[str]]) -> None:
        """Temporary read-only adapter for the copied PC Dashboard DOM.

        It exists only while its browser scripts are migrated page by page.  It
        intentionally exposes no PC cache, collector, outbox, or write route.
        """
        conditional = False
        try:
            store = self.server.data_server.store
            if path == "/api/calendar-days":
                self._require_exact_params(params, {"from", "to"})
                payload = store.calendar_days(params["from"][0], params["to"][0])
            elif path == "/api/timeline-events":
                self._require_exact_params(params, {"from", "to"})
                start, end = parse_read_range(params["from"][0], params["to"][0])
                payload = store.list_timeline(start, end)
                conditional = True
            elif path in {"/api/usage", "/api/locations", "/api/health-info"}:
                if set(params) not in (set(), {"date"}) or any(len(values) != 1 for values in params.values()):
                    raise ValueError("expected optional date")
                raw_date = params.get("date", [self._current_business_date()])[0]
                business_date = date.fromisoformat(raw_date)
                if business_date.isoformat() != raw_date:
                    raise ValueError("date must be YYYY-MM-DD")
                if path == "/api/health-info":
                    payload = store.read_health_info(business_date)
                else:
                    settings = store.get_shared_settings()
                    start = datetime.combine(business_date, time(int(settings["day_start_hour"])), tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
                    end = start + timedelta(days=1)
                    payload = store.read_usage(start, end) if path == "/api/usage" else store.read_locations(start, end)
            elif path == "/api/settings":
                self._require_exact_params(params, set())
                payload = store.get_shared_settings()
            elif path == "/api/wishes":
                if set(params) - {"include_archived"} or any(len(values) != 1 for values in params.values()):
                    raise ValueError("invalid wishes query")
                payload = {"wishes": store.list_wishes(include_archived=params.get("include_archived") == ["true"])}
            elif path == "/api/event-triggers":
                self._require_exact_params(params, set())
                payload = {"triggers": store.list_triggers()}
            elif path == "/api/trigger-types":
                self._require_exact_params(params, set())
                payload = {"trigger_types": store.trigger_types()}
            elif path == "/api/event-background":
                self._require_exact_params(params, {"business_date"})
                payload = store.event_background(params["business_date"][0])
            elif path == "/api/ai-readers":
                self._require_exact_params(params, set())
                payload = {"readers": store.ai_readers.list_readers()}
            elif path == "/api/blacklist/rules":
                self._require_exact_params(params, set())
                payload = {"rules": store.list_blacklist_rules()}
            elif path == "/api/live-usage":
                self._require_exact_params(params, set())
                payload = {"collection": {"browser": {"status": "not_applicable"}}}
            elif path == "/api/central-health":
                self._require_exact_params(params, set())
                store.ping()
                payload = {"status": "ok", "role": "central", "connected": True}
            else:
                self._require_exact_params(params, set())
                devices = store.list_managed_devices()
                if path == "/api/devices":
                    payload = {"devices": devices, "local": None}
                else:
                    payload = {"devices": devices}
        except (ValueError, KeyError):
            self._send(400, {"error": "invalid_dashboard_query", "message": "请求参数无效"})
            return
        except sqlite3.Error:
            self._send(500, {"error": "storage_error", "message": "central dashboard query failed"})
            return
        if conditional:
            self._send_conditional_json(payload)
            return
        self._send(200, payload)

    def _ai_reader_read_compat(self, path: str, params: dict[str, list[str]]) -> None:
        """Read central AI-reader facts for the copied timeline panel.

        Process inspection is deliberately reported as unavailable here: it is
        a property of an AI host, not of the central server which stores the
        pairing and access records.
        """
        prefix = "/api/ai-readers/"
        tail = path.removeprefix(prefix)
        reader_id, separator, action = tail.partition("/")
        if not reader_id or not separator or "/" in action:
            self._send(404, {"error": "not_found"}); return
        try:
            readers = self.server.data_server.store.ai_readers
            if action == "process-status":
                self._require_exact_params(params, set())
                if readers.get_reader(reader_id) is None:
                    self._send(404, {"error": "ai_reader_not_found"}); return
                self._send(200, {"reader_id": reader_id, "process_running": False, "process_display_name": None}); return
            if action == "access-logs":
                if set(params) - {"limit"} or len(params.get("limit", [None])) != 1:
                    raise ValueError("invalid limit")
                limit = int(params.get("limit", ["100"])[0])
                self._send(200, {"reader_id": reader_id, "logs": readers.list_access_logs(reader_id, limit=limit)}); return
            if action == "context-preview":
                if set(params) - {"view"} or any(len(values) != 1 or values == [""] for values in params.values()):
                    raise ValueError("invalid context preview request")
                self._send(200, readers.preview_next_context(reader_id, view=params.get("view", ["compact"])[0])); return
            self._send(404, {"error": "not_found"})
        except ValueError as error:
            self._send(400, {"error": "invalid_request", "message": str(error)})
        except sqlite3.Error:
            self._send(500, {"error": "storage_error", "message": "central AI reader query failed"})

    def _current_business_date(self) -> str:
        settings = self.server.data_server.store.get_shared_settings()
        local = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        if local.hour < int(settings["day_start_hour"]):
            local -= timedelta(days=1)
        return local.date().isoformat()

    def _dashboard_read(self, path: str, params: dict[str, list[str]]) -> None:
        """Return central projections to the loopback UI without a browser credential.

        The management server uses a private read credential to call the
        already-running local data service.  This keeps data API authorization
        and projection semantics in one place while ensuring the browser never
        receives a credential or falls back to a PC cache.
        """
        try:
            if path == "/api/dashboard/calendar-days":
                self._require_exact_params(params, {"from", "to"})
                from_date, to_date = params["from"][0], params["to"][0]
                for value in (from_date, to_date):
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError("from and to must be YYYY-MM-DD")
                data_path = "/v1/calendar-days"
            elif path == "/api/dashboard/health-info":
                self._require_exact_params(params, {"date"})
                raw_date = params["date"][0]
                if date.fromisoformat(raw_date).isoformat() != raw_date:
                    raise ValueError("date must be YYYY-MM-DD")
                data_path = "/v1/health-info"
            elif path == "/api/dashboard/devices":
                self._require_exact_params(params, set())
                data_path = "/v1/devices"
            else:
                self._require_exact_params(params, {"from", "to"})
                parse_read_range(params["from"][0], params["to"][0])
                if path == "/api/dashboard/timeline-events":
                    data_path = "/v1/timeline-events"
                elif path == "/api/dashboard/usage":
                    data_path = "/v1/read/usage"
                else:
                    data_path = "/v1/read/locations"
        except ValueError as error:
            self._send(400, {"error": "invalid_dashboard_query", "message": str(error)})
            return
        token = self.server.config.read_token
        if token is None:
            self._send(503, {"error": "read_access_not_configured", "message": "central dashboard read access is not configured"})
            return
        host, port = self.server.data_server.server_address[:2]
        query = urlencode({key: values[0] for key, values in params.items()})
        url = f"http://{host}:{port}{data_path}" + (f"?{query}" if query else "")
        try:
            payload = _json(Request(url, headers={"Authorization": f"Bearer {token}"}), 15)
        except HTTPError as error:
            status = 503 if error.code in {401, 403, 503} else 502
            self._send(status, {"error": "central_data_unavailable", "message": "central dashboard data is unavailable"})
            return
        except (OSError, ValueError, json.JSONDecodeError):
            self._send(502, {"error": "central_data_unavailable", "message": "central dashboard data is unavailable"})
            return
        self._send(200, payload)

    @staticmethod
    def _require_exact_params(params: dict[str, list[str]], names: set[str]) -> None:
        if set(params) != names or any(len(values) != 1 for values in params.values()):
            expected = ", ".join(sorted(names)) or "no query parameters"
            raise ValueError(f"expected exactly: {expected}")

    def _status(self) -> dict[str, Any]:
        endpoint: dict[str, Any] | None = None
        if self.server.config.config_path:
            try:
                endpoint = _read_external_config(str(self.server.config.config_path)).get("public_endpoint")  # type: ignore[assignment]
            except ValueError:
                pass
        public = endpoint if isinstance(endpoint, dict) else None
        safe_public = ({key: public.get(key) for key in ("provider", "base_url", "central_instance_id", "verified_at")}
                       if public else None)
        return {"status": "ok", "role": "life-link-central-management",
                "data_api": {"host": self.server.config.host, "port": self.server.config.port},
                "management": {"host": MANAGEMENT_HOST, "port": self.server.server_address[1], "ready": True},
                "devices": self.server.data_server.store.list_managed_devices(),
                "public_endpoint": safe_public,
                "central_instance_id": self.server.data_server.store.ai_readers.central_instance_id()}

    def do_POST(self) -> None:
        if self._reject_host(): return
        path = urlparse(self.path).path
        # The native tray has no browser Origin/CSRF context. Its per-process
        # bearer capability is stronger and is never returned to the WebUI.
        if path == "/api/shutdown": self._shutdown(); return
        if not self._protected(): return
        if path == "/api/device-invitations": self._create_invitation(); return
        if path == "/api/ai-connection-package": self._create_package(); return
        if path == "/api/network/verify": self._verify_network(); return
        if path == "/api/network/tailscale/detect": self._detect_tailscale(); return
        if path == "/api/runtime/login-startup": self._central_login_startup_update(); return
        if path.startswith("/api/ai-readers/") and path.endswith("/clear-reading-progress"):
            self._ai_reader_clear_progress(path); return
        if path == "/api/wishes" or path.startswith("/api/wishes/"):
            self._wish_write_compat("POST", path); return
        if (path == "/api/settings" or path.startswith("/api/device-management/")
                or path.startswith("/api/blacklist/rules") or path.startswith("/api/event-triggers")):
            self._pc_write_compat("POST", path); return
        self._send(404, {"error": "not_found"})

    def _ai_reader_clear_progress(self, path: str) -> None:
        reader_id = path.removeprefix("/api/ai-readers/").removesuffix("/clear-reading-progress")
        if not reader_id or "/" in reader_id or self._body() is None:
            if reader_id:
                self._send(400, {"error": "invalid_request", "message": "请求必须是 JSON 对象"})
            else:
                self._send(404, {"error": "not_found"})
            return
        try:
            reader = self.server.data_server.store.ai_readers.clear_reading_progress(reader_id)
        except sqlite3.Error:
            self._send(404, {"error": "ai_reader_not_found"}); return
        self._send(200, {"reader": reader})

    def do_PATCH(self) -> None:
        if self._reject_host() or not self._protected(): return
        path = urlparse(self.path).path
        if path == "/api/wishes" or path.startswith("/api/wishes/"):
            self._wish_write_compat("PATCH", path); return
        self._pc_write_compat("PATCH", path)

    def do_PUT(self) -> None:
        if self._reject_host() or not self._protected(): return
        path = urlparse(self.path).path
        if path.startswith("/api/wishes/"):
            self._wish_write_compat("PUT", path); return
        self._send(404, {"error": "not_found"})

    def do_DELETE(self) -> None:
        if self._reject_host() or not self._protected(): return
        path = urlparse(self.path).path
        if path.startswith("/api/wishes/"):
            self._wish_write_compat("DELETE", path); return
        self._pc_write_compat("DELETE", path)

    def _central_login_startup_status(self) -> None:
        try:
            self._send(200, central_windows_startup.status())
        except (OSError, RuntimeError) as error:
            self._send(503, {"error": "login_startup_unavailable", "message": str(error)})

    def _central_login_startup_update(self) -> None:
        body = self._body()
        if body is None:
            return
        if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
            self._send(400, {"error": "invalid_request", "message": "enabled 必须是布尔值"})
            return
        try:
            self._send(200, central_windows_startup.set_enabled(body["enabled"]))
        except (OSError, RuntimeError) as error:
            self._send(422, {"error": "login_startup_update_failed", "message": str(error)})

    def _management_source_device_id(self) -> str:
        """Use one registered central device identity for UI-originated audit events."""
        source = next(iter(self.server.config.token_bindings.values()), None)
        if not isinstance(source, str) or not source:
            raise ValueError("中央服务没有已注册的设备身份")
        return source

    @staticmethod
    def _is_uuid(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            uuid.UUID(value)
            return True
        except ValueError:
            return False

    def _wish_write_compat(self, method: str, path: str) -> None:
        """Write adapter for the copied timeline's central-authoritative wishes."""
        store = self.server.data_server.store
        try:
            source = self._management_source_device_id()
            if path == "/api/wishes":
                if method != "POST":
                    self._send(405, {"error": "method_not_allowed"}); return
                body = self._body()
                if body is None:
                    return
                allowed = {"request_id", "text", "duration_days", "ai_tracking_enabled"}
                if set(body) - allowed or not {"request_id", "text"} <= set(body):
                    raise ValueError("invalid wish body")
                request_id, text = body["request_id"], body["text"]
                duration = body.get("duration_days", 3)
                tracking = body.get("ai_tracking_enabled", False)
                if (not self._is_uuid(request_id) or not isinstance(text, str) or not text.strip()
                        or len(text.strip()) > 30 or duration not in {3, 7} or not isinstance(tracking, bool)):
                    raise ValueError("invalid request_id, text, duration_days, or ai_tracking_enabled")
                normalized = {"request_id": request_id, "text": text.strip(), "duration_days": duration,
                              "ai_tracking_enabled": tracking}
                created = store.create_wish(request_id=request_id, request_hash=content_hash(normalized),
                                             text=text.strip(), duration_days=duration,
                                             ai_tracking_enabled=tracking, source_device_id=source)
                self._send(201, created); return

            tail = path.removeprefix("/api/wishes/")
            if not tail:
                self._send(404, {"error": "wish_not_found"}); return
            if tail.endswith("/complete"):
                wish_id = tail.removesuffix("/complete")
                if method != "POST" or "/" in wish_id or not self._is_uuid(wish_id):
                    self._send(404, {"error": "wish_not_found"}); return
                completed = store.complete_wish(wish_id, source_device_id=source)
                if completed is None:
                    self._send(404, {"error": "wish_not_found"}); return
                self._send(200, completed); return
            if "/days/" in tail:
                wish_id, separator, business_date = tail.partition("/days/")
                if method != "PUT" or not separator or "/" in business_date or not self._is_uuid(wish_id):
                    self._send(404, {"error": "wish_not_found"}); return
                body = self._body()
                if body is None:
                    return
                if set(body) != {"evaluation"} or body.get("evaluation") not in {"completed", "not_completed"}:
                    raise ValueError("evaluation must be completed or not_completed")
                assessed = store.assess_wish_day(wish_id=wish_id, business_date=business_date,
                                                  evaluation=body["evaluation"], source_device_id=source)
                if assessed is None:
                    self._send(404, {"error": "wish_not_found"}); return
                self._send(200, assessed); return
            if "/" in tail or not self._is_uuid(tail):
                self._send(404, {"error": "wish_not_found"}); return
            if method == "PATCH":
                body = self._body()
                if body is None:
                    return
                text = body.get("text")
                if set(body) != {"text"} or not isinstance(text, str) or not (1 <= len(text.strip()) <= 30):
                    raise ValueError("text must contain 1 to 30 characters after trimming")
                updated = store.patch_wish_text(tail, text.strip())
                if updated is None:
                    self._send(404, {"error": "wish_not_found"}); return
                self._send(200, updated); return
            if method == "DELETE":
                deleted = store.delete_wish(tail)
                if deleted is None:
                    self._send(404, {"error": "wish_not_found"}); return
                self._send(204, b"", "application/json; charset=utf-8"); return
            self._send(405, {"error": "method_not_allowed"})
        except WishLimitReached as error:
            self._send(409, {"error": "unarchived_wish_limit_reached", "message": str(error)})
        except WishDeleted as error:
            self._send(410, {"error": "wish_deleted", "message": str(error)})
        except IdempotencyConflict as error:
            self._send(409, {"error": "idempotency_conflict", "message": str(error)})
        except WishDaysIncomplete as error:
            self._send(409, {"error": "wish_days_incomplete", "message": "all wish days must be assessed before manual completion",
                             "missing_business_dates": error.missing_business_dates})
        except WishNotCompletable as error:
            self._send(409, {"error": "wish_not_completable", "message": str(error)})
        except FutureWishDay as error:
            self._send(409, {"error": "future_wish_day", "message": str(error)})
        except WishDayNotFound as error:
            self._send(400, {"error": "invalid_assessment", "message": str(error)})
        except ValueError as error:
            self._send(400, {"error": "invalid_wish", "message": str(error)})
        except sqlite3.Error:
            self._send(500, {"error": "storage_error", "message": "central wish write failed"})

    def _pc_write_compat(self, method: str, path: str) -> None:
        """First write slice for copied UI: shared settings and device roster."""
        try:
            store = self.server.data_server.store
            if path == "/api/settings" and method == "POST":
                body = self._body()
                if body is None: return
                self._send(200, store.update_shared_settings(body)); return
            if path == "/api/blacklist/rules":
                if method != "POST":
                    self._send(405, {"error": "method_not_allowed"}); return
                body = self._body()
                if body is None: return
                created = store.create_blacklist_rule(
                    str(body.get("rule_type", "")), str(body.get("pattern", "")), str(body.get("label", "")),
                    enabled=body.get("enabled", True) is True, platform_scope=str(body.get("platform_scope", "")),
                )
                self._send(201, created); return
            if path.startswith("/api/blacklist/rules/"):
                rule_id = path.removeprefix("/api/blacklist/rules/")
                if not rule_id or "/" in rule_id:
                    self._send(404, {"error": "not_found"}); return
                if method == "DELETE":
                    if not store.delete_blacklist_rule(rule_id):
                        self._send(404, {"error": "rule_not_found"}); return
                    self._send(204, b"", "application/json; charset=utf-8"); return
                if method == "PATCH":
                    body = self._body()
                    if body is None: return
                    label = body.get("label")
                    enabled = body.get("enabled")
                    if label is not None and (not isinstance(label, str) or not label.strip() or len(label.strip()) > 100):
                        raise ValueError("label must be 1-100 characters")
                    if enabled is not None and not isinstance(enabled, bool):
                        raise ValueError("enabled must be boolean")
                    updated = store.update_blacklist_rule(rule_id, label=label, enabled=enabled)
                    if updated is None:
                        self._send(404, {"error": "rule_not_found"}); return
                    self._send(200, updated); return
                self._send(405, {"error": "method_not_allowed"}); return
            if path == "/api/event-triggers":
                if method != "POST":
                    self._send(405, {"error": "method_not_allowed"}); return
                body = self._body()
                if body is None: return
                request_id = body.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("request_id is required")
                if (not isinstance(body.get("config_version"), int)
                        or not isinstance(body.get("parameters"), dict)
                        or not isinstance(body.get("interval_minutes"), int)
                        or not isinstance(body.get("enabled", True), bool)
                        or body.get("wish_id") is not None and not isinstance(body.get("wish_id"), str)):
                    raise ValueError("invalid trigger payload")
                payload_hash = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                created = store.create_trigger(
                    request_id=request_id, request_hash=payload_hash, wish_id=body.get("wish_id"),
                    trigger_type=str(body.get("trigger_type", "")), config_version=body.get("config_version"),
                    parameters=body.get("parameters"), interval_minutes=body.get("interval_minutes"), enabled=body.get("enabled", True),
                )
                self._send(201, created); return
            if path.startswith("/api/event-triggers/"):
                trigger_id = path.removeprefix("/api/event-triggers/")
                if not trigger_id or "/" in trigger_id:
                    self._send(404, {"error": "not_found"}); return
                if method == "DELETE":
                    if not store.delete_trigger(trigger_id):
                        self._send(404, {"error": "trigger_not_found"}); return
                    self._send(204, b"", "application/json; charset=utf-8"); return
                if method == "PATCH":
                    body = self._body()
                    if body is None: return
                    allowed = {"wish_id", "parameters", "interval_minutes", "enabled"}
                    if not body or set(body) - allowed:
                        raise ValueError("invalid trigger patch")
                    updated = store.patch_trigger(trigger_id, body)
                    if updated is None:
                        self._send(404, {"error": "trigger_not_found"}); return
                    self._send(200, updated); return
                self._send(405, {"error": "method_not_allowed"}); return
            if not path.startswith("/api/device-management/"):
                self._send(404, {"error": "not_found"}); return
            device_id = path.removeprefix("/api/device-management/")
            if not device_id or "/" in device_id:
                self._send(404, {"error": "not_found"}); return
            if method in {"PATCH", "POST"}:
                body = self._body()
                if body is None: return
                name = body.get("display_name")
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
                    raise ValueError("display_name must be 1-100 characters")
                updated = store.rename_device(device_id, name)
                if updated is None:
                    self._send(404, {"error": "device_not_found"}); return
                self._send(200, updated); return
            if method == "DELETE":
                deleted = store.retire_device(device_id)
                if deleted is None:
                    self._send(404, {"error": "device_not_found"}); return
                if not deleted:
                    self._send(409, {"error": "device_delete_rejected"}); return
                self._send(204, b"", "application/json; charset=utf-8"); return
            self._send(405, {"error": "method_not_allowed"})
        except ValueError as error:
            self._send(400, {"error": "invalid_request", "message": str(error)})
        except sqlite3.IntegrityError:
            self._send(409, {"error": "duplicate_rule", "message": "黑名单规则已存在"})
        except sqlite3.Error:
            self._send(500, {"error": "storage_error", "message": "central dashboard write failed"})

    def _create_invitation(self) -> None:
        try:
            endpoint = self._configured_endpoint()
            created = create_invitation(self.server.data_server.store, central_base_url=str(endpoint["base_url"]),
                                        scope="dashboard", lifetime=timedelta(hours=24))
            self._send(201, {"code": created.code, "expires_at": created.expires_at})
        except (ValueError, OSError) as error:
            self._send(409, {"error": "invitation_unavailable", "message": str(error)})

    def _configured_endpoint(self) -> dict[str, Any]:
        if self.server.config.config_path is None: raise ValueError("中央配置不可写，无法签发远程设备配对码")
        payload = _read_external_config(str(self.server.config.config_path)).get("public_endpoint")
        expected_instance = self.server.data_server.store.ai_readers.central_instance_id()
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("base_url"), str)
                or not isinstance(payload.get("verified_at"), str)
                or payload.get("central_instance_id") != expected_instance):
            raise ValueError("请先完成已验证的外部 HTTPS 地址设置")
        return payload

    def _create_package(self) -> None:
        if not self.server.ai_package_lock.acquire(blocking=False):
            self._send(409, {
                "error": "ai_package_in_progress",
                "message": "AI 配对包正在生成，请等待当前操作完成。",
            })
            return
        try:
            endpoint = self._configured_endpoint()
            checked = verify_public_endpoint(str(endpoint["base_url"]), self.server.data_server, self.server.config)
            package = create_connection_package(store=self.server.data_server.store, external_origin=checked["base_url"])
            self._send_attachment(package.filename, package.payload)
        except Exception as error:
            self._send(409, {"error": "ai_connection_package_unavailable", "message": str(error)})
        finally:
            self.server.ai_package_lock.release()

    def _verify_network(self) -> None:
        body = self._body()
        if body is None: return
        provider, url = body.get("provider"), body.get("base_url")
        if provider not in PROVIDERS:
            self._send(400, {"error": "invalid_provider", "message": "未知连接方式"}); return
        try:
            checked = verify_public_endpoint(str(url), self.server.data_server, self.server.config)
            if self.server.config.config_path is None: raise ValueError("中央配置不可写")
            public_endpoint = self._save_public_endpoint(str(provider), checked["base_url"], checked["central_instance_id"])
            self._send(200, {"status": "verified", "public_endpoint": public_endpoint})
        except Exception as error:
            self._send(422, {"error": "endpoint_verification_failed", "message": str(error)})

    def _save_public_endpoint(self, provider: str, base_url: str, central_instance_id: str) -> dict[str, Any]:
        if self.server.config.config_path is None:
            raise ValueError("中央配置不可写")
        path = self.server.config.config_path
        current = _read_external_config(str(path))
        updated = dict(current)
        public_endpoint = {
            "version": 1,
            "provider": provider,
            "base_url": base_url,
            "central_instance_id": central_instance_id,
            "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        updated["public_endpoint"] = public_endpoint
        _secure_atomic_write_json(path, updated)
        return {key: public_endpoint[key] for key in ("provider", "base_url", "central_instance_id", "verified_at")}

    def _detect_tailscale(self) -> None:
        try:
            endpoint = detect_https_endpoint()
            self._send(200, {
                "status": "detected",
                "base_url": endpoint,
                "message": "已检测到 Tailscale 地址；请确认 Serve 转发后，再点击“验证并保存”。",
            })
        except TailscaleSetupError as error:
            self._send(422, {"error": "tailscale_detection_failed", "message": str(error)})

    def _shutdown(self) -> None:
        expected = os.environ.get("LIFE_LINK_MANAGEMENT_TOKEN")
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not expected or not hmac.compare_digest(supplied, expected):
            self._send(401, {"error": "invalid_management_token"}); return
        self._send(202, {"status": "shutting_down"})
        if self.server.shutdown_callback:
            threading.Thread(target=self.server.shutdown_callback, daemon=True).start()


def create_management_server(data_server: Any, config: CentralConfig, address: tuple[str, int] = (MANAGEMENT_HOST, MANAGEMENT_PORT),
                             shutdown_callback: Callable[[], None] | None = None) -> ManagementHTTPServer:
    if address[0] != MANAGEMENT_HOST:
        raise ValueError("management service must bind only to 127.0.0.1")
    management = ManagementHTTPServer(address, data_server, config, shutdown_callback)
    # The public HTTPS data service validates browser sessions, then forwards
    # authenticated page operations here over loopback.  The management server
    # itself remains permanently loopback-only.
    data_server.management_server = management
    return management
