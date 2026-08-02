import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler
from saltmdb.domain.services import relation_service
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation


class DummyRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class DummyServer:
    pass


class BrokenWFile:
    def write(self, b):
        raise ConnectionAbortedError(
            10053, "An established connection was aborted by the software in your host machine"
        )


class TestViewerRoutes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_handler_get_db_connection(self):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        conn = handler.get_db_connection()
        self.assertIsNotNone(conn)
        conn.close()

    def test_client_disconnect_during_send_json_and_html(self):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        handler.requestline = "GET / HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.wfile = BrokenWFile()
        # Should catch ConnectionAbortedError silently without throwing
        handler.send_json({"test": "data"})
        handler.send_html("<html></html>")


class TestViewerRoutesLineageAndParentIds(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _handler(self):
        return SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())

    def _capture_json(self, handler):
        captured = {}

        def fake_send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        handler.send_json = fake_send_json
        return captured

    def _mk(self, title, owner_id="viewer_tester"):
        res = store_memory(
            content=f"Raw content body for entity {title}",
            title=title,
            owner_id=owner_id,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        return res.split("ID: ")[1].strip()

    def _consolidate_two(self, title_a, title_b, cons_title, marker):
        a = self._mk(title_a)
        b = self._mk(title_b)
        content = (
            f"# {cons_title}\n\n"
            f"Synthesized summary combining {title_a} and {title_b} facts for the {marker} scenario.\n"
            "- Detail alpha\n- Detail beta"
        )
        res = commit_consolidation(
            parent_ids=[a, b],
            title=cons_title,
            content=content,
            owner_id="viewer_tester",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", res)
        return a, b, res.split("ID: ")[1].strip()

    def test_get_lineage_delegates_and_matches_relation_service_directly(self):
        a, b, c1 = self._consolidate_two(
            "Viewer Lineage A", "Viewer Lineage B", "Viewer Lineage C1", "delegate-match"
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_lineage(c1)

        direct = relation_service.analyze_lineage(entity_id=c1, db_connection=self.conn)

        payload = captured["data"]
        self.assertEqual(payload["root_id"], c1)

        direct_ids = {n["id"] for n in direct["ancestors"]}
        payload_ids = {n["id"] for n in payload["nodes"]}
        self.assertEqual(
            direct_ids,
            payload_ids,
            "handler's nodes list must match relation_service.analyze_lineage() directly",
        )

        direct_by_id = {n["id"]: n for n in direct["ancestors"]}
        for node in payload["nodes"]:
            d = direct_by_id[node["id"]]
            self.assertEqual(node["depth"], d["generation_depth"])
            self.assertEqual(node["title"], d["title"])
            self.assertEqual(node["status"], d["status"])

    def test_get_lineage_nodes_have_depth_and_generation_depth_equal_and_expected_keys(self):
        a, b, c1 = self._consolidate_two(
            "Viewer Depth A", "Viewer Depth B", "Viewer Depth C1", "depth-keys"
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_lineage(c1)

        nodes = captured["data"]["nodes"]
        self.assertGreaterEqual(len(nodes), 1)
        for node in nodes:
            self.assertIn("depth", node)
            self.assertIn("generation_depth", node)
            self.assertEqual(
                node["depth"],
                node["generation_depth"],
                "the frontend's loadLineage() reads n.depth -- it must equal generation_depth",
            )
            self.assertIn("owner_id", node)
            self.assertIn("title", node)
            self.assertIn("status", node)

    def test_get_lineage_entity_not_found_returns_error_regression(self):
        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_lineage("nonexistent-entity-id-xyz-does-not-exist")
        self.assertEqual(captured["status"], 404)
        self.assertIn("error", captured["data"])

    def test_get_entities_and_entity_detail_parent_ids_populated_correctly(self):
        a, b, c1 = self._consolidate_two(
            "Viewer ParentIds A", "Viewer ParentIds B", "Viewer ParentIds C1", "parent-ids-pin"
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_entities({})
        entities_by_id = {e["id"]: e for e in captured["data"]["entities"]}
        self.assertIn(c1, entities_by_id, "consolidated entity must appear in get_entities results")
        self.assertEqual(
            set(entities_by_id[c1]["parent_ids"]),
            {a, b},
            "get_entities must correctly populate parent_ids via row['parent_ids'] key access",
        )

        captured2 = self._capture_json(handler)
        handler.get_entity_detail(c1)
        self.assertEqual(
            set(captured2["data"]["parent_ids"]),
            {a, b},
            "get_entity_detail must correctly populate parent_ids via row['parent_ids'] key access",
        )

    def test_get_entities_is_core_filter(self):
        core_id = store_memory(
            content="Core architectural fact memory content",
            title="Core Architecture Fact",
            owner_id="viewer_tester",
            is_core=True,
            skip_duplicate_check=True,
            db_connection=self.conn,
        ).split("ID: ")[1].strip()

        non_core_id = store_memory(
            content="Non-core ephemeral detail content",
            title="Non Core Detail",
            owner_id="viewer_tester",
            is_core=False,
            skip_duplicate_check=True,
            db_connection=self.conn,
        ).split("ID: ")[1].strip()

        handler = self._handler()

        # Filter is_core=true
        captured_true = self._capture_json(handler)
        handler.get_entities({"is_core": ["true"]})
        entities_true = captured_true["data"]["entities"]
        true_ids = [e["id"] for e in entities_true]
        self.assertIn(core_id, true_ids)
        self.assertNotIn(non_core_id, true_ids)
        for e in entities_true:
            self.assertTrue(e["is_core"])

        # Filter is_core=false
        captured_false = self._capture_json(handler)
        handler.get_entities({"is_core": ["false"]})
        entities_false = captured_false["data"]["entities"]
        false_ids = [e["id"] for e in entities_false]
        self.assertIn(non_core_id, false_ids)
        self.assertNotIn(core_id, false_ids)
        for e in entities_false:
            self.assertFalse(e["is_core"])

    def test_get_search_is_core_filter(self):
        store_memory(
            content="Unique keyword quantum ground state core memory",
            title="Quantum Ground State",
            owner_id="viewer_tester",
            is_core=True,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_search({"q": ["quantum"], "is_core": ["true"]})
        self.assertEqual(captured["status"], 200)
        results = captured["data"]["results"]
        self.assertIsInstance(results, list)
        for r in results:
            self.assertTrue(r.get("is_core"))


if __name__ == "__main__":
    unittest.main()

