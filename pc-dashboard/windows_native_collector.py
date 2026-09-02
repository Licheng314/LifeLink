"""Windows foreground and input-state interval collector.

The collector keeps intervals in memory (or in the caller's checkpoint) and
produces stable-ID revisions only when an interval closes or the caller asks
for a snapshot. This keeps normal one-second polling out of the outbox while
retaining enough information to recover cleanly after a client restart.
"""

from __future__ import annotations

import ctypes
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol


UTC = timezone.utc
AFK_THRESHOLD_SECONDS = 180
DEFAULT_MAX_SAMPLE_GAP_SECONDS = 300
SOURCE = {"kind": "desktop", "collector": "windows_native", "reliability": "observed"}
_EVENT_NAMESPACE = uuid.UUID("8c15d592-87f5-55ac-81b6-1a70ebfbb4ce")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class ForegroundWindow:
    """The privacy-minimised current foreground-window observation."""

    hwnd: int
    process_name: str
    title: str = ""

    def __post_init__(self) -> None:
        # Never permit a full executable path to enter a checkpoint or event.
        object.__setattr__(self, "process_name", os.path.basename(self.process_name))


@dataclass(frozen=True)
class NativeSample:
    """One atomic sample supplied by the platform probe or a test double."""

    foreground: ForegroundWindow | None = None
    idle_seconds: float | None = None
    locked: bool = False
    available: bool = True


class NativeProbe(Protocol):
    def sample(self) -> NativeSample: ...


@dataclass
class _Interval:
    state: str
    started_at: datetime
    details: dict[str, Any]
    revision: int = 0


class WindowsNativeCollector:
    """Turn native samples into coarse foreground and input-state intervals.

    ``observe`` is the testable core. ``sample`` is a thin production adapter;
    callers may inject any :class:`NativeProbe` on platforms without Win32.
    """

    def __init__(
        self,
        probe: NativeProbe | None = None,
        *,
        afk_threshold_seconds: int = AFK_THRESHOLD_SECONDS,
        max_sample_gap_seconds: int = DEFAULT_MAX_SAMPLE_GAP_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if afk_threshold_seconds < 1 or max_sample_gap_seconds < 1:
            raise ValueError("sampling thresholds must be positive")
        self.probe = probe if probe is not None else WindowsNativeProbe()
        self.afk_threshold_seconds = int(afk_threshold_seconds)
        self.max_sample_gap_seconds = int(max_sample_gap_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._foreground: _Interval | None = None
        self._input: _Interval | None = None
        self._last_sample_at: datetime | None = None

    def sample(self, at: datetime | None = None) -> list[dict[str, Any]]:
        """Poll the native probe; failures close the prior known interval safely."""
        observed_at = _utc(at or self.clock())
        try:
            native = self.probe.sample()
        except Exception:
            native = NativeSample(available=False)
        return self.observe(native, observed_at)

    def observe(self, native: NativeSample, at: datetime) -> list[dict[str, Any]]:
        """Apply a native sample and return only intervals that have closed."""
        now = _utc(at)
        if self._last_sample_at is not None and now < self._last_sample_at:
            raise ValueError("sample time must not move backwards")
        events: list[dict[str, Any]] = []
        gap = self._last_sample_at is not None and (
            now - self._last_sample_at
        ).total_seconds() > self.max_sample_gap_seconds
        if gap:
            # A suspend/long scheduler pause is an unknown period, not usage.
            events.extend(self._close_all(self._last_sample_at))

        input_state, input_started_at = self._input_state(native, now, fresh=bool(gap))
        window = native.foreground if native.available and not native.locked else None
        self._transition_foreground(window, now, events)
        self._transition_input(input_state, input_started_at, now, events)
        self._last_sample_at = now
        return events

    def flush(self, at: datetime | None = None) -> list[dict[str, Any]]:
        """Close open intervals for orderly shutdown without generating ticks."""
        now = _utc(at or self.clock())
        if self._last_sample_at is not None and now < self._last_sample_at:
            raise ValueError("flush time must not move backwards")
        events = self._close_all(now)
        self._last_sample_at = now
        return events

    def snapshot(self, at: datetime | None = None) -> list[dict[str, Any]]:
        """Return revisions of open intervals without closing or resetting them.

        This is the bridge for callers that need an in-progress interval in the
        outbox. Repeated snapshots retain the interval event ID and increase
        its revision as its duration grows.
        """
        now = _utc(at or self.clock())
        events: list[dict[str, Any]] = []
        if self._foreground:
            events.append(self._event("app.foreground", self._foreground, now))
        if self._input:
            events.append(self._event("device.input_state", self._input, now))
        return events

    def checkpoint(self) -> dict[str, Any]:
        """Return JSON-safe state; persist it beside the outbox if desired."""
        return {
            "version": 1,
            "last_sample_at": _stamp(self._last_sample_at) if self._last_sample_at else None,
            "foreground": self._serialize_interval(self._foreground),
            "input": self._serialize_interval(self._input),
        }

    def restore(self, checkpoint: dict[str, Any]) -> None:
        """Restore state generated by :meth:`checkpoint`, rejecting malformed input."""
        if checkpoint.get("version") != 1:
            raise ValueError("unsupported native collector checkpoint")
        self._last_sample_at = self._optional_stamp(checkpoint.get("last_sample_at"))
        self._foreground = self._deserialize_interval(checkpoint.get("foreground"))
        self._input = self._deserialize_interval(checkpoint.get("input"))
        for interval in (self._foreground, self._input):
            if interval and self._last_sample_at and interval.started_at > self._last_sample_at:
                raise ValueError("checkpoint interval starts after last sample")

    @classmethod
    def from_checkpoint(cls, checkpoint: dict[str, Any], **kwargs: Any) -> "WindowsNativeCollector":
        collector = cls(**kwargs)
        collector.restore(checkpoint)
        return collector

    def _input_state(self, native: NativeSample, now: datetime, *, fresh: bool) -> tuple[str | None, datetime]:
        if not native.available:
            return None, now
        if native.locked:
            return "locked", now
        if native.idle_seconds is None or native.idle_seconds < 0:
            return None, now
        if native.idle_seconds < self.afk_threshold_seconds:
            return "active", now
        boundary = now - timedelta(seconds=native.idle_seconds - self.afk_threshold_seconds)
        # Never claim that a state observed after a long sleep existed during it.
        if fresh or (self._last_sample_at is not None and boundary < self._last_sample_at):
            boundary = now
        return "afk", boundary

    def _transition_foreground(self, window: ForegroundWindow | None, now: datetime, events: list[dict[str, Any]]) -> None:
        details = self._window_details(window) if window else None
        if self._foreground and self._foreground.details == details:
            return
        if self._foreground:
            events.append(self._event("app.foreground", self._foreground, now))
            self._foreground = None
        if details:
            self._foreground = _Interval("foreground", now, details)

    def _transition_input(self, state: str | None, started_at: datetime, now: datetime, events: list[dict[str, Any]]) -> None:
        if self._input and self._input.state == state:
            return
        if self._input:
            events.append(self._event("device.input_state", self._input, started_at if state == "afk" else now))
            self._input = None
        if state is not None:
            self._input = _Interval(
                state,
                started_at,
                {"status": state, "idle_threshold_seconds": self.afk_threshold_seconds},
            )

    def _close_all(self, at: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self._foreground:
            events.append(self._event("app.foreground", self._foreground, at))
            self._foreground = None
        if self._input:
            events.append(self._event("device.input_state", self._input, at))
            self._input = None
        return events

    @staticmethod
    def _window_details(window: ForegroundWindow) -> dict[str, Any]:
        # Window handles and titles are intentionally ephemeral probe details.
        # Process identity is sufficient for usage intervals and survives neither
        # sensitive document names nor unstable HWND values into the checkpoint.
        return {"process_name": window.process_name}

    def _event(self, event_type: str, interval: _Interval, ended_at: datetime) -> dict[str, Any]:
        if ended_at < interval.started_at:
            raise ValueError("interval cannot end before it starts")
        # UUIDv5 makes replay after a crash/checkpoint deterministic.
        identity = f"{event_type}|{interval.state}|{_stamp(interval.started_at)}|{interval.details}"
        interval.revision += 1
        return {
            "event_id": str(uuid.uuid5(_EVENT_NAMESPACE, identity)),
            "occurred_at": _stamp(interval.started_at),
            "event_type": event_type,
            "source": dict(SOURCE),
            "duration_seconds": int((ended_at - interval.started_at).total_seconds()),
            "revision": interval.revision,
            "payload": self._payload(event_type, interval.details),
        }

    @staticmethod
    def _payload(event_type: str, details: dict[str, Any]) -> dict[str, Any]:
        if event_type == "app.foreground":
            process_name = str(details["process_name"])
            return {
                "app": {
                    "display_name": process_name,
                    "package_name": process_name,
                    "process_name": process_name,
                }
            }
        return dict(details)

    @staticmethod
    def _serialize_interval(interval: _Interval | None) -> dict[str, Any] | None:
        if interval is None:
            return None
        return {
            "state": interval.state,
            "started_at": _stamp(interval.started_at),
            "details": dict(interval.details),
            "revision": interval.revision,
        }

    @staticmethod
    def _optional_stamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("checkpoint timestamp must be a string")
        return _parse_stamp(value)

    @classmethod
    def _deserialize_interval(cls, value: Any) -> _Interval | None:
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("state"), str) or not isinstance(value.get("details"), dict):
            raise ValueError("invalid checkpoint interval")
        revision = value.get("revision", 0)
        if not isinstance(revision, int) or revision < 0:
            raise ValueError("invalid checkpoint interval revision")
        return _Interval(
            value["state"],
            cls._optional_stamp(value.get("started_at")) or (_ for _ in ()).throw(ValueError("missing interval timestamp")),
            dict(value["details"]),
            revision,
        )


class WindowsNativeProbe:
    """Small ctypes Win32 adapter; instantiated safely on non-Windows hosts."""

    def sample(self) -> NativeSample:
        if os.name != "nt":
            return NativeSample(available=False)
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow())
        if not hwnd:
            return NativeSample(locked=True)
        idle_seconds = self._idle_seconds(user32)
        if idle_seconds is None:
            return NativeSample(available=False)
        process_name = self._process_name(user32, hwnd)
        if not process_name:
            return NativeSample(available=False)
        if process_name.casefold() in {"lockapp.exe", "logonui.exe"}:
            return NativeSample(idle_seconds=idle_seconds, locked=True)
        title = self._window_title(user32, hwnd)
        return NativeSample(ForegroundWindow(hwnd, process_name, title), idle_seconds)

    @staticmethod
    def _idle_seconds(user32: Any) -> float | None:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # dwTime is a 32-bit tick count; modulo arithmetic covers rollover.
        return ((int(ctypes.windll.kernel32.GetTickCount64()) & 0xFFFFFFFF) - info.dwTime & 0xFFFFFFFF) / 1000.0

    @staticmethod
    def _window_title(user32: Any, hwnd: int) -> str:
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    @staticmethod
    def _process_name(user32: Any, hwnd: int) -> str:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return os.path.basename(buffer.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
