#!/usr/bin/env python3
"""Life Link local MCP adapter with AI Reader pairing and cursor handling."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


SERVER_NAME = "life-link-mcp"
SERVER_TITLE = "Life Link"
SERVER_VERSION = "0.1.0"
STATE_SCHEMA = "life-link-mcp-state/v1"
PACKAGE_SCHEMA = "life-link-ai-mcp-connection-package/v1"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DPAPI_DESCRIPTION = "Life Link MCP state v1"


class LifeLinkMCPError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_state_dir() -> Path:
    configured = os.environ.get("LIFE_LINK_MCP_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    root = os.environ.get("LIFE_LINK_DATA_ROOT") or os.environ.get("LIFE_LINK_RUNTIME_ROOT")
    if root:
        return Path(os.path.expandvars(root)).expanduser() / "ai" / "mcp"
    profile = os.environ.get("USERPROFILE")
    return (Path(profile) if profile else Path.home()) / "LifeLink" / "ai" / "mcp"


def _safe_profile_id(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise LifeLinkMCPError("invalid_package", "MCP profile ID is invalid") from error
    normalized = str(parsed)
    if text != normalized:
        raise LifeLinkMCPError("invalid_package", "MCP profile ID is not canonical")
    return normalized


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(DPAPI_DESCRIPTION.encode("utf-8"))
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob), DPAPI_DESCRIPTION, ctypes.byref(entropy_blob),
        None, None, 0x01, ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
        del input_buffer, entropy_buffer


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(DPAPI_DESCRIPTION.encode("utf-8"))
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, ctypes.byref(entropy_blob),
        None, None, 0x01, ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
        del input_buffer, entropy_buffer


class StateStore:
    def __init__(self, root: Path, profile_id: str):
        self.root = root.resolve()
        self.profile_id = _safe_profile_id(profile_id)
        self.path = self.root / "profiles" / f"{self.profile_id}.json"

    def load(self) -> dict[str, Any] | None:
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise LifeLinkMCPError("state_unreadable", "Life Link MCP state is unreadable") from error
        if not isinstance(envelope, dict) or envelope.get("schema_version") != STATE_SCHEMA:
            raise LifeLinkMCPError("state_unreadable", "Life Link MCP state has an unsupported format")
        if envelope.get("profile_id") != self.profile_id:
            raise LifeLinkMCPError("state_unreadable", "Life Link MCP state profile does not match")
        protection = envelope.get("protection")
        if protection != ("dpapi-current-user" if os.name == "nt" else "file-permissions"):
            raise LifeLinkMCPError("state_unreadable", "Life Link MCP state protection does not match this platform")
        try:
            encrypted = base64.b64decode(str(envelope["protected_payload"]), validate=True)
            payload = json.loads(_dpapi_unprotect(encrypted).decode("utf-8"))
        except Exception as error:
            raise LifeLinkMCPError("state_unreadable", "Life Link MCP state cannot be decrypted for this user") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            raise LifeLinkMCPError("state_unreadable", "Life Link MCP state is incomplete")
        return {**envelope, "secret": payload}

    def save(
        self, *, central_instance_id: str, reader: dict[str, Any], secret: dict[str, Any],
        created_at: str | None = None, disabled_reason: str | None = None,
    ) -> None:
        now = utc_now_text()
        encoded = json.dumps(secret, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        protected = base64.b64encode(_dpapi_protect(encoded)).decode("ascii")
        envelope = {
            "schema_version": STATE_SCHEMA,
            "profile_id": self.profile_id,
            "central_instance_id": central_instance_id,
            "reader": reader,
            "created_at": created_at or now,
            "updated_at": now,
            "disabled_reason": disabled_reason,
            "protection": "dpapi-current-user" if os.name == "nt" else "file-permissions",
            "protected_payload": protected,
        }
        _atomic_write_json(self.path, envelope)


class ConnectionPackage:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest_path = self.directory / "manifest.json"
        self.pairing_path = self.directory / "pairing.json"
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LifeLinkMCPError("invalid_package", "MCP connection package manifest is missing or invalid") from error
        if not isinstance(manifest, dict) or manifest.get("schema_version") != PACKAGE_SCHEMA:
            raise LifeLinkMCPError("invalid_package", "MCP connection package version is unsupported")
        self.manifest = manifest
        self.profile_id = _safe_profile_id(manifest.get("mcp_profile_id"))

    def reader_identity(self, client_info: dict[str, Any]) -> dict[str, Any]:
        identity_path = self.directory / "reader.json"
        if not identity_path.is_file():
            return _reader_identity(client_info, self.profile_id)
        try:
            document = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LifeLinkMCPError("invalid_reader_identity", "reader.json is invalid") from error
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != "life-link-mcp-reader/v1"
            or not isinstance(document.get("reader"), dict)
        ):
            raise LifeLinkMCPError("invalid_reader_identity", "reader.json has an unsupported format")
        reader = dict(document["reader"])
        allowed = {"type", "instance_id", "display_name", "process_binding"}
        if set(reader) - allowed:
            raise LifeLinkMCPError("invalid_reader_identity", "reader.json contains unsupported fields")
        reader_type = reader.get("type")
        instance_id = reader.get("instance_id")
        display_name = reader.get("display_name")
        if not isinstance(reader_type, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", reader_type):
            raise LifeLinkMCPError("invalid_reader_identity", "reader type is invalid")
        if not isinstance(instance_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", instance_id):
            raise LifeLinkMCPError("invalid_reader_identity", "reader instance ID is invalid")
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 100:
            raise LifeLinkMCPError("invalid_reader_identity", "reader display name is invalid")
        if display_name.strip() == "AI Companion":
            display_name = str(
                client_info.get("title") or client_info.get("name") or display_name
            ).strip()[:100]
        normalized: dict[str, Any] = {
            "type": reader_type,
            "instance_id": instance_id,
            "display_name": display_name,
        }
        binding = reader.get("process_binding")
        if binding is not None:
            if not isinstance(binding, dict):
                raise LifeLinkMCPError("invalid_reader_identity", "process binding must be an object")
            strategy = binding.get("strategy")
            expected = {"strategy", "display_name", "process_name"}
            if strategy == "hosted-argument":
                expected.add("argument_path_segments")
            if strategy not in {"process-name", "hosted-argument"} or set(binding) != expected:
                raise LifeLinkMCPError("invalid_reader_identity", "process binding fields are invalid")
            if not isinstance(binding.get("display_name"), str) or not binding["display_name"].strip():
                raise LifeLinkMCPError("invalid_reader_identity", "process display name is invalid")
            if not isinstance(binding.get("process_name"), str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,128}\.exe", binding["process_name"],
            ):
                raise LifeLinkMCPError("invalid_reader_identity", "process name must be an exact .exe name")
            if strategy == "hosted-argument":
                segments = binding.get("argument_path_segments")
                if (
                    not isinstance(segments, list) or not 1 <= len(segments) <= 8
                    or any(
                        not isinstance(item, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,128}", item)
                        for item in segments
                    )
                ):
                    raise LifeLinkMCPError("invalid_reader_identity", "hosted process path segments are invalid")
            normalized["process_binding"] = binding
        return normalized

    def load_pairing(self) -> dict[str, Any]:
        try:
            pairing = json.loads(self.pairing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LifeLinkMCPError(
                "pairing_unavailable",
                "No usable one-time pairing material remains; generate a new MCP connection package",
            ) from error
        required = {"central_instance_id", "claim_url", "pairing_id", "pairing_token"}
        if not isinstance(pairing, dict) or not required.issubset(pairing):
            raise LifeLinkMCPError("invalid_package", "One-time pairing material is incomplete")
        parsed = urlparse(str(pairing["claim_url"]))
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.path != "/v1/ai-readers/pairings/claim"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise LifeLinkMCPError("invalid_package", "Pairing endpoint must be the local Life Link claim endpoint")
        return pairing

    def discard_pairing(self) -> None:
        try:
            self.pairing_path.unlink(missing_ok=True)
        except OSError:
            pass


def _http_json(
    method: str, url: str, *, token: str, body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read()
    except HTTPError as error:
        status = int(error.code)
        raw = error.read()
    except (URLError, TimeoutError, OSError) as error:
        raise LifeLinkMCPError("central_unreachable", "Life Link central service is unavailable") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifeLinkMCPError("invalid_central_response", "Life Link central returned an invalid response") from error
    if not isinstance(payload, dict):
        raise LifeLinkMCPError("invalid_central_response", "Life Link central response must be an object")
    return status, payload


def _reader_type(client_info: dict[str, Any]) -> str:
    source = str(client_info.get("name") or "mcp-client").lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", source).strip("-._")[:52]
    if not slug or not slug[0].isalpha():
        slug = "client"
    return f"mcp.{slug}"[:64]


def _reader_identity(client_info: dict[str, Any], profile_id: str) -> dict[str, str]:
    display_name = str(
        client_info.get("title") or client_info.get("name") or "AI Companion"
    ).strip()[:100]
    return {
        "type": _reader_type(client_info),
        "instance_id": f"mcp:{profile_id}",
        "display_name": display_name or "AI Companion",
    }


class LifeLinkReader:
    def __init__(
        self, package: ConnectionPackage, state_store: StateStore,
        *, http_json: Callable[..., tuple[int, dict[str, Any]]] = _http_json,
    ):
        self.package = package
        self.state_store = state_store
        self.http_json = http_json

    def status(self) -> dict[str, Any]:
        state = self.state_store.load()
        if state is None:
            return {
                "connected": False,
                "profile_id": self.package.profile_id,
                "pairing_available": self.package.pairing_path.is_file(),
            }
        secret = state["secret"]
        expires = parse_utc(secret.get("expires_at"))
        return {
            "connected": not state.get("disabled_reason") and (
                expires is None or datetime.now(timezone.utc) < expires
            ),
            "profile_id": self.package.profile_id,
            "central_instance_id": state.get("central_instance_id"),
            "reader_id": secret.get("reader_id"),
            "reader": state.get("reader"),
            "token_expires_at": secret.get("expires_at"),
            "disabled_reason": state.get("disabled_reason"),
        }

    def pair(self, client_info: dict[str, Any]) -> dict[str, Any]:
        existing = self.state_store.load()
        if existing is not None and not existing.get("disabled_reason"):
            return existing
        pairing = self.package.load_pairing()
        template = pairing.get("claim_request_body_template")
        reader = self.package.reader_identity(client_info)
        claim = {
            "schema_version": (
                template.get("schema_version") if isinstance(template, dict)
                else "life-radio-ai-reader-pairing-claim-v1"
            ),
            "pairing_id": pairing["pairing_id"],
            "reader": reader,
        }
        status, profile = self.http_json(
            "POST", str(pairing["claim_url"]), token=str(pairing["pairing_token"]), body=claim,
        )
        if status != 200:
            error_code = str(profile.get("error") or "pairing_failed")
            raise LifeLinkMCPError(error_code, "Life Link pairing was rejected", http_status=status)
        required = {"access_token", "reader_id", "expires_at", "context_url"}
        if not required.issubset(profile) or not all(isinstance(profile[key], str) for key in required):
            raise LifeLinkMCPError("invalid_central_response", "Life Link pairing response is incomplete")
        context_url = urlparse(str(profile["context_url"]))
        if (
            context_url.scheme not in {"http", "https"}
            or context_url.hostname not in {"127.0.0.1", "::1", "localhost"}
            or context_url.path != "/v1/read/ai/context"
            or context_url.username or context_url.password
            or context_url.query or context_url.fragment
        ):
            raise LifeLinkMCPError("invalid_central_response", "Life Link context endpoint is invalid")
        secret = {
            "access_token": profile["access_token"],
            "reader_id": profile["reader_id"],
            "expires_at": profile["expires_at"],
            "context_url": profile["context_url"],
            "next_cursor": None,
            "understanding_version": None,
        }
        self.state_store.save(
            central_instance_id=str(pairing["central_instance_id"]),
            reader=reader,
            secret=secret,
        )
        self.package.discard_pairing()
        loaded = self.state_store.load()
        if loaded is None:
            raise LifeLinkMCPError("state_unreadable", "Life Link MCP state was not saved")
        return loaded

    def read_context(self, client_info: dict[str, Any], *, view: str = "compact") -> dict[str, Any]:
        if view not in {"compact", "full"}:
            raise LifeLinkMCPError("invalid_arguments", "view must be compact or full")
        state = self.state_store.load() or self.pair(client_info)
        if state.get("disabled_reason"):
            raise LifeLinkMCPError("connection_disabled", "Life Link connection requires a new pairing")
        secret = dict(state["secret"])
        params: dict[str, str] = {"view": view}
        if secret.get("next_cursor"):
            params["cursor"] = str(secret["next_cursor"])
        if secret.get("understanding_version"):
            params["understanding_version"] = str(secret["understanding_version"])

        def request_context(active_params: dict[str, str]) -> tuple[int, dict[str, Any]]:
            return self.http_json(
                "GET", f"{secret['context_url']}?{urlencode(active_params)}",
                token=str(secret["access_token"]),
            )

        status, payload = request_context(params)
        if status in {409, 410} and "cursor" in params:
            params.pop("cursor", None)
            secret["next_cursor"] = None
            status, payload = request_context(params)
        if status == 401:
            self.state_store.save(
                central_instance_id=str(state["central_instance_id"]),
                reader=dict(state["reader"]), secret=secret,
                created_at=str(state["created_at"]), disabled_reason="token_invalid",
            )
            raise LifeLinkMCPError("token_invalid", "Life Link connection requires a new pairing", http_status=401)
        if status != 200:
            raise LifeLinkMCPError(
                str(payload.get("error") or "context_read_failed"),
                "Life Link context read failed", http_status=status,
            )
        next_cursor = payload.get("next_cursor")
        understanding = payload.get("understanding")
        if not isinstance(next_cursor, str) or not isinstance(understanding, dict):
            raise LifeLinkMCPError("invalid_central_response", "Life Link context response is incomplete")
        understanding_version = understanding.get("version")
        if not isinstance(understanding_version, str):
            raise LifeLinkMCPError("invalid_central_response", "Life Link understanding version is missing")
        secret["next_cursor"] = next_cursor
        secret["understanding_version"] = understanding_version
        self.state_store.save(
            central_instance_id=str(state["central_instance_id"]),
            reader=dict(state["reader"]), secret=secret,
            created_at=str(state["created_at"]),
        )
        return payload

    def check_updates(self) -> dict[str, Any]:
        state = self.state_store.load()
        if state is None or state.get("disabled_reason"):
            return {"connected": False, "update_mcp": False}
        secret = dict(state["secret"])
        updates_url = str(secret["context_url"]).removesuffix("/context") + "/updates"
        params = {"cursor": str(secret["next_cursor"])} if secret.get("next_cursor") else {}
        status, payload = self.http_json(
            "GET", updates_url + (f"?{urlencode(params)}" if params else ""),
            token=str(secret["access_token"]),
        )
        if status != 200 or not isinstance(payload.get("update_mcp"), bool):
            raise LifeLinkMCPError(str(payload.get("error") or "update_check_failed"), "Life Link update check failed", http_status=status)
        return {"connected": True, "update_mcp": payload["update_mcp"]}


TOOLS = [
    {
        "name": "lifelink_connection_status",
        "title": "Life Link 连接状态",
        "description": "检查本机 Life Link MCP 是否已经配对；不访问个人上下文，也不会推进读取游标。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "lifelink_check_updates",
        "title": "检查 Life Link 提醒更新",
        "description": "轻量检查是否有尚未读取的提醒类事件。update_mcp=true 时再调用 lifelink_read_context；不会读取正文或推进游标。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "lifelink_read_context",
        "title": "读取 Life Link 上下文",
        "description": (
            "读取中央已经整理好的个人背景、当前状态和相对上次游标新增的事件。"
            "首次调用会使用连接包中的一次性授权自动配对。默认使用 compact。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string", "enum": ["compact", "full"], "default": "compact",
                    "description": "默认 compact；只有确实需要内部结构时才使用 full。",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": False, "openWorldHint": True,
        },
    },
]


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": serialized}],
        "structuredContent": payload,
        "isError": is_error,
    }


class MCPServer:
    def __init__(self, reader: LifeLinkReader):
        self.reader = reader
        self.client_info: dict[str, Any] = {"name": "mcp-client", "version": "unknown"}
        self.initialized = False

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request") if "id" in message else None
        if "id" not in message:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")
        try:
            if method == "initialize":
                client_info = params.get("clientInfo")
                if isinstance(client_info, dict):
                    self.client_info = client_info
                requested = str(params.get("protocolVersion") or "")
                negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
                return self._result(request_id, {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION,
                    },
                    "instructions": (
                        "Life Link 只读连接。优先调用 lifelink_connection_status 检查状态；"
                        "需要个人背景或新增事件时调用 lifelink_read_context。"
                    ),
                })
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": TOOLS})
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    return self._error(request_id, -32602, "Tool arguments must be an object")
                if name == "lifelink_connection_status":
                    return self._result(request_id, _tool_result(self.reader.status()))
                if name == "lifelink_check_updates":
                    return self._result(request_id, _tool_result(self.reader.check_updates()))
                if name == "lifelink_read_context":
                    unexpected = set(arguments) - {"view"}
                    if unexpected:
                        return self._error(request_id, -32602, "Unknown tool arguments")
                    payload = self.reader.read_context(
                        self.client_info, view=str(arguments.get("view") or "compact"),
                    )
                    return self._result(request_id, _tool_result(payload))
                return self._error(request_id, -32602, f"Unknown tool: {name}")
            return self._error(request_id, -32601, "Method not found")
        except LifeLinkMCPError as error:
            safe = {"error": error.code, "message": str(error)}
            if error.http_status is not None:
                safe["http_status"] = error.http_status
            return self._result(request_id, _tool_result(safe, is_error=True))
        except Exception:
            return self._result(request_id, _tool_result({
                "error": "internal_error", "message": "Life Link MCP encountered an internal error",
            }, is_error=True))

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def run_stdio(self) -> None:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                response = self.handle(message)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "Parse error")
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()


def _make_reader(package_dir: Path, state_dir: Path | None = None) -> LifeLinkReader:
    package = ConnectionPackage(package_dir)
    store = StateStore(state_dir or default_state_dir(), package.profile_id)
    return LifeLinkReader(package, store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Life Link local MCP adapter")
    subparsers = parser.add_subparsers(dest="command")
    for command in ("serve", "status", "pair"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--package-dir", type=Path, default=executable_dir())
        subparser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    command = args.command or "serve"
    try:
        reader = _make_reader(
            getattr(args, "package_dir", executable_dir()),
            getattr(args, "state_dir", None),
        )
        if command == "serve":
            MCPServer(reader).run_stdio()
            return 0
        if command == "status":
            print(json.dumps(reader.status(), ensure_ascii=False))
            return 0
        if command == "pair":
            state = reader.pair({"name": "life-link-mcp", "title": "AI Companion"})
            print(json.dumps({
                "connected": True,
                "profile_id": reader.package.profile_id,
                "central_instance_id": state.get("central_instance_id"),
                "reader_id": state["secret"].get("reader_id"),
            }, ensure_ascii=False))
            return 0
    except LifeLinkMCPError as error:
        print(json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
