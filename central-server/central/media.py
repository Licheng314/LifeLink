"""Independent Bilibili audio extraction, run inside the central service.

This module is deliberately self-contained:
- It does not touch the event/device storage or the v1 delivery contract.
- It invokes the standalone development/tools/bilibili-audio command-line utility.
- Its executable dependencies live in the unique LifeLink user-data root, not
  beside the development source.
- Output files live under the central data directory.

Jobs are kept in memory only. The MP3 files on disk are the durable source of
truth; a central service restart clears the job history but keeps the music.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import default_data_dir
from .domain import utc_timestamp

# Only these hosts may be submitted, so the endpoint cannot be abused as an
# arbitrary server-side download proxy.
_ALLOWED_HOST_PATTERNS = (
    re.compile(r"^([a-z0-9-]+\.)?bilibili\.com$", re.IGNORECASE),
    re.compile(r"^([a-z0-9-]+\.)?b23\.tv$", re.IGNORECASE),
)
_VIDEO_PATH_RE = re.compile(r"/video/[A-Za-z0-9]+", re.IGNORECASE)
_MAX_HISTORY = 50
_JOB_TIMEOUT_SECONDS = 60 * 20
_WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _default_tool_script() -> Path | None:
    # central/central/media.py -> repo root is parents[2]
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root / "development" / "tools" / "bilibili-audio" / "bilibili_audio.py"
    return candidate if candidate.exists() else None


def _is_allowed_url(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not any(pattern.match(host) for pattern in _ALLOWED_HOST_PATTERNS):
        return False
    # b23.tv short links do not contain /video/ in the path; full bilibili URLs
    # must point at a video page.
    if host.endswith("bilibili.com") and not _VIDEO_PATH_RE.search(parsed.path or ""):
        return False
    return True


@dataclass(frozen=True)
class MediaSettings:
    audio_dir: Path
    incoming_dir: Path
    tool_script: Path | None
    tool_bin_dir: Path
    python_executable: str = field(default_factory=lambda: sys.executable)

    @classmethod
    def from_config(cls, config: Any) -> "MediaSettings":
        data_dir = default_data_dir()
        audio_dir = getattr(config, "media_audio_dir", None) or data_dir / "media" / "audio"
        incoming_dir = getattr(config, "media_incoming_dir", None) or data_dir / "media" / "incoming"
        tool_script = getattr(config, "media_tool_script", None) or _default_tool_script()
        tool_bin_dir = data_dir.parent / "tools" / "bilibili-audio" / "bin"
        return cls(
            audio_dir=Path(audio_dir),
            incoming_dir=Path(incoming_dir),
            tool_script=Path(tool_script) if tool_script else None,  # type: ignore[arg-type]
            tool_bin_dir=tool_bin_dir,
        )

    def ready(self) -> tuple[bool, str | None]:
        if self.tool_script is None or not self.tool_script.exists():
            return False, "未找到 bilibili_audio.py，请检查小工具目录或配置 media_tool_script。"
        return True, None


class MediaManager:
    """Single-worker background runner for Bilibili audio extraction."""

    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=1)
        self._current_job_id: str | None = None
        self._worker = threading.Thread(
            target=self._run_worker,
            daemon=True,
            name="life-radio-media-worker",
        )
        self._worker.start()

    # ----- public API -----------------------------------------------------
    def list_items(self) -> list[dict[str, Any]]:
        audio_dir = self.settings.audio_dir
        if not audio_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in audio_dir.glob("*.mp3"):
            if not path.is_file():
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": utc_timestamp(
                        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    ),
                }
            )
        items.sort(key=lambda item: item["modified_at"], reverse=True)
        return items

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [dict(job) for job in self._jobs.values()]
        jobs.sort(key=lambda job: job["created_at"], reverse=True)
        return jobs

    def has_active_job(self) -> bool:
        with self._lock:
            return any(job["status"] in {"queued", "processing"} for job in self._jobs.values())

    def submit(self, url: str) -> tuple[dict[str, Any] | None, str | None, int | None]:
        url = (url or "").strip()
        if not url:
            return None, "请提供 B 站视频链接。", 400
        if not _is_allowed_url(url):
            return None, "只支持 bilibili.com/video/ 或 b23.tv 视频链接。", 400
        ready, reason = self.settings.ready()
        if not ready:
            return None, reason, 503
        with self._lock:
            if any(job["status"] in {"queued", "processing"} for job in self._jobs.values()):
                return None, "已有任务正在处理，请完成后再提交。", 409
            job_id = uuid.uuid4().hex
            now = utc_timestamp()
            job: dict[str, Any] = {
                "id": job_id,
                "url": url,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "message": "排队中",
                "file": None,
            }
            self._jobs[job_id] = job
            self._trim_history_locked()
        try:
            self._queue.put_nowait(job_id)
        except queue.Full:
            with self._lock:
                self._jobs[job_id]["status"] = "failed"
                self._jobs[job_id]["updated_at"] = utc_timestamp()
                self._jobs[job_id]["message"] = "任务队列繁忙，请稍后再试。"
            return None, "任务队列繁忙，请稍后再试。", 409
        return self._public_job(job_id), None, None

    def open_folder(self) -> tuple[bool, str | None]:
        audio_dir = self.settings.audio_dir
        try:
            audio_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return False, f"无法创建目录：{error}"
        try:
            if os.name == "nt":
                os.startfile(str(audio_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(audio_dir)])
            else:
                subprocess.Popen(["xdg-open", str(audio_dir)])
        except OSError as error:
            return False, f"无法打开文件夹：{error}"
        return True, None

    # ----- worker ---------------------------------------------------------
    def _public_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def _set_status(self, job_id: str, status: str, message: str, **extra: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["status"] = status
            job["message"] = message
            job["updated_at"] = utc_timestamp()
            for key, value in extra.items():
                job[key] = value
            if status == "processing":
                self._current_job_id = job_id

    def _run_worker(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process_job(job_id)
            except Exception as error:  # pragma: no cover - defensive guard
                self._set_status(job_id, "failed", f"内部错误：{error}")
            finally:
                with self._lock:
                    if self._current_job_id == job_id:
                        self._current_job_id = None
                self._queue.task_done()

    def _process_job(self, job_id: str) -> None:
        job = self._public_job(job_id)
        if job is None:
            return
        url = job["url"]
        self._set_status(job_id, "processing", "正在下载并转换为 MP3…")

        self.settings.audio_dir.mkdir(parents=True, exist_ok=True)
        self.settings.incoming_dir.mkdir(parents=True, exist_ok=True)

        command = [
            self.settings.python_executable,
            str(self.settings.tool_script),
            url,
            "--output-dir",
            str(self.settings.audio_dir),
            "--temp-dir",
            str(self.settings.incoming_dir),
        ]
        creationflags = _WINDOWS_NO_WINDOW if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["LIFE_LINK_BILIBILI_TOOL_BIN"] = str(self.settings.tool_bin_dir)
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_JOB_TIMEOUT_SECONDS,
                creationflags=creationflags,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._set_status(job_id, "failed", "任务超时（超过 20 分钟）。")
            return
        except OSError as error:
            self._set_status(job_id, "failed", f"无法启动下载工具：{error}")
            return

        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            tail = self._tail(output) or f"退出码 {completed.returncode}"
            self._set_status(job_id, "failed", tail)
            return

        output_file = self._parse_output_file(output)
        if output_file is not None and output_file.exists():
            stat = output_file.stat()
            self._set_status(
                job_id,
                "completed",
                "完成",
                file={
                    "name": output_file.name,
                    "size": stat.st_size,
                },
            )
            return

        # The script reported success but did not print a parseable path.
        newest = self._newest_mp3()
        if newest is not None:
            stat = newest.stat()
            self._set_status(
                job_id,
                "completed",
                "完成",
                file={"name": newest.name, "size": stat.st_size},
            )
            return
        self._set_status(job_id, "failed", "下载完成，但未找到生成的 MP3 文件。")

    # ----- helpers --------------------------------------------------------
    def _trim_history_locked(self) -> None:
        if len(self._jobs) <= _MAX_HISTORY:
            return
        ordered = sorted(self._jobs.values(), key=lambda job: job["created_at"])
        for stale in ordered[: len(self._jobs) - _MAX_HISTORY]:
            self._jobs.pop(stale["id"], None)

    @staticmethod
    def _parse_output_file(output: str) -> Path | None:
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line.startswith("OUTPUT_FILE:"):
                candidate = Path(line.split(":", 1)[1].strip())
                return candidate if candidate.suffix.lower() == ".mp3" else candidate
            for prefix in ("完成：", "同名 MP3 已存在，跳过下载："):
                if line.startswith(prefix):
                    return Path(line[len(prefix):].strip())
        return None

    def _newest_mp3(self) -> Path | None:
        candidates = [path for path in self.settings.audio_dir.glob("*.mp3") if path.is_file()]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    @staticmethod
    def _tail(output: str, limit: int = 600) -> str:
        if not output:
            return ""
        if len(output) <= limit:
            return output
        return "…" + output[-limit:]

