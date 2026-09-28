import hashlib
import re
import unittest
from pathlib import Path

from saltmdb.viewer.routes import STATIC_ASSETS
from saltmdb.viewer.templates import get_frontend_html

VENDOR = Path(__file__).resolve().parents[1] / "src/saltmdb/viewer/static/vendor"
# First DOMPurify release containing the fixes for GHSA-cmwh-pvxp-8882, -c2j3-45gr-mqc4 and
# -55q2-fjhq-7xh7 (audit finding L-1).
MIN_DOMPURIFY = (3, 4, 13)


class TestViewerVendorAssets(unittest.TestCase):
    def _recorded_hashes(self):
        text = (VENDOR / "THIRD_PARTY.md").read_text(encoding="utf-8")
        return re.findall(r"- `([^`]+)` .*?SHA-256 `([0-9a-f]{64})`", text)

    def test_every_recorded_hash_matches_the_vendored_file(self):
        recorded = self._recorded_hashes()
        self.assertTrue(recorded)
        for name, expected in recorded:
            with self.subTest(name):
                actual = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)

    def test_every_vendored_file_is_recorded_and_served(self):
        recorded = {name for name, _ in self._recorded_hashes()}
        on_disk = {p.name for p in VENDOR.glob("*.js")}
        self.assertEqual(on_disk, recorded)
        for name in on_disk:
            self.assertIn(f"/static/vendor/{name}", STATIC_ASSETS)

    def test_dompurify_is_past_the_published_advisories_and_loaded_by_the_shell(self):
        (name,) = [p.name for p in VENDOR.glob("dompurify-*.min.js")]
        version = tuple(
            int(part) for part in re.search(r"dompurify-([\d.]+)\.min\.js", name)[1].split(".")
        )
        self.assertGreaterEqual(version, MIN_DOMPURIFY)
        self.assertIn(f"/static/vendor/{name}", get_frontend_html())
        header = (VENDOR / name).read_text(encoding="utf-8")[:200]
        self.assertIn("DOMPurify " + ".".join(map(str, version)), header)


if __name__ == "__main__":
    unittest.main()
