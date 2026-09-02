"""Passive, read-only AI reader pairing, cursors, and access auditing."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from .domain import canonical_json, utc_timestamp

if TYPE_CHECKING:
    from .storage import CentralStore


PAIRING_SCHEMA = "life-radio-ai-reader-pairing-v1"
CLAIM_SCHEMA = "life-radio-ai-reader-pairing-claim-v1"
PAIRING_LIFETIME = timedelta(hours=24)
TOKEN_LIFETIME = timedelta(days=90)
CURSOR_LIFETIME = timedelta(days=90)
READER_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CONTEXT_VIEWS = {"full", "compact"}
AI_READER_AUDIT_EVENT_KEYS = {
    "system.ai_reader_connected",
    "system.ai_reader_context_served",
}

PROCESS_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}\.exe$", re.IGNORECASE)
PROCESS_ARGUMENT_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.@+-]{1,128}$")


def _arguments_contain_path_segments(
    arguments: list[str], expected_segments: list[str]
) -> bool:
    """Match consecutive, case-insensitive path segments in one process argument."""
    expected = [segment.casefold() for segment in expected_segments]
    for argument in arguments[1:]:
        segments = [
            segment for segment in re.split(r"[\\/]+", str(argument).casefold())
            if segment
        ]
        if any(segments[index:index + len(expected)] == expected
               for index in range(len(segments) - len(expected) + 1)):
            return True
    return False


def _windows_command_line_arguments(command_line: str) -> list[str]:
    """Parse one Windows command line without retaining or logging its contents."""
    if sys.platform != "win32" or not command_line:
        return []
    import ctypes

    argument_count = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(
        ctypes.c_wchar_p(command_line), ctypes.byref(argument_count)
    )
    if not argv:
        return []
    try:
        return [str(argv[index]) for index in range(argument_count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def _normalize_process_binding(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("reader.process_binding must be an object")
    strategy = value.get("strategy")
    expected_keys = {"strategy", "display_name", "process_name"}
    if strategy == "hosted-argument":
        expected_keys.add("argument_path_segments")
    if set(value) != expected_keys or strategy not in {"process-name", "hosted-argument"}:
        raise ValueError(
            "reader.process_binding must use process-name or hosted-argument fields"
        )
    display_name = value.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 100:
        raise ValueError("reader.process_binding.display_name must contain 1 to 100 characters")
    process_name = value.get("process_name")
    if not isinstance(process_name, str) or not PROCESS_NAME_PATTERN.fullmatch(process_name):
        raise ValueError("reader.process_binding.process_name must be an exact .exe file name")
    binding = {
        "strategy": strategy,
        "display_name": display_name.strip(),
        "process_name": process_name.casefold(),
    }
    if strategy == "hosted-argument":
        segments = value.get("argument_path_segments")
        if (
            not isinstance(segments, list)
            or not 1 <= len(segments) <= 8
            or any(
                not isinstance(segment, str)
                or not PROCESS_ARGUMENT_SEGMENT_PATTERN.fullmatch(segment)
                for segment in segments
            )
        ):
            raise ValueError(
                "reader.process_binding.argument_path_segments must contain 1 to 8 stable path segments"
            )
        binding["argument_path_segments"] = [segment.casefold() for segment in segments]
    return binding


def detect_process_binding(binding: dict[str, Any] | None) -> bool | None:
    """Detect one paired Windows application without retaining command lines."""
    if sys.platform != "win32":
        return None
    if not binding:
        return None
    process_name = str(binding.get("process_name") or "")
    if not PROCESS_NAME_PATTERN.fullmatch(process_name):
        return None
    include_arguments = binding.get("strategy") == "hosted-argument"
    expected_segments = list(binding.get("argument_path_segments") or [])
    if include_arguments and not expected_segments:
        return None
    select_fields = "Name,CommandLine" if include_arguments else "Name"
    query = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        f"$items=Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" "
        f"-ErrorAction Stop | Select-Object {select_fields};"
        "@($items) | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return None
        payload = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    rows = payload if isinstance(payload, list) else [payload]
    saw_process = False
    saw_readable_command_line = False
    for row in rows:
        if not isinstance(row, dict) or str(row.get("Name") or "").casefold() != process_name.casefold():
            continue
        saw_process = True
        if not include_arguments:
            return True
        command_line = str(row.get("CommandLine") or "")
        if not command_line:
            continue
        saw_readable_command_line = True
        arguments = _windows_command_line_arguments(command_line)
        if _arguments_contain_path_segments(arguments, expected_segments):
            return True
    if include_arguments and saw_process and not saw_readable_command_line:
        return None
    return False


def _stored_process_binding(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        return _normalize_process_binding(json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _reader_process_status(row: sqlite3.Row) -> tuple[bool | None, str | None]:
    binding = _stored_process_binding(row["process_binding_json"])
    if binding is not None:
        return detect_process_binding(binding), str(binding["display_name"])
    return process_identity_matches(
        executable_path=row["process_executable_path"],
        pid=row["process_pid"],
        started_at=row["process_started_at"],
    ), None


AI_READER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_readers (
    reader_id TEXT PRIMARY KEY,
    reader_type TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    paired_at TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    cursor_epoch INTEGER NOT NULL CHECK (cursor_epoch >= 1),
    last_requested_at TEXT,
    last_requested_cursor_created_at TEXT,
    last_requested_timeline_event_id TEXT,
    last_served_at TEXT,
    last_served_cursor_created_at TEXT,
    last_served_timeline_event_id TEXT,
    process_executable_path TEXT,
    process_pid INTEGER,
    process_started_at TEXT,
    process_binding_json TEXT,
    UNIQUE(reader_type, instance_id)
);

CREATE TABLE IF NOT EXISTS ai_reader_pairings (
    pairing_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    central_instance_id TEXT NOT NULL,
    claim_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_reader_id TEXT REFERENCES ai_readers(reader_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_reader_pairings_expires
    ON ai_reader_pairings(expires_at);

CREATE TABLE IF NOT EXISTS ai_reader_cursors (
    cursor_hash TEXT PRIMARY KEY,
    reader_id TEXT NOT NULL REFERENCES ai_readers(reader_id),
    cursor_epoch INTEGER NOT NULL,
    business_date TEXT,
    position_created_at TEXT,
    timeline_event_id TEXT,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK (
        (position_created_at IS NULL AND timeline_event_id IS NULL)
        OR (position_created_at IS NOT NULL AND timeline_event_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_ai_reader_cursors_reader
    ON ai_reader_cursors(reader_id, cursor_epoch, issued_at);
CREATE INDEX IF NOT EXISTS idx_timeline_created_for_ai_readers
    ON timeline_events(created_at, timeline_event_id);

CREATE TABLE IF NOT EXISTS ai_reader_access_logs (
    access_log_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    reader_id TEXT NOT NULL REFERENCES ai_readers(reader_id),
    requested_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    result TEXT NOT NULL,
    cursor_epoch INTEGER,
    business_date TEXT,
    requested_cursor_created_at TEXT,
    requested_timeline_event_id TEXT,
    served_cursor_created_at TEXT,
    served_timeline_event_id TEXT,
    served_event_ids_json TEXT NOT NULL,
    served_report_ids_json TEXT NOT NULL,
    importance_counts_json TEXT NOT NULL,
    background_generated_at TEXT,
    understanding_version TEXT,
    response_hash TEXT,
    response_bytes INTEGER,
    duration_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_reader_access_logs_reader
    ON ai_reader_access_logs(reader_id, requested_at DESC, access_log_id DESC);
"""


class AIReaderError(RuntimeError):
    error_code = "ai_reader_error"


class AIReaderInvalidToken(AIReaderError):
    error_code = "invalid_ai_reader_token"


class AIReaderTokenExpired(AIReaderError):
    error_code = "ai_reader_token_expired"


class AIReaderNotFound(AIReaderError):
    error_code = "ai_reader_not_found"


class AIReaderPairingInvalid(AIReaderError):
    error_code = "invalid_ai_reader_pairing"


class AIReaderPairingExpired(AIReaderError):
    error_code = "ai_reader_pairing_expired"


class AIReaderPairingAlreadyClaimed(AIReaderError):
    error_code = "ai_reader_pairing_already_claimed"


class AIReaderCursorInvalid(AIReaderError):
    error_code = "invalid_cursor"


class AIReaderCursorExpired(AIReaderError):
    error_code = "cursor_expired"


class AIReaderCursorSuperseded(AIReaderError):
    error_code = "cursor_superseded"


@dataclass(frozen=True)
class CreatedAIReaderPairing:
    text: str
    pairing_id: str
    expires_at: str
    central_instance_id: str
    claim_url: str


@dataclass(frozen=True)
class ServedAIContext:
    payload: dict[str, Any]
    body: bytes


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must use UTC Z form")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc)


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def is_loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def normalize_loopback_claim_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not is_loopback_address(parsed.hostname)
        or parsed.username
        or parsed.password
        or parsed.path != "/v1/ai-readers/pairings/claim"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("claim_url must be the loopback AI reader claim endpoint")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, "", ""))


def _normalize_process_identity(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "executable_path", "pid", "started_at"
    }:
        raise ValueError(
            "reader.process_identity must contain only executable_path, pid, and started_at"
        )
    executable_path = value.get("executable_path")
    if (
        not isinstance(executable_path, str)
        or not executable_path.strip()
        or not os.path.isabs(executable_path)
        or len(executable_path) > 4096
    ):
        raise ValueError("reader.process_identity.executable_path must be an absolute path")
    pid = value.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ValueError("reader.process_identity.pid must be a positive integer")
    started_at = value.get("started_at")
    try:
        normalized_started_at = utc_timestamp(_parse_utc(started_at))
    except (TypeError, ValueError) as error:
        raise ValueError(
            "reader.process_identity.started_at must be a UTC ISO timestamp"
        ) from error
    return {
        "executable_path": os.path.normpath(executable_path.strip()),
        "pid": pid,
        "started_at": normalized_started_at,
    }


def _windows_process_identity(pid: int) -> dict[str, Any] | None:
    """Read one process identity without spawning a shell or a helper process."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return None
    try:
        path_buffer = ctypes.create_unicode_buffer(32768)
        path_length = wintypes.DWORD(len(path_buffer))
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, path_buffer, ctypes.byref(path_length)
        ):
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        started_at = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=ticks // 10
        )
        return {
            "executable_path": os.path.normpath(path_buffer.value),
            "pid": pid,
            "started_at": utc_timestamp(started_at),
        }
    finally:
        kernel32.CloseHandle(handle)


def process_identity_matches(
    *, executable_path: str | None, pid: int | None, started_at: str | None
) -> bool | None:
    """Match one local process without depending on sub-millisecond clock precision."""
    if not executable_path or pid is None or not started_at:
        return None
    actual = _windows_process_identity(int(pid))
    if actual is None:
        return None
    expected_path = os.path.normcase(os.path.normpath(executable_path))
    actual_path = os.path.normcase(os.path.normpath(str(actual["executable_path"])))
    if actual_path != expected_path or actual["pid"] != int(pid):
        return False
    # Windows FILETIME commonly has finer precision than the timestamp an AI
    # runtime can report.  PID and exact executable path identify the process;
    # start time only guards against PID reuse, so tolerate a small conversion
    # difference instead of requiring equal serialized strings.
    try:
        expected_started = _parse_utc(started_at)
        actual_started = _parse_utc(str(actual["started_at"]))
    except (TypeError, ValueError):
        return False
    return abs((actual_started - expected_started).total_seconds()) <= 1.0


def validate_claim_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "pairing_id",
        "reader",
    }:
        raise ValueError(
            "claim must contain only schema_version, pairing_id, and reader"
        )
    if payload.get("schema_version") != CLAIM_SCHEMA:
        raise ValueError(f"schema_version must be {CLAIM_SCHEMA}")
    try:
        parsed_id = uuid.UUID(str(payload.get("pairing_id")))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("pairing_id must be a UUID") from error
    if payload.get("pairing_id") != str(parsed_id):
        raise ValueError("pairing_id must use canonical UUID form")
    reader = payload.get("reader")
    if not isinstance(reader, dict) or set(reader) - {
        "type", "instance_id", "display_name", "process_identity", "process_binding"
    } or not {"type", "instance_id", "display_name"}.issubset(reader):
        raise ValueError(
            "reader must contain type, instance_id, display_name, and optional process binding"
        )
    if reader.get("process_identity") is not None and reader.get("process_binding") is not None:
        raise ValueError("reader must not combine process_identity and process_binding")
    reader_type = reader.get("type")
    if not isinstance(reader_type, str) or not READER_TYPE_PATTERN.fullmatch(
        reader_type
    ):
        raise ValueError("reader.type must be a lowercase stable type")
    instance_id = reader.get("instance_id")
    if (
        not isinstance(instance_id, str)
        or not INSTANCE_ID_PATTERN.fullmatch(instance_id)
    ):
        raise ValueError("reader.instance_id must be a stable 1 to 128 character identifier")
    display_name = reader.get("display_name")
    if (
        not isinstance(display_name, str)
        or not display_name.strip()
        or len(display_name.strip()) > 100
    ):
        raise ValueError("reader.display_name must contain 1 to 100 characters")
    return {
        "pairing_id": str(parsed_id),
        "reader": {
            "type": reader_type,
            "instance_id": instance_id,
            "display_name": display_name.strip(),
            "process_identity": _normalize_process_identity(
                reader.get("process_identity")
            ),
            "process_binding": _normalize_process_binding(reader.get("process_binding")),
        },
    }


class AIReaderService:
    def __init__(self, store: CentralStore) -> None:
        self.store = store
        self._initialize()

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _initialize(self) -> None:
        with self.store._connection() as connection:
            connection.executescript(AI_READER_SCHEMA)
            cursor_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ai_reader_cursors)"
                ).fetchall()
            }
            if "business_date" not in cursor_columns:
                connection.execute(
                    "ALTER TABLE ai_reader_cursors ADD COLUMN business_date TEXT"
                )
            access_log_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ai_reader_access_logs)"
                ).fetchall()
            }
            if "cursor_epoch" not in access_log_columns:
                connection.execute(
                    "ALTER TABLE ai_reader_access_logs ADD COLUMN cursor_epoch INTEGER"
                )
            reader_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ai_readers)"
                ).fetchall()
            }
            for column, definition in (
                ("process_executable_path", "TEXT"),
                ("process_pid", "INTEGER"),
                ("process_started_at", "TEXT"),
                ("process_binding_json", "TEXT"),
            ):
                if column not in reader_columns:
                    connection.execute(
                        f"ALTER TABLE ai_readers ADD COLUMN {column} {definition}"
                    )
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT value FROM kv WHERE key = 'central_instance_id'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO kv(key, value) VALUES ('central_instance_id', ?)",
                        (f"central-{uuid.uuid4()}",),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def central_instance_id(self) -> str:
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT value FROM kv WHERE key = 'central_instance_id'"
            ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("central instance identity is missing")
        return str(row["value"])

    def create_pairing(
        self,
        *,
        claim_url: str,
        central_instance_id: str | None = None,
        central_display_name: str | None = None,
        lifetime: timedelta = PAIRING_LIFETIME,
        now: datetime | None = None,
    ) -> CreatedAIReaderPairing:
        if lifetime.total_seconds() <= 0:
            raise ValueError("pairing lifetime must be positive")
        normalized_url = normalize_loopback_claim_url(claim_url)
        stable_instance_id = self.central_instance_id()
        if central_instance_id is not None:
            if (
                not isinstance(central_instance_id, str)
                or central_instance_id.strip() != stable_instance_id
            ):
                raise ValueError("central_instance_id is stable and cannot be replaced")
        current = _now(now)
        pairing_id = str(uuid.uuid4())
        pairing_token = secrets.token_urlsafe(32)
        expires_at = utc_timestamp(current + lifetime)
        central_instance_id = stable_instance_id
        with self.store._connection() as connection:
            connection.execute(
                """
                INSERT INTO ai_reader_pairings(
                    pairing_id, token_hash, central_instance_id, claim_url,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pairing_id,
                    self.token_hash(pairing_token),
                    central_instance_id,
                    normalized_url,
                    utc_timestamp(current),
                    expires_at,
                ),
            )
        text = canonical_json(
            {
                "schema_version": PAIRING_SCHEMA,
                "central_instance_id": central_instance_id,
                "central_display_name": (
                    central_display_name.strip()
                    if isinstance(central_display_name, str) and central_display_name.strip()
                    else "Life Link Central"
                ),
                "claim_url": normalized_url,
                "pairing_id": pairing_id,
                "pairing_token": pairing_token,
                "expires_at": expires_at,
                "instructions": [
                    (
                        "这是 Life Link 的一次只读连接任务。先按 central_instance_id 判断是否已有有效连接；"
                        "已有连接不要重复 claim。若未连接，立即按模板 claim。"
                    ),
                    (
                        "未连接时向 claim_url POST，Authorization 使用 Bearer pairing_token，正文使用模板并替换 reader 身份。"
                        "若 AI 与中央运行在同一台 Windows 机器，按实际应用填写 process_binding；"
                        "原生应用使用精确进程文件名，Node/Python/Java 等宿主应用还必须填写能标识应用的连续参数路径段。"
                        "无法准确确认时删除 process_binding，不能只绑定 node.exe、python.exe 或 java.exe。"
                    ),
                    (
                        "claim 成功后立即私密保存一次性返回的 access_token、reader_id、expires_at、context_url，"
                        "并立刻用 access_token GET context_url 完成首次读取（首次不带 cursor）。"
                        "JSON 用 UTF-8 处理，不要直接用 Windows 默认 GBK print；必要时写 UTF-8 文件或设置 stdout。"
                        "后续保存并提交最新 next_cursor 和 understanding.version；遇到 409/410 丢弃 cursor，"
                        "无 cursor 重试一次。默认用 compact，只有确需完整结构时才用 view=full。"
                    ),
                ],
                "claim_request_body_template": {
                    "schema_version": CLAIM_SCHEMA,
                    "pairing_id": pairing_id,
                    "reader": {
                        "type": "replace-with-stable-reader-type",
                        "instance_id": "replace-with-stable-instance-id",
                        "display_name": "replace-with-reader-display-name",
                        "process_binding": {
                            "strategy": "hosted-argument",
                            "display_name": "REPLACE_WITH_APPLICATION_NAME",
                            "process_name": "REPLACE_WITH_EXACT_HOST_PROCESS_NAME.exe",
                            "argument_path_segments": [
                                "REPLACE_WITH_STABLE_APPLICATION_PATH_SEGMENT"
                            ]
                        },
                    },
                },
            }
        )
        return CreatedAIReaderPairing(
            text=text,
            pairing_id=pairing_id,
            expires_at=expires_at,
            central_instance_id=central_instance_id,
            claim_url=normalized_url,
        )

    def claim_pairing(
        self,
        *,
        pairing_token: str,
        claim: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _now(now)
        claimed_at = utc_timestamp(current)
        token_expires_at = utc_timestamp(current + TOKEN_LIFETIME)
        supplied_hash = self.token_hash(pairing_token)
        reader = claim["reader"]
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                pairing = connection.execute(
                    "SELECT * FROM ai_reader_pairings WHERE pairing_id = ?",
                    (claim["pairing_id"],),
                ).fetchone()
                if pairing is None or not hmac.compare_digest(
                    str(pairing["token_hash"]), supplied_hash
                ):
                    raise AIReaderPairingInvalid("pairing is missing or invalid")
                if current >= _parse_utc(str(pairing["expires_at"])):
                    raise AIReaderPairingExpired("pairing has expired")
                if pairing["claimed_at"] is not None:
                    raise AIReaderPairingAlreadyClaimed(
                        "pairing has already returned its permanent token"
                    )

                existing = connection.execute(
                    """
                    SELECT * FROM ai_readers
                    WHERE reader_type = ? AND instance_id = ?
                    """,
                    (reader["type"], reader["instance_id"]),
                ).fetchone()
                reader_id = (
                    str(existing["reader_id"])
                    if existing is not None
                    else str(uuid.uuid4())
                )
                permanent_token = secrets.token_urlsafe(32)
                permanent_hash = self.token_hash(permanent_token)
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO ai_readers(
                            reader_id, reader_type, instance_id, display_name,
                            created_at, paired_at, token_hash, token_expires_at,
                            revoked_at, cursor_epoch, process_executable_path,
                            process_pid, process_started_at, process_binding_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?)
                        """,
                        (
                            reader_id,
                            reader["type"],
                            reader["instance_id"],
                            reader["display_name"],
                            claimed_at,
                            claimed_at,
                            permanent_hash,
                            token_expires_at,
                            (reader["process_identity"] or {}).get("executable_path"),
                            (reader["process_identity"] or {}).get("pid"),
                            (reader["process_identity"] or {}).get("started_at"),
                            canonical_json(reader["process_binding"])
                            if reader["process_binding"] else None,
                        ),
                    )
                    cursor_epoch = 1
                else:
                    cursor_epoch = int(existing["cursor_epoch"]) + 1
                    process_identity = reader["process_identity"]
                    process_binding = reader["process_binding"]
                    if process_identity is None and process_binding is None:
                        process_identity = {
                            "executable_path": existing["process_executable_path"],
                            "pid": existing["process_pid"],
                            "started_at": existing["process_started_at"],
                        }
                        if not any(process_identity.values()):
                            process_identity = None
                        process_binding = (
                            json.loads(existing["process_binding_json"])
                            if existing["process_binding_json"] else None
                        )
                    connection.execute(
                        """
                        UPDATE ai_readers
                        SET display_name = ?, paired_at = ?, token_hash = ?,
                            token_expires_at = ?, revoked_at = NULL,
                            cursor_epoch = ?, process_executable_path = ?,
                            process_pid = ?, process_started_at = ?,
                            process_binding_json = ?
                        WHERE reader_id = ?
                        """,
                        (
                            reader["display_name"],
                            claimed_at,
                            permanent_hash,
                            token_expires_at,
                            cursor_epoch,
                            (process_identity or {}).get("executable_path"),
                            (process_identity or {}).get("pid"),
                            (process_identity or {}).get("started_at"),
                            canonical_json(process_binding) if process_binding else None,
                            reader_id,
                        ),
                    )
                # Personal-mode Life Link has exactly one connected AI
                # companion. Claiming a new identity atomically revokes every
                # other reader token while preserving its audit history.
                connection.execute(
                    """
                    UPDATE ai_readers
                    SET revoked_at = ?, cursor_epoch = cursor_epoch + 1
                    WHERE reader_id <> ? AND revoked_at IS NULL
                    """,
                    (claimed_at, reader_id),
                )
                connection.execute(
                    """
                    UPDATE ai_reader_pairings
                    SET claimed_at = ?, claimed_reader_id = ?
                    WHERE pairing_id = ?
                    """,
                    (claimed_at, reader_id, claim["pairing_id"]),
                )
                self._insert_audit_event(
                    connection,
                    occurred_at=claimed_at,
                    event_key="system.ai_reader_connected",
                    title=f"AI 已连接 · {reader['display_name']}",
                    detail=(
                        f"{reader['display_name']} 已与 Life Link 建立只读连接，"
                        f"Token 有效至 {self._local_timestamp(token_expires_at)}。"
                    ),
                    subject={
                        "reader_id": reader_id,
                        "reader_display_name": reader["display_name"],
                    },
                    evidence={
                        "pairing_id": claim["pairing_id"],
                        "token_expires_at": token_expires_at,
                    },
                    dedupe_key=f"ai_reader.connected|{claim['pairing_id']}",
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        context_url = str(pairing["claim_url"]).removesuffix(
            "/v1/ai-readers/pairings/claim"
        ) + "/v1/read/ai/context"
        return {
            "access_token": permanent_token,
            "reader_id": reader_id,
            "expires_at": token_expires_at,
            "context_url": context_url,
        }

    def authenticate(
        self, token: str | None, *, now: datetime | None = None, touch: bool = True
    ) -> dict[str, Any]:
        if not token:
            raise AIReaderInvalidToken("AI reader Bearer token is missing or invalid")
        current = _now(now)
        digest = self.token_hash(token)
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM ai_readers WHERE token_hash = ?", (digest,)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise AIReaderInvalidToken(
                    "AI reader Bearer token is missing or invalid"
                )
            if current >= _parse_utc(str(row["token_expires_at"])):
                raise AIReaderTokenExpired("AI reader token has expired")
            if touch:
                connection.execute(
                    "UPDATE ai_readers SET last_requested_at = ? WHERE reader_id = ?",
                    (utc_timestamp(current), row["reader_id"]),
                )
            return dict(row)

    def check_updates(
        self, reader: dict[str, Any], *, cursor: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Check for unread high-priority events without serving content or advancing state."""
        current = _now(now)
        settings = self.store.get_shared_settings()
        local = current.astimezone(timezone(timedelta(hours=8))) - timedelta(
            hours=int(settings["day_start_hour"])
        )
        business_date = local.date().isoformat()
        start_at, end_at = self._business_window(
            business_date, int(settings["day_start_hour"])
        )
        reader_id = str(reader["reader_id"])
        with self.store._connection() as connection:
            fresh = connection.execute(
                "SELECT cursor_epoch FROM ai_readers WHERE reader_id = ? AND revoked_at IS NULL",
                (reader_id,),
            ).fetchone()
            if fresh is None:
                raise AIReaderInvalidToken("AI reader is no longer active")
            position: tuple[datetime, str] | None = None
            if cursor:
                cursor_row = connection.execute(
                    "SELECT * FROM ai_reader_cursors WHERE cursor_hash = ?",
                    (self.token_hash(cursor),),
                ).fetchone()
                if cursor_row is None or str(cursor_row["reader_id"]) != reader_id:
                    raise AIReaderCursorInvalid("cursor is missing or invalid")
                if int(cursor_row["cursor_epoch"]) != int(fresh["cursor_epoch"]):
                    raise AIReaderCursorSuperseded("cursor was superseded by a reader reset or re-pairing")
                if current >= _parse_utc(str(cursor_row["expires_at"])):
                    raise AIReaderCursorExpired("cursor has expired")
                if cursor_row["business_date"] == business_date and cursor_row["position_created_at"]:
                    position = (
                        _parse_utc(str(cursor_row["position_created_at"])),
                        str(cursor_row["timeline_event_id"]),
                    )
            rows = connection.execute(
                "SELECT created_at, timeline_event_id, event_key FROM timeline_events "
                "WHERE occurred_at >= ? AND occurred_at < ? "
                "AND (importance = 'high' OR wish_id IS NOT NULL OR trigger_id IS NOT NULL)",
                (start_at, end_at),
            ).fetchall()
            pending = [row for row in rows if str(row["event_key"]) not in AI_READER_AUDIT_EVENT_KEYS and (
                position is None or (_parse_utc(str(row["created_at"])), str(row["timeline_event_id"])) > position
            )]
        return {"update_mcp": bool(pending)}

    @staticmethod
    def _timeline_event(row: sqlite3.Row, device_names: dict[str, str]) -> dict[str, Any]:
        subject = json.loads(row["subject_json"])
        evidence = json.loads(row["evidence_json"])
        referenced_device_id = subject.get("device_id") if isinstance(subject, dict) else None
        referenced_device_id = referenced_device_id or row["source_device_id"]
        device_name = device_names.get(str(referenced_device_id)) if referenced_device_id else None
        title = str(row["title"])
        if row["event_key"] == "device_usage_milestone" and device_name and not row["wish_id"]:
            title = f"设备使用·{device_name}"
        return {
            "timeline_event_id": row["timeline_event_id"],
            "occurred_at": row["occurred_at"],
            "created_at": row["created_at"],
            "event_key": row["event_key"],
            "category": row["category"],
            "importance": row["importance"],
            "title": title,
            "detail": row["detail"],
            "source_kind": row["source_kind"],
            "source_device_id": row["source_device_id"],
            "device_display_name": device_name,
            "wish_id": row["wish_id"],
            "trigger_id": row["trigger_id"],
            "subject": subject,
            "evidence": evidence,
            "statistics_window": (
                json.loads(row["statistics_window_json"])
                if row["statistics_window_json"]
                else None
            ),
            "delivery": (
                json.loads(row["delivery_json"])
                if row["delivery_json"]
                else None
            ),
            "dedupe_key": row["dedupe_key"],
        }

    @staticmethod
    def _business_window(
        business_date: str, day_start_hour: int
    ) -> tuple[str, str]:
        local_zone = timezone(timedelta(hours=8))
        start_local = datetime.combine(
            date.fromisoformat(business_date),
            datetime.min.time(),
            local_zone,
        ) + timedelta(hours=day_start_hour)
        end_local = start_local + timedelta(days=1)
        return (
            utc_timestamp(start_local.astimezone(timezone.utc)),
            utc_timestamp(end_local.astimezone(timezone.utc)),
        )

    @staticmethod
    def _position(row: sqlite3.Row | None) -> tuple[str | None, str | None]:
        if row is None:
            return None, None
        return str(row["created_at"]), str(row["timeline_event_id"])

    @staticmethod
    def _local_timestamp(value: str) -> str:
        return _parse_utc(value).astimezone(
            timezone(timedelta(hours=8))
        ).isoformat()

    @classmethod
    def _compact_payload(
        cls,
        *,
        background: dict[str, Any],
        events: list[dict[str, Any]],
        understanding_version: str,
        understanding: dict[str, Any],
        known_understanding_version: str | None,
        next_cursor: str,
    ) -> dict[str, Any]:
        background_lines: list[str] = []
        summary = background.get("background_summary") or {}
        for key in ("wish", "device_and_apps", "blacklist", "location_and_activity"):
            section = summary.get(key) or {}
            title = str(section.get("title") or "").strip()
            for item in section.get("items") or []:
                text = str(item.get("text") or "").strip()
                if text:
                    background_lines.append(f"{title}：{text}" if title else text)

        current_by_device: dict[str, list[str]] = {}
        current_order: list[str] = []
        for item in background.get("real_time_items") or []:
            if not item.get("include_in_ai"):
                continue
            identity = str(item.get("device_id") or item.get("kind") or "unknown")
            if identity not in current_by_device:
                current_by_device[identity] = []
                current_order.append(identity)
            text = str(item.get("display_text") or "").strip().rstrip("。")
            if text:
                current_by_device[identity].append(text)
        current_lines = [
            "；".join(current_by_device[identity]) + "。"
            for identity in current_order
            if current_by_device[identity]
        ]

        compact_events: list[dict[str, Any]] = []
        for event in events:
            evidence = event.get("evidence") or {}
            if str(event.get("event_key") or "").startswith("report.") and evidence.get("body"):
                text = str(evidence["body"])
            else:
                title = str(event.get("title") or "").strip()
                detail = str(event.get("detail") or "").strip()
                text = f"{title}：{detail}" if title and detail else title or detail
            compact_events.append(
                {
                    "at": cls._local_timestamp(str(event["occurred_at"])),
                    "importance": event["importance"],
                    "text": text,
                }
            )

        if known_understanding_version == understanding_version:
            compact_understanding: dict[str, Any] = {
                "version": understanding_version,
                "unchanged": True,
            }
        else:
            compact_understanding = {
                "version": understanding_version,
                "unchanged": False,
                "items": [
                    str(item.get("text") or "")
                    for item in understanding.get("items") or []
                    if str(item.get("text") or "")
                ],
            }
        return {
            "business_date": background["business_date"],
            "timezone": "Asia/Shanghai",
            "generated_at": cls._local_timestamp(str(background["generated_at"])),
            "generated_at_label": background.get("generated_at_label"),
            "understanding": compact_understanding,
            "background": background_lines,
            "current": current_lines,
            "events": compact_events,
            "next_cursor": next_cursor,
        }

    def _record_error(
        self,
        *,
        reader_id: str,
        request_id: str,
        requested_at: str,
        result: str,
        started: float,
        requested_position: tuple[str | None, str | None] = (None, None),
    ) -> None:
        completed_at = utc_timestamp()
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        with self.store._connection() as connection:
            reader = connection.execute(
                "SELECT cursor_epoch FROM ai_readers WHERE reader_id = ?",
                (reader_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO ai_reader_access_logs(
                    access_log_id, request_id, reader_id, requested_at,
                    completed_at, result, cursor_epoch, requested_cursor_created_at,
                    requested_timeline_event_id, served_event_ids_json,
                    served_report_ids_json, importance_counts_json, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]',
                          '{"high":0,"normal":0,"low":0}', ?)
                """,
                (
                    str(uuid.uuid4()),
                    request_id,
                    reader_id,
                    requested_at,
                    completed_at,
                    result,
                    int(reader["cursor_epoch"]) if reader is not None else None,
                    requested_position[0],
                    requested_position[1],
                    duration_ms,
                ),
            )

    def serve_context(
        self,
        reader: dict[str, Any],
        *,
        cursor: str | None,
        business_date: str | None,
        known_understanding_version: str | None,
        view: str = "compact",
        now: datetime | None = None,
    ) -> ServedAIContext:
        if view not in CONTEXT_VIEWS:
            raise ValueError("view must be full or compact")
        started = time.perf_counter()
        current = _now(now)
        requested_at = utc_timestamp(current)
        request_id = str(uuid.uuid4())
        reader_id = str(reader["reader_id"])
        requested_position: tuple[str | None, str | None] = (None, None)

        background = self.store.event_background(business_date, now=current)
        selected_business_date = str(background["business_date"])
        understanding = dict(background.pop("ai_understanding"))
        understanding_version = "sha256:" + hashlib.sha256(
            canonical_json(understanding).encode("utf-8")
        ).hexdigest()[:16]
        settings = self.store.get_shared_settings()
        start_at, end_at = self._business_window(
            selected_business_date, int(settings["day_start_hour"])
        )

        try:
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    fresh_reader = connection.execute(
                        "SELECT * FROM ai_readers WHERE reader_id = ?", (reader_id,)
                    ).fetchone()
                    if fresh_reader is None or fresh_reader["revoked_at"] is not None:
                        raise AIReaderInvalidToken("AI reader is no longer active")
                    if current >= _parse_utc(str(fresh_reader["token_expires_at"])):
                        raise AIReaderTokenExpired("AI reader token has expired")

                    epoch = int(fresh_reader["cursor_epoch"])
                    all_rows = sorted(
                        connection.execute("SELECT * FROM timeline_events").fetchall(),
                        key=lambda row: (
                            _parse_utc(str(row["created_at"])),
                            str(row["timeline_event_id"]),
                        ),
                    )
                    start_key = _parse_utc(start_at)
                    end_key = _parse_utc(end_at)
                    business_rows = [
                        row
                        for row in all_rows
                        if start_key
                        <= _parse_utc(str(row["occurred_at"]))
                        < end_key
                        and str(row["importance"]) != "low"
                        and str(row["event_key"]) not in AI_READER_AUDIT_EVENT_KEYS
                    ]
                    if cursor is not None:
                        cursor_row = connection.execute(
                            "SELECT * FROM ai_reader_cursors WHERE cursor_hash = ?",
                            (self.token_hash(cursor),),
                        ).fetchone()
                        if (
                            cursor_row is None
                            or str(cursor_row["reader_id"]) != reader_id
                        ):
                            raise AIReaderCursorInvalid("cursor is missing or invalid")
                        requested_position = (
                            cursor_row["position_created_at"],
                            cursor_row["timeline_event_id"],
                        )
                        if int(cursor_row["cursor_epoch"]) != epoch:
                            raise AIReaderCursorSuperseded(
                                "cursor was superseded by a reader reset or re-pairing"
                            )
                        if current >= _parse_utc(str(cursor_row["expires_at"])):
                            raise AIReaderCursorExpired("cursor has expired")
                        cursor_business_date = cursor_row["business_date"]
                        if cursor_business_date != selected_business_date:
                            # Business-day rollover is automatic: the previous
                            # day's bookmark must not expose prior-day events.
                            rows = business_rows
                        elif requested_position[0] is None:
                            rows = business_rows
                        else:
                            requested_key = (
                                _parse_utc(str(requested_position[0])),
                                str(requested_position[1]),
                            )
                            rows = [
                                row
                                for row in business_rows
                                if (
                                    _parse_utc(str(row["created_at"])),
                                    str(row["timeline_event_id"]),
                                )
                                > requested_key
                            ]
                        served_position = self._position(
                            business_rows[-1] if business_rows else None
                        )
                    else:
                        rows = business_rows
                        served_position = self._position(
                            business_rows[-1] if business_rows else None
                        )

                    device_names = {
                        str(row["device_id"]): str(row["effective_name"])
                        for row in connection.execute(
                            """
                            SELECT device_id,
                                   COALESCE(custom_name, display_name) AS effective_name
                            FROM devices
                            """
                        ).fetchall()
                    }
                    events = [
                        self._timeline_event(row, device_names) for row in rows
                    ]
                    next_cursor = secrets.token_urlsafe(32)
                    importance_counts = {"high": 0, "normal": 0, "low": 0}
                    for event in events:
                        importance_counts[str(event["importance"])] += 1
                    if view == "compact":
                        payload = self._compact_payload(
                            background=background,
                            events=events,
                            understanding_version=understanding_version,
                            understanding=understanding,
                            known_understanding_version=known_understanding_version,
                            next_cursor=next_cursor,
                        )
                    elif known_understanding_version == understanding_version:
                        understanding_payload: dict[str, Any] = {
                            "version": understanding_version,
                            "unchanged": True,
                        }
                        payload = {
                            "understanding": understanding_payload,
                            "background": background,
                            "events": events,
                            "importance_counts": importance_counts,
                            "next_cursor": next_cursor,
                        }
                    else:
                        understanding_payload = {
                            "version": understanding_version,
                            "unchanged": False,
                            "content": understanding,
                        }
                        payload = {
                            "understanding": understanding_payload,
                            "background": background,
                            "events": events,
                            "importance_counts": importance_counts,
                            "next_cursor": next_cursor,
                        }
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    response_hash = hashlib.sha256(body).hexdigest()
                    completed_at = utc_timestamp()
                    duration_ms = max(
                        0, round((time.perf_counter() - started) * 1000)
                    )
                    event_ids = [
                        str(event["timeline_event_id"]) for event in events
                    ]
                    report_ids = [
                        str(event["timeline_event_id"])
                        for event in events
                        if str(event["event_key"]).startswith("report.")
                    ]

                    connection.execute(
                        """
                        INSERT INTO ai_reader_cursors(
                            cursor_hash, reader_id, cursor_epoch,
                            business_date, position_created_at, timeline_event_id,
                            issued_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.token_hash(next_cursor),
                            reader_id,
                            epoch,
                            selected_business_date,
                            served_position[0],
                            served_position[1],
                            requested_at,
                            utc_timestamp(current + CURSOR_LIFETIME),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO ai_reader_access_logs(
                            access_log_id, request_id, reader_id, requested_at,
                            completed_at, result, cursor_epoch, business_date,
                            requested_cursor_created_at,
                            requested_timeline_event_id,
                            served_cursor_created_at, served_timeline_event_id,
                            served_event_ids_json, served_report_ids_json,
                            importance_counts_json, background_generated_at,
                            understanding_version, response_hash, response_bytes,
                            duration_ms
                        ) VALUES (?, ?, ?, ?, ?, 'served', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            request_id,
                            reader_id,
                            requested_at,
                            completed_at,
                            epoch,
                            selected_business_date,
                            requested_position[0],
                            requested_position[1],
                            served_position[0],
                            served_position[1],
                            canonical_json(event_ids),
                            canonical_json(report_ids),
                            canonical_json(importance_counts),
                            background["generated_at"],
                            understanding_version,
                            response_hash,
                            len(body),
                            duration_ms,
                        ),
                    )
                    self._insert_audit_event(
                        connection,
                        occurred_at=completed_at,
                        event_key="system.ai_reader_context_served",
                        title=f"AI 已访问 Life Link · {fresh_reader['display_name']}",
                        detail=(
                            f"Life Link 已向 {fresh_reader['display_name']} 提供"
                            f"当前业务日背景和 {len(events)} 条事件。"
                        ),
                        subject={
                            "reader_id": reader_id,
                            "reader_display_name": fresh_reader["display_name"],
                        },
                        evidence={
                            "request_id": request_id,
                            "business_date": selected_business_date,
                            "served_event_count": len(events),
                        },
                        dedupe_key=f"ai_reader.context_served|{request_id}",
                    )
                    connection.execute(
                        """
                        UPDATE ai_readers
                        SET last_requested_cursor_created_at = ?,
                            last_requested_timeline_event_id = ?,
                            last_served_at = ?,
                            last_served_cursor_created_at = ?,
                            last_served_timeline_event_id = ?
                        WHERE reader_id = ?
                        """,
                        (
                            requested_position[0],
                            requested_position[1],
                            completed_at,
                            served_position[0],
                            served_position[1],
                            reader_id,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except AIReaderError as error:
            self._record_error(
                reader_id=reader_id,
                request_id=request_id,
                requested_at=requested_at,
                result=error.error_code,
                started=started,
                requested_position=requested_position,
            )
            raise

        return ServedAIContext(payload=payload, body=body)

    def preview_next_context(
        self,
        reader_id: str,
        *,
        business_date: str | None = None,
        view: str = "compact",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Preview the next well-behaved reader response without side effects.

        The preview follows the latest cursor issued for the reader's current
        epoch.  It deliberately does not authenticate as the reader, issue a
        usable cursor, write an access log, update reader state, or create an
        audit event.
        """
        if view not in CONTEXT_VIEWS:
            raise ValueError("view must be full or compact")
        current = _now(now)
        background = self.store.event_background(business_date, now=current)
        selected_business_date = str(background["business_date"])
        understanding = dict(background.pop("ai_understanding"))
        understanding_version = "sha256:" + hashlib.sha256(
            canonical_json(understanding).encode("utf-8")
        ).hexdigest()[:16]
        settings = self.store.get_shared_settings()
        start_at, end_at = self._business_window(
            selected_business_date, int(settings["day_start_hour"])
        )

        with self.store._connection() as connection:
            reader = connection.execute(
                "SELECT * FROM ai_readers WHERE reader_id = ?", (reader_id,)
            ).fetchone()
            if reader is None:
                raise AIReaderNotFound("AI reader was not found")
            if reader["revoked_at"] is not None or current >= _parse_utc(
                str(reader["token_expires_at"])
            ):
                raise AIReaderInvalidToken("AI reader is not active")

            epoch = int(reader["cursor_epoch"])
            cursor_row = connection.execute(
                """
                SELECT * FROM ai_reader_cursors
                WHERE reader_id = ? AND cursor_epoch = ?
                ORDER BY issued_at DESC, rowid DESC
                LIMIT 1
                """,
                (reader_id, epoch),
            ).fetchone()
            latest_log = connection.execute(
                """
                SELECT understanding_version FROM ai_reader_access_logs
                WHERE reader_id = ? AND result = 'served'
                ORDER BY requested_at DESC, access_log_id DESC
                LIMIT 1
                """,
                (reader_id,),
            ).fetchone()
            known_understanding_version = (
                str(latest_log["understanding_version"])
                if latest_log is not None and latest_log["understanding_version"]
                else None
            )
            all_rows = sorted(
                connection.execute("SELECT * FROM timeline_events").fetchall(),
                key=lambda row: (
                    _parse_utc(str(row["created_at"])),
                    str(row["timeline_event_id"]),
                ),
            )
            start_key = _parse_utc(start_at)
            end_key = _parse_utc(end_at)
            business_rows = [
                row
                for row in all_rows
                if start_key <= _parse_utc(str(row["occurred_at"])) < end_key
                and str(row["importance"]) != "low"
                and str(row["event_key"]) not in AI_READER_AUDIT_EVENT_KEYS
            ]
            if (
                cursor_row is None
                or cursor_row["business_date"] != selected_business_date
                or cursor_row["position_created_at"] is None
            ):
                rows = business_rows
            else:
                cursor_key = (
                    _parse_utc(str(cursor_row["position_created_at"])),
                    str(cursor_row["timeline_event_id"]),
                )
                rows = [
                    row
                    for row in business_rows
                    if (
                        _parse_utc(str(row["created_at"])),
                        str(row["timeline_event_id"]),
                    )
                    > cursor_key
                ]
            device_names = {
                str(row["device_id"]): str(row["effective_name"])
                for row in connection.execute(
                    """
                    SELECT device_id,
                           COALESCE(custom_name, display_name) AS effective_name
                    FROM devices
                    """
                ).fetchall()
            }
            events = [self._timeline_event(row, device_names) for row in rows]

        importance_counts = {"high": 0, "normal": 0, "low": 0}
        for event in events:
            importance_counts[str(event["importance"])] += 1
        preview_cursor = "<正式读取时由中央签发>"
        if view == "compact":
            context = self._compact_payload(
                background=background,
                events=events,
                understanding_version=understanding_version,
                understanding=understanding,
                # A human-facing source preview must remain self-contained.
                # The real reader can still submit its saved version and get
                # unchanged=true on the authenticated read endpoint.
                known_understanding_version=None,
                next_cursor=preview_cursor,
            )
        else:
            understanding_payload: dict[str, Any] = {
                "version": understanding_version,
                "unchanged": known_understanding_version == understanding_version,
            }
            if not understanding_payload["unchanged"]:
                understanding_payload["content"] = understanding
            context = {
                "understanding": understanding_payload,
                "background": background,
                "events": events,
                "importance_counts": importance_counts,
                "next_cursor": preview_cursor,
            }
        return {
            "preview_only": True,
            "side_effects": False,
            "cursor_basis": "latest_issued_cursor",
            "reader_id": reader_id,
            "context": context,
        }

    @staticmethod
    def _reader_payload(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
        if row["revoked_at"] is not None:
            status = "revoked"
        elif now >= _parse_utc(str(row["token_expires_at"])):
            status = "expired"
        else:
            status = "active"
        process_binding = _stored_process_binding(row["process_binding_json"])
        return {
            "reader_id": row["reader_id"],
            "type": row["reader_type"],
            "instance_id": row["instance_id"],
            "display_name": row["display_name"],
            "status": status,
            "created_at": row["created_at"],
            "paired_at": row["paired_at"],
            "token_expires_at": row["token_expires_at"],
            "revoked_at": row["revoked_at"],
            "cursor_epoch": int(row["cursor_epoch"]),
            "last_requested_at": row["last_requested_at"],
            "last_requested_position": {
                "created_at": row["last_requested_cursor_created_at"],
                "timeline_event_id": row["last_requested_timeline_event_id"],
            },
            "last_served_at": row["last_served_at"],
            "last_served_position": {
                "created_at": row["last_served_cursor_created_at"],
                "timeline_event_id": row["last_served_timeline_event_id"],
            },
            # Runtime inspection is intentionally isolated to process_status();
            # ordinary reader/settings reads must not spawn a Windows process query.
            "process_running": None,
            "process_display_name": (
                str(process_binding["display_name"]) if process_binding else None
            ),
        }

    def process_status(self, reader_id: str) -> dict[str, Any]:
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT reader_id, process_executable_path, process_pid,
                       process_started_at, process_binding_json
                FROM ai_readers WHERE reader_id = ?
                """,
                (reader_id,),
            ).fetchone()
        if row is None:
            raise AIReaderNotFound("AI reader was not found")
        process_running, process_display_name = _reader_process_status(row)
        return {
            "reader_id": reader_id,
            "process_running": process_running,
            "process_display_name": process_display_name,
        }

    def list_readers(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = _now(now)
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_readers ORDER BY paired_at DESC, reader_id DESC"
            ).fetchall()
        return [self._reader_payload(row, current) for row in rows]

    def get_reader(
        self, reader_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        current = _now(now)
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM ai_readers WHERE reader_id = ?", (reader_id,)
            ).fetchone()
        return self._reader_payload(row, current) if row is not None else None

    def list_access_logs(self, reader_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if self.get_reader(reader_id) is None:
            raise AIReaderNotFound("AI reader was not found")
        with self.store._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ai_reader_access_logs
                WHERE reader_id = ?
                ORDER BY requested_at DESC, access_log_id DESC
                LIMIT ?
                """,
                (reader_id, limit),
            ).fetchall()
        return [
            {
                "access_log_id": row["access_log_id"],
                "request_id": row["request_id"],
                "requested_at": row["requested_at"],
                "completed_at": row["completed_at"],
                "result": row["result"],
                "cursor_epoch": row["cursor_epoch"],
                "business_date": row["business_date"],
                "requested_position": {
                    "created_at": row["requested_cursor_created_at"],
                    "timeline_event_id": row["requested_timeline_event_id"],
                },
                "served_position": {
                    "created_at": row["served_cursor_created_at"],
                    "timeline_event_id": row["served_timeline_event_id"],
                },
                "served_event_ids": json.loads(row["served_event_ids_json"]),
                "served_report_ids": json.loads(row["served_report_ids_json"]),
                "importance_counts": json.loads(row["importance_counts_json"]),
                "background_generated_at": row["background_generated_at"],
                "understanding_version": row["understanding_version"],
                "response_hash": row["response_hash"],
                "response_bytes": row["response_bytes"],
                "duration_ms": int(row["duration_ms"]),
            }
            for row in rows
        ]

    def revoke_reader(
        self, reader_id: str, *, now: datetime | None = None
    ) -> bool:
        revoked_at = utc_timestamp(_now(now))
        with self.store._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_readers
                SET revoked_at = COALESCE(revoked_at, ?),
                    cursor_epoch = CASE
                        WHEN revoked_at IS NULL THEN cursor_epoch + 1
                        ELSE cursor_epoch
                    END
                WHERE reader_id = ?
                """,
                (revoked_at, reader_id),
            )
        return cursor.rowcount > 0

    def clear_reading_progress(self, reader_id: str) -> dict[str, Any]:
        """Clear visible served markers and force the reader to restart today."""
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE ai_readers
                    SET cursor_epoch = cursor_epoch + 1,
                        last_requested_cursor_created_at = NULL,
                        last_requested_timeline_event_id = NULL,
                        last_served_cursor_created_at = NULL,
                        last_served_timeline_event_id = NULL
                    WHERE reader_id = ? AND revoked_at IS NULL
                    """,
                    (reader_id,),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    raise AIReaderNotFound("AI reader was not found or is revoked")
                row = connection.execute(
                    "SELECT * FROM ai_readers WHERE reader_id = ?", (reader_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return self._reader_payload(row, _now(None))

    def primary_reader(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        current = _now(now)
        with self.store._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_readers
                WHERE revoked_at IS NULL AND token_expires_at > ?
                ORDER BY paired_at DESC, reader_id DESC
                LIMIT 1
                """,
                (utc_timestamp(current),),
            ).fetchone()
        return self._reader_payload(row, current) if row is not None else None

    def served_event_ids_for_primary(self) -> tuple[dict[str, Any] | None, set[str]]:
        reader = self.primary_reader()
        if reader is None:
            return None, set()
        with self.store._connection() as connection:
            rows = connection.execute(
                """
                SELECT served_event_ids_json FROM ai_reader_access_logs
                WHERE reader_id = ? AND cursor_epoch = ? AND result = 'served'
                """,
                (reader["reader_id"], reader["cursor_epoch"]),
            ).fetchall()
        event_ids: set[str] = set()
        for row in rows:
            event_ids.update(str(value) for value in json.loads(row[0]))
        return reader, event_ids

    @staticmethod
    def _insert_audit_event(
        connection: sqlite3.Connection,
        *,
        occurred_at: str,
        event_key: str,
        title: str,
        detail: str,
        subject: dict[str, Any],
        evidence: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO timeline_events(
                timeline_event_id, occurred_at, created_at, event_key, category,
                importance, title, detail, source_kind, source_device_id,
                wish_id, trigger_id, subject_json, evidence_json,
                statistics_window_json, delivery_json, dedupe_key
            ) VALUES (?, ?, ?, ?, 'system', 'low', ?, ?, 'central', NULL,
                      NULL, NULL, ?, ?, NULL, NULL, ?)
            """,
            (
                str(uuid.uuid4()), occurred_at, occurred_at, event_key,
                title[:120], detail[:500], canonical_json(subject),
                canonical_json(evidence), dedupe_key,
            ),
        )


def load_reader_status_read_only(
    database_path: Path, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Read tray status without provisioning credentials or mutating SQLite."""
    current = _now(now)
    uri = database_path.expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM ai_readers ORDER BY paired_at DESC, reader_id DESC"
        ).fetchall()
    finally:
        connection.close()
    return [AIReaderService._reader_payload(row, current) for row in rows]
