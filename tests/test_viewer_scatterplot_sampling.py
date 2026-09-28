import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerScatterplotSampling(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "scatter.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        rng = np.random.default_rng(7)
        for index in range(4):
            stamp = f"2026-09-2{index}T00:00:00+00:00"
            self.conn.execute(
                "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
                "title, full_content, status, embedding_status) "
                "VALUES (?, ?, ?, ?, 'tester', ?, 'body', 'raw', 'ready')",
                (f"e{index}", stamp, stamp, stamp, f"Entity {index}"),
            )
            self.conn.execute(
                "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                (f"e{index}", rng.standard_normal(384).astype(np.float32).tobytes()),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _scatterplot(self):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        handler.get_scatterplot()
        return captured["data"]

    def test_sample_is_the_newest_embeddings_and_reports_truncation(self):
        with patch("saltmdb.viewer.routes.scatterplot.SCATTERPLOT_MAX_POINTS", 2):
            first = self._scatterplot()
            second = self._scatterplot()

        ids = lambda data: sorted(p["id"] for p in data["points"])  # noqa: E731
        self.assertEqual(ids(first), ["e2", "e3"])
        self.assertEqual(ids(first), ids(second))
        self.assertEqual(first["total_ready"], 4)
        self.assertTrue(first["truncated"])

    def test_untruncated_sample_says_so(self):
        data = self._scatterplot()

        self.assertEqual(len(data["points"]), 4)
        self.assertEqual(data["total_ready"], 4)
        self.assertFalse(data["truncated"])


if __name__ == "__main__":
    unittest.main()
