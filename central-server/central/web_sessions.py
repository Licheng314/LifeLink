"""Ephemeral browser sessions for the public Life Link WebUI.

The browser never receives a device credential.  A paired client asks for a
short-lived bootstrap code and the browser exchanges that code once for an
HttpOnly cookie.  State intentionally stays in memory: restarting central
revokes every browser session without a database migration or a second secret
store.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass


BOOTSTRAP_TTL_SECONDS = 90
SESSION_TTL_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class BrowserSession:
    device_id: str
    csrf_token: str
    expires_at: float


class WebSessionManager:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._bootstraps: dict[str, tuple[str, float]] = {}
        self._sessions: dict[str, BrowserSession] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        self._bootstraps = {
            digest: value for digest, value in self._bootstraps.items()
            if value[1] > now
        }
        self._sessions = {
            digest: session for digest, session in self._sessions.items()
            if session.expires_at > now
        }

    def create_bootstrap(self, device_id: str) -> str:
        if not device_id:
            raise ValueError("device_id is required")
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._bootstraps[self._digest(token)] = (device_id, now + BOOTSTRAP_TTL_SECONDS)
        return token

    def claim_bootstrap(self, token: str) -> tuple[str, BrowserSession] | None:
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._prune(now)
            claimed = self._bootstraps.pop(self._digest(token), None)
            if claimed is None:
                return None
            device_id, _ = claimed
            session_token = secrets.token_urlsafe(32)
            session = BrowserSession(
                device_id=device_id,
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now + SESSION_TTL_SECONDS,
            )
            self._sessions[self._digest(session_token)] = session
            return session_token, session

    def get_session(self, token: str | None) -> BrowserSession | None:
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._prune(now)
            return self._sessions.get(self._digest(token))
