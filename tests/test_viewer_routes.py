import unittest
import tempfile
import os
import shutil
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np

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


class TestViewerBrowseAndHybridSearch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "viewer-browse.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _handler(self):
        return SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())

    @staticmethod
    def _capture(handler):
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        return captured

    def _insert_entity(self, entity_id, created_at, updated_at):
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, created_at, updated_at, updated_at, entity_id, f"content {entity_id}"),
        )
        self.conn.commit()

    def test_entities_sort_and_inclusive_utc_date_range_share_count_and_pages(self):
        self._insert_entity("early", "2026-08-10T23:00:00+00:00", "2026-08-11T01:00:00+00:00")
        self._insert_entity("middle", "2026-08-11T12:00:00+00:00", "2026-08-12T01:00:00+00:00")
        self._insert_entity("late", "2026-08-12T01:00:00+00:00", "2026-08-13T01:00:00+00:00")
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_entities(
            {
                "sort": ["created_desc"],
                "date_field": ["created"],
                "date_from": ["2026-08-11"],
                "date_to": ["2026-08-11"],
                "limit": ["1"],
            }
        )
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["data"]["total_count"], 1)
        self.assertEqual(captured["data"]["total_pages"], 1)
        self.assertEqual(captured["data"]["entities"][0]["id"], "middle")
        self.assertEqual(captured["data"]["sort"], "created_desc")

    def test_entities_reject_invalid_temporal_query(self):
        handler = self._handler()
        missing_field = self._capture(handler)
        handler.get_entities({"date_from": ["2026-08-11"]})
        self.assertEqual(missing_field["status"], 400)
        invalid_sort = self._capture(handler)
        handler.get_entities({"sort": ["relevance"]})
        self.assertEqual(invalid_sort["status"], 400)
        reversed_range = self._capture(handler)
        handler.get_entities(
            {"date_field": ["updated"], "date_from": ["2026-08-12"], "date_to": ["2026-08-11"]}
        )
        self.assertEqual(reversed_range["status"], 400)

    @patch("saltmdb.viewer.routes.memory_service.search_memory")
    def test_search_delegates_to_broad_hybrid_service(self, search_memory_mock):
        search_memory_mock.return_value = [{"id": "hybrid-hit", "title": "Hybrid hit", "score": 0.42}]
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_search({"q": ["meaningful query"]})
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["data"]["mode"], "broad")
        self.assertEqual(captured["data"]["results"][0]["id"], "hybrid-hit")
        search_memory_mock.assert_called_once_with(
            query_keywords="meaningful query",
            limit=50,
            include_related=False,
            mode="broad",
            db_path=self.db_path,
        )


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
        core_id = (
            store_memory(
                content="Core architectural fact memory content",
                title="Core Architecture Fact",
                owner_id="viewer_tester",
                is_core=True,
                core_reason="Test fixture core reason for the viewer is_core filter regression test.",
                core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
                skip_duplicate_check=True,
                db_connection=self.conn,
            )
            .split("ID: ")[1]
            .strip()
        )

        non_core_id = (
            store_memory(
                content="Non-core ephemeral detail content",
                title="Non Core Detail",
                owner_id="viewer_tester",
                is_core=False,
                skip_duplicate_check=True,
                db_connection=self.conn,
            )
            .split("ID: ")[1]
            .strip()
        )

        handler = self._handler()

        # Omitted and blank filters preserve the unfiltered entity list.
        captured_all = self._capture_json(handler)
        handler.get_entities({})
        all_ids = {e["id"] for e in captured_all["data"]["entities"]}
        self.assertIn(core_id, all_ids)
        self.assertIn(non_core_id, all_ids)

        captured_blank = self._capture_json(handler)
        handler.get_entities({"is_core": ["   "]})
        blank_ids = {e["id"] for e in captured_blank["data"]["entities"]}
        self.assertEqual(blank_ids, all_ids)

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

        # Accepted aliases are equivalent, including case-insensitive input.
        captured_alias = self._capture_json(handler)
        handler.get_entities({"is_core": ["YeS"]})
        alias_ids = {e["id"] for e in captured_alias["data"]["entities"]}
        self.assertEqual(alias_ids, set(true_ids))

        # A supplied value outside the documented aliases is a client error.
        captured_invalid = self._capture_json(handler)
        handler.get_entities({"is_core": ["sometimes"]})
        self.assertEqual(captured_invalid["status"], 400)
        self.assertIn("error", captured_invalid["data"])

    def test_get_entities_is_core_combines_with_other_filters_and_pagination(self):
        def create_entity(title, is_core, memory_type):
            core_kwargs = {}
            if is_core:
                core_kwargs = {
                    "core_reason": "Test fixture core reason for the viewer entities is_core filter test.",
                    "core_exit_condition": "Test fixture exit condition: this regression test tears down its temp DB.",
                }
            result = store_memory(
                content=f"Unique content for {title}",
                title=title,
                owner_id="viewer_tester",
                is_core=is_core,
                memory_type=memory_type,
                skip_duplicate_check=True,
                db_connection=self.conn,
                **core_kwargs,
            )
            return result.split("ID: ")[1].strip()

        matching_ids = {
            create_entity("Core Decision One", True, "decision"),
            create_entity("Core Decision Two", True, "decision"),
        }
        create_entity("Non Core Decision", False, "decision")
        create_entity("Core Fact", True, "fact")

        handler = self._handler()
        query = {
            "is_core": ["1"],
            "status": ["raw"],
            "memory_type": ["decision"],
            "limit": ["1"],
        }

        captured_page_1 = self._capture_json(handler)
        handler.get_entities({**query, "page": ["1"]})
        page_1 = captured_page_1["data"]
        self.assertEqual(captured_page_1["status"], 200)
        self.assertEqual(page_1["total_count"], 2)
        self.assertEqual(page_1["total_pages"], 2)
        self.assertEqual(page_1["pagination"]["total"], 2)
        self.assertEqual(page_1["pagination"]["total_pages"], 2)
        self.assertEqual(len(page_1["entities"]), 1)
        self.assertTrue(page_1["entities"][0]["is_core"])
        self.assertEqual(page_1["entities"][0]["status"], "raw")
        self.assertEqual(page_1["entities"][0]["memory_type"], "decision")

        captured_page_2 = self._capture_json(handler)
        handler.get_entities({**query, "page": ["2"]})
        page_2 = captured_page_2["data"]
        self.assertEqual(page_2["total_count"], 2)
        self.assertEqual(page_2["pagination"]["total"], 2)
        self.assertEqual(len(page_2["entities"]), 1)
        self.assertEqual(
            {page_1["entities"][0]["id"], page_2["entities"][0]["id"]},
            matching_ids,
        )

    def test_get_search_is_core_filter(self):
        store_memory(
            content="Unique keyword quantum ground state core memory",
            title="Quantum Ground State",
            owner_id="viewer_tester",
            is_core=True,
            core_reason="Test fixture core reason for the viewer search is_core filter regression test.",
            core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
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


class TestViewerScatterplot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        # Real production schema (init_db creates entity_embeddings as an actual vec0
        # virtual table) -- required to exercise/catch a regression in the viewer's
        # own per-request connection needing the sqlite_vec extension loaded before it
        # can query that table, same as the consolidate_vector_clusters regression.
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

    def _insert_ready_entity(self, entity_id, title):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "title, full_content, status, embedding_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', 'ready')",
            (entity_id, now, now, now, "viewer_tester", title, f"Content for {title}"),
        )
        vec = np.ones(384, dtype=np.float32) * (hash(entity_id) % 100 / 100.0)
        self.conn.execute(
            "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (entity_id, vec.tobytes()),
        )

    def test_get_scatterplot_with_ready_embeddings(self):
        self._insert_ready_entity("scatter-e1", "Scatterplot Entity One")
        self._insert_ready_entity("scatter-e2", "Scatterplot Entity Two")
        self.conn.commit()

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_scatterplot()

        self.assertNotIn("error", captured["data"])
        points = captured["data"]["points"]
        self.assertEqual(len(points), 2)
        point_ids = {p["id"] for p in points}
        self.assertEqual(point_ids, {"scatter-e1", "scatter-e2"})
        for p in points:
            self.assertIn("x", p)
            self.assertIn("y", p)


if __name__ == "__main__":
    unittest.main()
