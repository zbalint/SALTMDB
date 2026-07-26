import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.mcp import tools

class TestMCPToolsWrapper(unittest.TestCase):
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

    def test_search_memory_alias_resolution(self):
        tools.store_memory(content="Token auth via OAuth2 and JWT", title="Auth Module", owner_id="agent1", skip_duplicate_check=True)
        
        # Test query alias
        res1 = tools.search_memory(query="authentication OAuth2", owner_id="agent1")
        self.assertTrue(len(res1) > 0)
        self.assertGreater(res1[0]["score"], 0.0)

        # Test q alias
        res2 = tools.search_memory(q="authentication OAuth2", owner_id="agent1")
        self.assertTrue(len(res2) > 0)
        self.assertGreater(res2[0]["score"], 0.0)

        # Test keywords alias
        res3 = tools.search_memory(keywords="authentication OAuth2", owner_id="agent1")
        self.assertTrue(len(res3) > 0)
        self.assertGreater(res3[0]["score"], 0.0)

    def test_search_memory_fetch_full(self):
        res = tools.store_memory(content="Full content text of target chunk", title="Target Chunk", owner_id="agent1", skip_duplicate_check=True)
        entity_id = res.split("ID: ")[1].strip()
        
        chunk = tools.search_memory(entity_id=entity_id)
        self.assertIn("Full content text of target chunk", chunk)

    def test_store_memory_alias_resolution(self):
        res = tools.store_memory(text="Some valid long enough text content for testing quality gate", tag="#python", owner="user_test", skip_duplicate_check=True)
        self.assertIn("stored successfully", res)

    def test_store_memory_check_duplicates_only(self):
        tools.store_memory(content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers", title="OAuth2 Authentication Core", owner_id="user_test", skip_duplicate_check=True)
        dup_res = tools.store_memory(content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers", title="OAuth2 Authentication Core", owner_id="user_test", check_duplicates_only=True)
        self.assertIsInstance(dup_res, dict)
        self.assertTrue(dup_res.get("duplicate_found", False))

    def test_log_event_alias_resolution(self):
        res = tools.log_event(agent="test_agent", event_type="decision", description="Decision logged via alias")
        self.assertIn("logged successfully", res)

    def test_get_events_modes(self):
        tools.log_event(agent_id="test_agent", type="attempt", content="Event mode test", session_id="sess_123")
        events = tools.get_events(agent_id="test_agent", mode="events")
        self.assertTrue(len(events) > 0)

        session_events = tools.get_events(session_id="sess_123", mode="session")
        self.assertTrue(len(session_events) > 0)

    def test_get_canonical_tags_alias_resolution(self):
        tools.store_memory(content="Tag test content", title="Tag Test", tags=["#database"], owner_id="user1", skip_duplicate_check=True)
        tags = tools.get_canonical_tags(query="data")
        self.assertIsInstance(tags, list)

    def _tag_count_for_entity(self, entity_id):
        return self.conn.execute("SELECT COUNT(*) FROM entity_tags WHERE entity_id = ?", (entity_id,)).fetchone()[0]

    def test_store_memory_update_preserves_tags_when_omitted(self):
        res = tools.store_memory(content="Content for tag preservation test on update path", title="Tag Preservation Entity", tags=["#python", "#backend"], owner_id="user1", skip_duplicate_check=True)
        entity_id = res.split("ID: ")[1].strip()
        self.assertEqual(self._tag_count_for_entity(entity_id), 2)

        update_res = tools.store_memory(entity_id=entity_id, content="Content for tag preservation test on update path", title="Tag Preservation Entity", is_core=True, owner_id="user1", skip_duplicate_check=True)
        self.assertIn("stored successfully", update_res)
        self.assertEqual(self._tag_count_for_entity(entity_id), 2)

    def test_store_memory_update_explicit_empty_tags_clears(self):
        res = tools.store_memory(content="Content for explicit tag clearing test on update path", title="Tag Clearing Entity", tags=["#python"], owner_id="user1", skip_duplicate_check=True)
        entity_id = res.split("ID: ")[1].strip()
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)

        tools.store_memory(entity_id=entity_id, content="Content for explicit tag clearing test on update path", title="Tag Clearing Entity", tags=[], owner_id="user1", skip_duplicate_check=True)
        self.assertEqual(self._tag_count_for_entity(entity_id), 0)

    def test_store_memory_update_explicit_tags_replaces(self):
        res = tools.store_memory(content="Content for explicit tag replacement test on update path", title="Tag Replacement Entity", tags=["#alpha"], owner_id="user1", skip_duplicate_check=True)
        entity_id = res.split("ID: ")[1].strip()

        tools.store_memory(entity_id=entity_id, content="Content for explicit tag replacement test on update path", title="Tag Replacement Entity", tags=["#beta"], owner_id="user1", skip_duplicate_check=True)
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)
        row = self.conn.execute("""
            SELECT t.name FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = ?
        """, (entity_id,)).fetchone()
        self.assertEqual(row[0], "#beta")

    def test_ephemeral_memory_tool(self):
        store_res = tools.ephemeral_memory(action="store", key="secret_token", value="super_secret_123")
        self.assertIn("stored successfully", store_res)
        
        get_res = tools.ephemeral_memory(action="get", key="secret_token")
        self.assertEqual(get_res, "super_secret_123")

    def test_polymorphic_archive_memory(self):
        res1 = tools.store_memory(content="Archive test single node", title="Single Node", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip()
        
        arch_res1 = tools.archive_memory(entity_id=id1)
        self.assertIn("successfully archived", arch_res1)

        res2 = tools.store_memory(content="Archive test bulk node 1", title="Bulk Node 1", owner_id="user1", skip_duplicate_check=True)
        res3 = tools.store_memory(content="Archive test bulk node 2", title="Bulk Node 2", owner_id="user1", skip_duplicate_check=True)
        id2 = res2.split("ID: ")[1].strip()
        id3 = res3.split("ID: ")[1].strip()

        # Test passing stringified list / actual list
        arch_res2 = tools.archive_memory(entity_id=[id2, id3])
        self.assertIsInstance(arch_res2, list)

    def test_polymorphic_manage_relation(self):
        res1 = tools.store_memory(content="Source entity for relation", title="Source Entity", owner_id="user1", skip_duplicate_check=True)
        res2 = tools.store_memory(content="Target entity for relation", title="Target Entity", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertIn("Relation successfully stored", rel_res)

        bulk_rel_res = tools.manage_relation(relations=[{"source_id": id1, "target_id": id2, "predicate": "links_to"}])
        self.assertIsInstance(bulk_rel_res, list)

    def test_inspect_graph_modes(self):
        res1 = tools.store_memory(content="Root entity node title", title="Root Entity", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip() if "ID: " in res1 else "Root Entity"

        deps = tools.inspect_graph(entity_id=id1, mode="dependencies")
        self.assertIsInstance(deps, dict)

        lineage = tools.inspect_graph(entity_id=id1, mode="lineage")
        self.assertIsInstance(lineage, dict)

        orphans = tools.inspect_graph(mode="orphans")
        self.assertIsInstance(orphans, dict)

if __name__ == "__main__":
    unittest.main()
