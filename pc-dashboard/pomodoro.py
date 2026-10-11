"""Local Pomodoro state and durable completion handoff; no schedule dependency."""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

FOCUS_SECONDS = 25 * 60
REST_SECONDS = 5 * 60
MAX_GOAL_LENGTH = 120
MAX_TICK_GAP = 6.0


class PomodoroTimer:
    def __init__(self, focus_seconds: int = FOCUS_SECONDS, rest_seconds: int = REST_SECONDS):
        self.focus_seconds = focus_seconds
        self.rest_seconds = rest_seconds
        self.phase = "idle"
        self.remaining = 0.0
        self.goal = ""
        self.paused = False
        self.pause_reason = ""
        self.session_id = ""
        self.started_at = ""
        self.pending_events: list[dict] = []

    @property
    def active(self) -> bool:
        return self.phase in {"focus", "rest"}

    @property
    def display_seconds(self) -> int:
        return math.ceil(self.remaining)

    @property
    def progress(self) -> float:
        duration = self.focus_seconds if self.phase == "focus" else self.rest_seconds
        return min(1.0, max(0.0, self.remaining / duration)) if self.active else 0.0

    def start(self, goal: str, now: datetime | None = None) -> None:
        if self.active:
            raise ValueError("A Pomodoro is already running")
        self.goal = " ".join(goal.split())[:MAX_GOAL_LENGTH]
        self.session_id = str(uuid.uuid4())
        self.started_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        self.phase, self.remaining = "focus", float(self.focus_seconds)
        self.paused, self.pause_reason = False, ""

    def pause(self, reason: str = "manual") -> bool:
        if not self.active or self.paused:
            return False
        self.paused, self.pause_reason = True, reason
        return True

    def resume(self) -> None:
        self.paused, self.pause_reason = False, ""

    def stop(self) -> None:
        self.phase, self.remaining = "idle", 0.0
        self.paused, self.pause_reason = False, ""

    def advance(self, elapsed: float, *, blocked: bool = False, now: datetime | None = None) -> list[str]:
        if not self.active:
            return []
        if blocked or elapsed > MAX_TICK_GAP:
            return ["paused"] if self.pause("locked" if blocked else "sleep") else []
        if self.paused:
            return []
        elapsed = max(0.0, elapsed)
        transitions: list[str] = []
        while self.active and elapsed >= self.remaining:
            elapsed -= self.remaining
            if self.phase == "focus":
                completed = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
                self.pending_events.append({
                    "event_id": self.session_id,
                    "occurred_at": completed,
                    "event_key": "pomodoro.completed",
                    "title": "番茄钟完成",
                    "detail": f"工作计时 {self.focus_seconds // 60} 分钟" + (f"；目标：{self.goal}" if self.goal else ""),
                    "metadata": {"goal": self.goal, "focus_seconds": self.focus_seconds,
                                 "started_at": self.started_at, "completed_at": completed},
                })
                self.phase, self.remaining = "rest", float(self.rest_seconds)
                transitions.append("rest")
            else:
                self.phase, self.remaining = "complete", 0.0
                transitions.append("complete")
        if self.active:
            self.remaining -= elapsed
        return transitions

    def snapshot(self) -> dict:
        return {"phase": self.phase, "remaining": self.remaining, "goal": self.goal,
                "paused": self.paused, "pause_reason": self.pause_reason,
                "session_id": self.session_id, "started_at": self.started_at,
                "pending_events": self.pending_events}

    @classmethod
    def restore(cls, data: dict) -> "PomodoroTimer":
        timer = cls()
        timer.goal = " ".join(str(data.get("goal", "")).split())[:MAX_GOAL_LENGTH]
        pending = data.get("pending_events", [])
        if isinstance(pending, list):
            timer.pending_events = [item for item in pending if isinstance(item, dict)
                                    and item.get("event_key") == "pomodoro.completed"]
        phase = data.get("phase")
        # A completed round is transient UI, not a session to resume on startup.
        # Keep its goal and pending delivery, but reopen ready for a fresh round.
        if phase in {"focus", "rest"}:
            try:
                remaining = float(data["remaining"])
                session_id = str(uuid.UUID(str(data["session_id"])))
                started_at = str(data["started_at"])
                duration = timer.focus_seconds if phase == "focus" else timer.rest_seconds
                if math.isfinite(remaining) and 0 < remaining <= duration:
                    timer.phase, timer.remaining = phase, remaining
                    timer.session_id, timer.started_at = session_id, started_at
                    timer.paused, timer.pause_reason = True, "restart"
            except (KeyError, ValueError, TypeError):
                pass
        return timer


def load_timer(path: Path) -> PomodoroTimer:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PomodoroTimer.restore(data) if isinstance(data, dict) else PomodoroTimer()
    except (OSError, ValueError):
        return PomodoroTimer()


def save_timer(path: Path, timer: PomodoroTimer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(timer.snapshot(), ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
