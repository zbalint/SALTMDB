"""Static-source contracts for viewer.js.

No browser, node or playwright is available (RAM-limited dev box), so these pin the wiring the
audit found missing. They cannot prove focus, keyboard or layout behavior at runtime.
"""

import unittest
from pathlib import Path


def _read_static(name):
    root = Path(__file__).resolve().parents[1] / "src/saltmdb/viewer/static"
    return (root / name).read_text(encoding="utf-8")


class TestViewerFrontendContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _read_static("viewer.js")

    def _function_body(self, name, following):
        start = self.script.index(f"const {name} = ")
        return self.script[start : self.script.index(f"const {following} = ", start)]

    def test_system_health_renders_the_data_the_endpoint_provides(self):
        body = self._function_body("operations", "tags")
        for expected in (
            "data.warnings",
            "latest_snapshot",
            "vector.available",
            "wal_bytes",
            "shm_bytes",
            "page_count",
            "freelist_count",
            "daemon.version",
            "uptime_s",
            "No warnings",
        ):
            with self.subTest(expected):
                self.assertIn(expected, body)


if __name__ == "__main__":
    unittest.main()
