import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerRelationValidity(unittest.TestCase):
    """Every current-state Viewer relation surface must agree on which edges are valid now."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "relations.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        for entity_id in ("A", "B", "C"):
            self.conn.execute(
                """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content)
                   VALUES (?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                           '2026-01-01T00:00:00+00:00', ?, 'body')""",
                (entity_id, f"Memory {entity_id}"),
            )
        # A->B was closed long ago; A->C is still current.
        self._relation("expired", "A", "B", valid_to="2026-02-01T00:00:00+00:00")
        self._relation("current", "A", "C", valid_to=None)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _relation(self, relation_id, source, target, valid_to):
        self.conn.execute(
            """INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from,
                                      valid_to)
               VALUES (?, ?, ?, 'related_to', '2026-01-01T00:00:00+00:00',
                       '2026-01-01T00:00:00+00:00', ?)""",
            (relation_id, source, target, valid_to),
        )

    def _call(self, method_name, *args):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        getattr(handler, method_name)(*args)
        self.assertEqual(captured["status"], 200)
        return captured["data"]

    def test_all_current_state_surfaces_hide_expired_relations(self):
        all_relations = self._call("get_all_relations", {})
        graph = self._call("get_relations_graph", {})
        per_entity = self._call("get_entity_relations", "A", {})
        detail = self._call("get_entity_detail", "A")

        with self.subTest("all relations list"):
            self.assertEqual(all_relations["total_count"], 1)
            self.assertEqual([r["id"] for r in all_relations["relations"]], ["current"])
        with self.subTest("global graph"):
            self.assertEqual(graph["total_edges"], 1)
        with self.subTest("per-entity relations"):
            self.assertEqual(per_entity["total_count"], 1)
            self.assertEqual([r["id"] for r in per_entity["relations"]], ["current"])
        with self.subTest("detail counts and previews"):
            self.assertEqual(detail["relations"]["outgoing_count"], 1)
            self.assertEqual([r["id"] for r in detail["relations"]["outgoing"]], ["current"])


if __name__ == "__main__":
    unittest.main()
