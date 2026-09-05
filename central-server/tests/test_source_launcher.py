from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "launcher" / "source_launcher.py"
SPEC = importlib.util.spec_from_file_location("central_source_launcher_test", MODULE_PATH)
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class SourceLauncherTests(unittest.TestCase):
    def test_supported_versions_are_explicit_and_ordered(self) -> None:
        self.assertEqual(launcher.SUPPORTED_PYTHON_VERSIONS, ((3, 14), (3, 13)))

    def test_configured_python_exe_uses_adjacent_pythonw(self) -> None:
        candidate = Path(r"C:\\Python314\\python.exe")
        self.assertEqual(launcher.windowless_candidate(candidate), Path(r"C:\\Python314\\pythonw.exe"))

    def test_prefers_python_314_and_rejects_unsupported_candidates(self) -> None:
        root = Path(r"C:\\Users\\tester\\AppData\\Local")
        expected = root / "Programs" / "Python" / "Python314" / "pythonw.exe"

        def supported(candidate: Path) -> bool:
            return candidate == expected

        with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=True), patch.object(
            launcher.shutil, "which", return_value=None
        ), patch.object(launcher, "is_supported_python", side_effect=supported):
            self.assertEqual(launcher.source_python(), expected)

    def test_rejects_python_outside_supported_versions(self) -> None:
        candidate = Path(r"C:\\Python312\\pythonw.exe")
        with patch.object(Path, "is_file", return_value=True), patch.object(
            launcher.subprocess, "run"
        ) as run:
            run.return_value.returncode = 1
            self.assertFalse(launcher.is_supported_python(candidate))
