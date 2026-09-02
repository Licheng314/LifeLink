import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from diagnose_client import diagnose
from outbox import Outbox


class DiagnoseClientTests(unittest.TestCase):
    def test_reports_identity_consistency_and_outbox_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.json"
            config = root / "config.json"
            outbox_path = root / "outbox.sqlite3"
            identity.write_text(
                json.dumps({"device_id": "desktop-test"}), encoding="utf-8",
            )
            config.write_text(
                json.dumps({"device": {"device_id": "desktop-test"}}),
                encoding="utf-8",
            )
            outbox = Outbox(outbox_path)
            outbox.close()

            report = diagnose(identity, config, outbox_path)

            self.assertTrue(report["identity_matches_profile"])
            self.assertEqual(report["outbox_integrity"], "ok")
            self.assertEqual(report["outbox_states"], {})


if __name__ == "__main__":
    unittest.main()
