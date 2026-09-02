from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_BATCH_FILES = (
    PROJECT_ROOT / "central-server" / "start_server.bat",
    PROJECT_ROOT / "central-server" / "maintenance" / "create_invitation.bat",
    PROJECT_ROOT / "central-server" / "maintenance" / "configure_public_endpoint.bat",
    PROJECT_ROOT / "central-server" / "maintenance" / "configure_tailscale_endpoint.bat",
    PROJECT_ROOT / "pc-dashboard" / "start_central_client.bat",
    PROJECT_ROOT / "pc-dashboard" / "maintenance" / "diagnose_client.bat",
)


class WindowsBatchEntrypointTests(unittest.TestCase):
    def test_active_batch_files_are_ascii_with_crlf(self) -> None:
        for path in ACTIVE_BATCH_FILES:
            with self.subTest(path=path.relative_to(PROJECT_ROOT)):
                raw = path.read_bytes()
                raw.decode("ascii")
                self.assertTrue(raw.endswith(b"\r\n"))
                without_crlf = raw.replace(b"\r\n", b"")
                self.assertNotIn(b"\n", without_crlf)
                self.assertNotIn(b"\r", without_crlf)
                self.assertNotIn(b"chcp ", raw.lower())

    def test_main_launchers_use_a_quoted_absolute_powershell_bridge(self) -> None:
        expected = {
            PROJECT_ROOT / "central-server" / "start_server.bat": "central",
            PROJECT_ROOT / "pc-dashboard" / "start_central_client.bat": "pc",
        }
        for path, role in expected.items():
            with self.subTest(path=path.relative_to(PROJECT_ROOT)):
                text = path.read_text(encoding="ascii")
                self.assertIn(
                    r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe",
                    text,
                )
                self.assertIn(f'-File "%BOOTSTRAP%" -Role {role}', text)
                self.assertIn('if not exist "%BOOTSTRAP%"', text)
                self.assertIn("pause >nul", text)


if __name__ == "__main__":
    unittest.main()

