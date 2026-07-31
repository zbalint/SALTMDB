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
        entity_id = res.split("ID: ")[1].split()[0]
        self.assertEqual(self._tag_count_for_entity(entity_id), 2)

        update_res = tools.store_memory(entity_id=entity_id, content="Content for tag preservation test on update path", title="Tag Preservation Entity", is_core=True, owner_id="user1", skip_duplicate_check=True)
        self.assertIn("stored successfully", update_res)
        self.assertEqual(self._tag_count_for_entity(entity_id), 2)

    def test_store_memory_update_explicit_empty_tags_clears(self):
        res = tools.store_memory(content="Content for explicit tag clearing test on update path", title="Tag Clearing Entity", tags=["#python"], owner_id="user1", skip_duplicate_check=True)
        entity_id = res.split("ID: ")[1].split()[0]
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)

        tools.store_memory(entity_id=entity_id, content="Content for explicit tag clearing test on update path", title="Tag Clearing Entity", tags=[], owner_id="user1", skip_duplicate_check=True)
        self.assertEqual(self._tag_count_for_entity(entity_id), 0)

    def test_store_memory_update_explicit_tags_replaces(self):
        res = tools.store_memory(content="Content for explicit tag replacement test on update path", title="Tag Replacement Entity", tags=["#alpha"], owner_id="user1", skip_duplicate_check=True)
        entity_id = res.split("ID: ")[1].split()[0]

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

    def test_get_canonical_predicates_tool(self):
        results = tools.get_canonical_predicates(query="elaborates")
        names = {r["name"] for r in results}
        self.assertIn("elaborates_on", names)

    def test_manage_relation_predicate_canonicalization(self):
        res1 = tools.store_memory(content="Source entity for predicate canonicalization test", title="Predicate Canon Source", owner_id="user1", skip_duplicate_check=True)
        res2 = tools.store_memory(content="Target entity for predicate canonicalization test", title="Predicate Canon Target", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="Depends-On")
        self.assertIn("Relation successfully stored", rel_res)

        row = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ?", (id1, id2)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            row[0], "depends_on",
            "manage_relation must persist the CANONICALIZED predicate, not the raw 'Depends-On' input"
        )

        rel_res2 = tools.manage_relation(source_id=id1, target_id=id2, predicate="Depends-On")
        self.assertIn("already exists", rel_res2)

    def test_store_memory_memory_type_round_trip(self):
        res = tools.store_memory(content="Content for memory_type tool round trip test", title="Memory Type Tool Entity", owner_id="user1", memory_type="preference", skip_duplicate_check=True)
        self.assertIn("stored successfully", res)
        entity_id = res.split("ID: ")[1].strip()

        row = self.conn.execute("SELECT memory_type FROM entities WHERE id = ?", (entity_id,)).fetchone()
        self.assertEqual(row[0], "preference")

        # Confirm it also round-trips through search_memory's echoed field.
        search_res = tools.search_memory(owner_id="user1", memory_type_filter="preference")
        ids = {r["id"] for r in search_res}
        self.assertIn(entity_id, ids)

    def test_store_memory_type_alias_resolves_to_memory_type(self):
        res = tools.store_memory(content="Content for the 'type' alias resolution test", title="Type Alias Entity", owner_id="user1", type="decision", skip_duplicate_check=True)
        self.assertIn("stored successfully", res)
        entity_id = res.split("ID: ")[1].strip()

        row = self.conn.execute("SELECT memory_type FROM entities WHERE id = ?", (entity_id,)).fetchone()
        self.assertEqual(row[0], "decision")

    def test_search_memory_memory_type_filter_round_trip(self):
        tools.store_memory(content="Fact-typed content for the memory_type_filter tool test", title="Fact Typed Tool Entity", owner_id="user1", memory_type="fact", skip_duplicate_check=True)
        tools.store_memory(content="Event-typed content for the memory_type_filter tool test", title="Event Typed Tool Entity", owner_id="user1", memory_type="event", skip_duplicate_check=True)

        results = tools.search_memory(memory_type_filter="fact", owner_id="user1")
        self.assertTrue(len(results) > 0)
        for r in results:
            self.assertEqual(r["memory_type"], "fact")

    def test_inspect_graph_modes(self):
        res1 = tools.store_memory(content="Root entity node title", title="Root Entity", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip() if "ID: " in res1 else "Root Entity"

        deps = tools.inspect_graph(entity_id=id1, mode="dependencies")
        self.assertIsInstance(deps, dict)

        lineage = tools.inspect_graph(entity_id=id1, mode="lineage")
        self.assertIsInstance(lineage, dict)

        orphans = tools.inspect_graph(mode="orphans")
        self.assertIsInstance(orphans, dict)

    def test_inspect_graph_point_in_time_threads_through_dependencies_and_lineage(self):
        import time
        from datetime import datetime, UTC

        res1 = tools.store_memory(content="PIT MCP dependency source content", title="PIT MCP Source", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip()
        res2 = tools.store_memory(content="PIT MCP dependency target content", title="PIT MCP Target", owner_id="user1", skip_duplicate_check=True)
        id2 = res2.split("ID: ")[1].strip()

        pit_before = datetime.now(UTC).isoformat()
        time.sleep(1.1)
        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertIn("successfully stored", rel_res)

        deps_before = tools.inspect_graph(entity_id=id1, mode="dependencies", point_in_time=pit_before)
        self.assertIsInstance(deps_before, dict)
        self.assertEqual(deps_before["total_dependencies_found"], 0, "edge created after pit_before must not appear")

        deps_now = tools.inspect_graph(entity_id=id1, mode="dependencies")
        self.assertEqual(deps_now["total_dependencies_found"], 1)

        # Lineage threading: consolidate two memories and confirm point_in_time excludes the
        # brand-new consolidated_from ancestry while an unrestricted (now) call includes it.
        res3 = tools.store_memory(content="PIT MCP lineage parent A content", title="PIT MCP Lineage A", owner_id="user1", skip_duplicate_check=True)
        a_id = res3.split("ID: ")[1].strip()
        res4 = tools.store_memory(content="PIT MCP lineage parent B content", title="PIT MCP Lineage B", owner_id="user1", skip_duplicate_check=True)
        b_id = res4.split("ID: ")[1].strip()

        pit_before_lineage = datetime.now(UTC).isoformat()
        time.sleep(1.1)
        cons_content = (
            "# PIT MCP Consolidated Lineage\n\n"
            "Synthesized summary combining PIT MCP lineage parent facts for point-in-time threading.\n"
            "- Detail alpha\n- Detail beta"
        )
        cons_res = tools.commit_consolidation(parent_ids=[a_id, b_id], title="PIT MCP Consolidated Lineage Entity", content=cons_content, owner_id="user1")
        self.assertIn("Successfully committed", cons_res)
        c_id = cons_res.split("ID: ")[1].strip()

        lineage_before = tools.inspect_graph(entity_id=c_id, mode="lineage", point_in_time=pit_before_lineage)
        self.assertIsInstance(lineage_before, dict)
        self.assertEqual(lineage_before["total_ancestors"], 0)

        lineage_now = tools.inspect_graph(entity_id=c_id, mode="lineage")
        self.assertEqual(lineage_now["total_ancestors"], 2)

    def test_manage_relation_invalidate_mode(self):
        res1 = tools.store_memory(content="Source entity for relation invalidation test", title="Invalidate MCP Source", owner_id="user1", skip_duplicate_check=True)
        res2 = tools.store_memory(content="Target entity for relation invalidation test", title="Invalidate MCP Target", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertIn("Relation successfully stored", rel_res)
        rel_id = rel_res.split("ID: ")[1].rstrip(")")

        inv_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on", invalidate=True)
        self.assertIn("Relation invalidated", inv_res)

        row = self.conn.execute("SELECT invalid_at, valid_to FROM relations WHERE id = ?", (rel_id,)).fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNotNone(row[1])
        self.assertEqual(row[0], row[1])

    def test_manage_relation_valid_at_and_invalid_at_passthrough(self):
        res1 = tools.store_memory(content="Source entity for valid_at passthrough", title="ValidAt Source", owner_id="user1", skip_duplicate_check=True)
        res2 = tools.store_memory(content="Target entity for valid_at passthrough", title="ValidAt Target", owner_id="user1", skip_duplicate_check=True)
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        custom_valid_at = "2025-02-01T00:00:00+00:00"
        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on", valid_at=custom_valid_at)
        self.assertIn("Relation successfully stored", rel_res)
        rel_id = rel_res.split("ID: ")[1].rstrip(")")

        row = self.conn.execute("SELECT valid_at FROM relations WHERE id = ?", (rel_id,)).fetchone()
        self.assertEqual(row[0], custom_valid_at)

        custom_invalid_at = "2025-03-01T00:00:00+00:00"
        inv_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on", invalidate=True, invalid_at=custom_invalid_at)
        self.assertIn("Relation invalidated", inv_res)

        row2 = self.conn.execute("SELECT invalid_at FROM relations WHERE id = ?", (rel_id,)).fetchone()
        self.assertEqual(row2[0], custom_invalid_at)

    def test_mcp_tool_count_regression_guard(self):
        registered_count = len(tools.mcp._tool_manager._tools)
        self.assertEqual(registered_count, 12, f"MCP server tool count must be exactly 12, got {registered_count}")

if __name__ == "__main__":
    unittest.main()
