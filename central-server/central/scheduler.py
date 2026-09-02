"""Small stoppable, injectable-clock minute scheduler for central projections."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from .evaluator import evaluate_scheduled_system

logger = logging.getLogger(__name__)


class MinuteScheduler:
    def __init__(self, store, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        with self.store._connection() as connection:
            evaluate_scheduled_system(connection, self.store, now=self.clock())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear(); self._thread = threading.Thread(target=self._loop, name="life-radio-minute-scheduler", daemon=True); self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try: self.run_once()
            except Exception: logger.exception("minute scheduler projection failed")
            self._stop.wait(60)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join()
        if thread is None or not thread.is_alive():
            self._thread = None
