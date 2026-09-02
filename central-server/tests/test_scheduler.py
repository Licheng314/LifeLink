import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from central.scheduler import MinuteScheduler
from central.storage import CentralStore


class SchedulerTests(unittest.TestCase):
    def test_stop_clears_worker_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CentralStore(Path(directory) / "central.sqlite3", {})
            scheduler = MinuteScheduler(store, clock=lambda: datetime(2026, 8, 16, tzinfo=timezone.utc))
            scheduler.start()
            scheduler.stop()
            self.assertIsNone(scheduler._thread)


if __name__ == "__main__":
    unittest.main()
