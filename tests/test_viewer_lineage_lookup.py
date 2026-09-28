import io
import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerLineageLookup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "lineage.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        for entity_id, title in (("alpha-1", "Alpha one"), ("alpha-2", "Alpha two")):
            self.conn.execute(
                """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title,
                                         full_content)
                   VALUES (?, '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00',
                           '2026-09-01T00:00:00+00:00', ?, 'body')""",
                (entity_id, title),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _lineage(self, reference):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        handler.get_lineage(reference)
        return captured["status"], captured["data"]

    def test_exact_id_resolves_even_when_it_is_also_a_prefix_of_others(self):
        status, data = self._lineage("alpha-1")

        self.assertEqual(status, 200)
        self.assertEqual(data["root_id"], "alpha-1")

    def test_ambiguous_prefix_returns_bounded_candidates_instead_of_guessing(self):
        status, data = self._lineage("alpha-")

        self.assertEqual(status, 409)
        self.assertEqual(data["error"], "Ambiguous entity reference")
        self.assertEqual(sorted(c["id"] for c in data["candidates"]), ["alpha-1", "alpha-2"])

    def test_unique_prefix_resolves_and_title_substrings_do_not(self):
        unique_status, unique = self._lineage("alpha-2")
        substring_status, _ = self._lineage("one")

        self.assertEqual((unique_status, unique["root_id"]), (200, "alpha-2"))
        self.assertEqual(substring_status, 404)


if __name__ == "__main__":
    unittest.main()
