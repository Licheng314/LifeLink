#!/usr/bin/env python3
"""Loopback-only WebUI for claiming a Life Link central invitation."""

from __future__ import annotations

import base64
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from central_client_setup import (
    _validate_token,
    validate_central_base_url,
    validate_client_profile,
    write_client_profile,
)
from device_identity import device_descriptor
from runtime_paths import resource_dir


INVITATION_PREFIX = "LR1."
CLAIM_SCHEMA = "life-radio-enrollment-claim-v1"
INVITATION_FIELDS = {
    "v", "invitation_id", "central_base_url", "invitation_token", "scope",
    "expires_at",
}
MAX_BODY_BYTES = 64 * 1024
BASE_DIR = resource_dir()
SETUP_HTML = BASE_DIR / "central_client_setup.html"


class EnrollmentClaimError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Invitation:
    invitation_id: str
    central_base_url: str
    scope: str
    expires_at: str
    invitation_token: str = field(repr=False)


@dataclass
class SetupState:
    device: dict[str, str]
    config_path: Path
    identity_path: Path
    allow_loopback_http: bool = False
    previews: dict[str, Invitation] = field(default_factory=dict, repr=False)
    completed_profile: dict[str, Any] | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def _utc_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field_name} must be a UTC ISO-8601 timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid UTC ISO-8601 timestamp") from error
    return parsed.astimezone(timezone.utc)


def parse_invitation_code(
    code: Any,
    *,
    allow_loopback_http: bool = False,
    now: datetime | None = None,
) -> Invitation:
    if not isinstance(code, str) or not code.startswith(INVITATION_PREFIX):
        raise ValueError("邀请码必须以 LR1. 开头")
    encoded = code[len(INVITATION_PREFIX):]
    if not encoded or len(encoded) > 16_384 or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise ValueError("邀请码格式无效")
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(
            encoded + padding, altchars=b"-_", validate=True,
        ).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("邀请码内容无法解析") from error
    if not isinstance(payload, dict) or set(payload) != INVITATION_FIELDS:
        raise ValueError("邀请码字段与 LR1 v1 格式不一致")
    if payload.get("v") != 1:
        raise ValueError("邀请码版本不受支持")
    invitation_id = payload.get("invitation_id")
    if not isinstance(invitation_id, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
        invitation_id,
    ):
        raise ValueError("邀请码 invitation_id 无效")
    scope = payload.get("scope")
    if scope not in {"upload", "dashboard"}:
        raise ValueError("邀请码权限范围无效")
    expires_at = payload.get("expires_at")
    expiry = _utc_datetime(expires_at, "expires_at")
    if expiry <= (now or datetime.now(timezone.utc)):
        raise ValueError("邀请码已过期")
    return Invitation(
        invitation_id=invitation_id,
        central_base_url=validate_central_base_url(
            payload.get("central_base_url"),
            allow_loopback_http=allow_loopback_http,
        ),
        invitation_token=_validate_token(
            payload.get("invitation_token"), "invitation_token",
        ),
        scope=scope,
        expires_at=str(expires_at),
    )


def invitation_preview(invitation: Invitation, device: dict[str, str]) -> dict[str, str]:
    return {
        "central_base_url": invitation.central_base_url,
        "scope": invitation.scope,
        "scope_label": "上传与多设备 Dashboard 读取" if invitation.scope == "dashboard" else "仅上传本机数据",
        "expires_at": invitation.expires_at,
        "device_name": device["display_name"],
    }


def claim_invitation(
    invitation: Invitation,
    device: dict[str, str],
    *,
    allow_loopback_http: bool = False,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    if _utc_datetime(invitation.expires_at, "expires_at") <= datetime.now(timezone.utc):
        raise EnrollmentClaimError("邀请码已过期，请重新获取")
    claim = {
        "schema_version": CLAIM_SCHEMA,
        "invitation_id": invitation.invitation_id,
        "device": {
            "device_id": device["device_id"],
            "platform": "desktop",
            "display_name": device["display_name"],
        },
    }
    request = Request(
        f"{invitation.central_base_url}/v1/enrollments/claim",
        data=json.dumps(claim, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {invitation.invitation_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    open_request = opener
    if open_request is None:
        safe_opener = build_opener(ProxyHandler({}), NoRedirect())
        open_request = safe_opener.open
    try:
        with open_request(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            body = response.read()
    except HTTPError as error:
        if error.code in {401, 403, 404, 409, 410}:
            raise EnrollmentClaimError("邀请码无效、已使用或已过期") from error
        if error.code == 429:
            raise EnrollmentClaimError("请求过于频繁，请稍后重试") from error
        raise EnrollmentClaimError("中央服务暂时无法完成注册") from error
    except (URLError, TimeoutError, OSError) as error:
        raise EnrollmentClaimError("无法连接中央服务，请检查网络后重试") from error
    if status != 200:
        raise EnrollmentClaimError("中央服务未能完成注册")
    try:
        profile = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnrollmentClaimError("中央服务返回了无效配置") from error
    try:
        normalized = validate_client_profile(
            profile,
            local_device_id=device["device_id"],
            allow_loopback_http=allow_loopback_http,
        )
    except ValueError as error:
        raise EnrollmentClaimError("中央服务返回的客户端配置校验失败") from error
    if normalized["central_base_url"] != invitation.central_base_url:
        raise EnrollmentClaimError("中央服务返回了不匹配的服务地址")
    if invitation.scope == "upload" and normalized.get("read_token"):
        raise EnrollmentClaimError("中央服务返回的权限超出邀请码范围")
    return normalized


class ThreadedSetupServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: SetupState):
        super().__init__(address, SetupHandler)
        self.state = state


class SetupHandler(BaseHTTPRequestHandler):
    server: ThreadedSetupServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        if size < 1 or size > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("请求必须是 UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:
        if self.path == "/":
            body = SETUP_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/setup/context":
            self.send_json(200, {
                "device": self.server.state.device,
                "configured": False,
            })
            return
        self.send_json(404, {"error": "not_found", "message": "页面不存在"})

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            if self.path == "/api/setup/preview":
                self.handle_preview(payload)
            elif self.path == "/api/setup/claim":
                self.handle_claim(payload)
            else:
                self.send_json(404, {"error": "not_found", "message": "接口不存在"})
        except ValueError as error:
            self.send_json(400, {"error": "invalid_request", "message": str(error)})

    def handle_preview(self, payload: dict[str, Any]) -> None:
        invitation = parse_invitation_code(
            payload.get("invite_code"),
            allow_loopback_http=self.server.state.allow_loopback_http,
        )
        preview_id = secrets.token_urlsafe(18)
        with self.server.state.lock:
            self.server.state.previews.clear()
            self.server.state.previews[preview_id] = invitation
        self.send_json(200, {
            "preview_id": preview_id,
            "preview": invitation_preview(invitation, self.server.state.device),
        })

    def handle_claim(self, payload: dict[str, Any]) -> None:
        preview_id = payload.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id:
            raise ValueError("请先解析邀请码")
        with self.server.state.lock:
            invitation = self.server.state.previews.pop(preview_id, None)
        if invitation is None:
            raise ValueError("预览已失效，请重新粘贴邀请码")
        try:
            profile = claim_invitation(
                invitation,
                self.server.state.device,
                allow_loopback_http=self.server.state.allow_loopback_http,
            )
            write_client_profile(
                profile,
                config_path=self.server.state.config_path,
                identity_path=self.server.state.identity_path,
                allow_loopback_http=self.server.state.allow_loopback_http,
            )
        except EnrollmentClaimError as error:
            self.send_json(502, {"error": "claim_failed", "message": str(error)})
            return
        except (OSError, ValueError):
            self.send_json(500, {
                "error": "config_write_failed",
                "message": "客户端配置无法安全保存，请检查本机权限后重试",
            })
            return
        with self.server.state.lock:
            self.server.state.completed_profile = profile
            self.server.state.previews.clear()
        self.send_json(200, {"status": "configured", "message": "配置成功，正在启动 Life Link。"})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def create_setup_server(
    *,
    config_path: Path,
    identity_path: Path,
    port: int = 8090,
    allow_loopback_http: bool = False,
) -> ThreadedSetupServer:
    device = device_descriptor(identity_path=identity_path)
    state = SetupState(
        device=device,
        config_path=config_path,
        identity_path=identity_path,
        allow_loopback_http=allow_loopback_http,
    )
    return ThreadedSetupServer(("127.0.0.1", port), state)
