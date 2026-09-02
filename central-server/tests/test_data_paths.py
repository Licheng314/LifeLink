import tempfile
import unittest
from pathlib import Path

from central.config import default_config_path, default_data_dir


class CentralDataPathTests(unittest.TestCase):
    def test_default_is_unique_per_user_and_not_installation_local(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"USERPROFILE": directory}
            expected = Path(directory) / "LifeLink" / "central"
            self.assertEqual(default_data_dir(environment), expected)
            self.assertEqual(default_config_path(environment), expected / "config.json")

    def test_explicit_data_root_is_shared_by_components(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"LIFE_LINK_DATA_ROOT": directory}
            self.assertEqual(default_data_dir(environment), Path(directory) / "central")


if __name__ == "__main__":
    unittest.main()
