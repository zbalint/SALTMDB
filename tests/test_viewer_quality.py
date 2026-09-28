import os
import shutil
import tempfile
import unittest
from pathlib import Path

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerQuality(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "quality.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _quality(self, query):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        handler.get_quality(query)
        return captured["status"], captured["data"]

    def _insert(self, entity_id, updated_at, embedding_status="pending", quality_status=None):
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title,
                                     full_content, embedding_status, quality_status)
               VALUES (?, ?, ?, ?, ?, 'body', ?, ?)""",
            (
                entity_id,
                updated_at,
                updated_at,
                updated_at,
                entity_id,
                embedding_status,
                quality_status,
            ),
        )
        self.conn.commit()

    def test_quality_reports_exact_totals_and_pages_items(self):
        for index in range(3):
            self._insert(f"m{index}", f"2026-09-2{index}T00:00:00+00:00")

        status, first = self._quality({"limit": ["2"]})
        _, second = self._quality({"limit": ["2"], "page": ["2"]})

        self.assertEqual(status, 200)
        self.assertEqual(first["items_total"], 3)
        self.assertEqual(first["total_pages"], 2)
        self.assertEqual([i["id"] for i in first["items"]], ["m2", "m1"])
        self.assertEqual([i["id"] for i in second["items"]], ["m0"])
        self.assertEqual(first["orphan_raw_total"], 3)
        self.assertEqual(len(first["orphan_raw"]), 2)

    def test_quality_filters_narrow_items_and_total_together(self):
        self._insert("p1", "2026-09-20T00:00:00+00:00", embedding_status="pending")
        self._insert("f1", "2026-09-21T00:00:00+00:00", embedding_status="failed")
        self._insert(
            "q1", "2026-09-22T00:00:00+00:00", embedding_status="ready", quality_status="flagged"
        )

        _, failed = self._quality({"embedding_status": ["failed"]})
        _, flagged = self._quality({"quality_status": ["flagged"]})

        self.assertEqual([i["id"] for i in failed["items"]], ["f1"])
        self.assertEqual(failed["items_total"], 1)
        self.assertEqual([i["id"] for i in flagged["items"]], ["q1"])
        self.assertEqual(flagged["items_total"], 1)


class TestViewerQualityFrontendContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "src/saltmdb/viewer/static"
        cls.script = (root / "viewer.js").read_text(encoding="utf-8")

    def test_quality_view_uses_real_totals_filters_and_paging(self):
        self.assertIn("metric('Quality signals', data.items_total", self.script)
        self.assertIn("metric('Orphaned raw memories', data.orphan_raw_total", self.script)
        self.assertNotIn("data.items.length, 'warning'", self.script)
        self.assertIn("select('Embedding status'", self.script)
        self.assertIn("select('Quality status'", self.script)
        self.assertIn("/api/quality?${", self.script)
        self.assertIn("setAttribute('aria-label', 'Quality pages')", self.script)
        self.assertIn("Showing ${data.orphan_raw.length} of ${data.orphan_raw_total}", self.script)


if __name__ == "__main__":
    unittest.main()
