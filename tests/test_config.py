import os
import unittest
from saltmdb.config import get_viewer_host, get_viewer_port, is_viewer_enabled


class TestConfigViewer(unittest.TestCase):
    def setUp(self):
        self.env_keys = ["SALTMDB_VIEWER_PORT", "SALTMDB_VIEWER_HOST", "SALTMDB_VIEWER_ENABLED"]
        self.orig_env = {}
        for key in self.env_keys:
            if key in os.environ:
                self.orig_env[key] = os.environ[key]
                del os.environ[key]

    def tearDown(self):
        for key in self.env_keys:
            if key in self.orig_env:
                os.environ[key] = self.orig_env[key]
            elif key in os.environ:
                del os.environ[key]

    def test_get_viewer_port_default(self):
        self.assertEqual(get_viewer_port(), 8080)

    def test_get_viewer_port_override(self):
        os.environ["SALTMDB_VIEWER_PORT"] = "9090"
        self.assertEqual(get_viewer_port(), 9090)

    def test_get_viewer_host_default(self):
        self.assertEqual(get_viewer_host(), "127.0.0.1")

    def test_get_viewer_host_override(self):
        os.environ["SALTMDB_VIEWER_HOST"] = "0.0.0.0"
        self.assertEqual(get_viewer_host(), "0.0.0.0")

    def test_is_viewer_enabled_default(self):
        self.assertTrue(is_viewer_enabled())

    def test_is_viewer_enabled_truthy_overrides(self):
        for truthy_val in ["true", "TRUE", "1", "yes", "on", " True "]:
            os.environ["SALTMDB_VIEWER_ENABLED"] = truthy_val
            self.assertTrue(
                is_viewer_enabled(),
                f"Expected is_viewer_enabled() to be True for value {truthy_val!r}",
            )

    def test_is_viewer_enabled_falsy_overrides(self):
        for falsy_val in ["0", "false", "no", "off", "FALSE", " No ", "OFF", " 0 "]:
            os.environ["SALTMDB_VIEWER_ENABLED"] = falsy_val
            self.assertFalse(
                is_viewer_enabled(),
                f"Expected is_viewer_enabled() to be False for value {falsy_val!r}",
            )


if __name__ == "__main__":
    unittest.main()
