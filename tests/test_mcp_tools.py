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
        tools.store_memory(
            content="Token auth via OAuth2 and JWT",
            title="Auth Module",
            owner_id="agent1",
            skip_duplicate_check=True,
        )

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
        res = tools.store_memory(
            content="Full content text of target chunk",
            title="Target Chunk",
            owner_id="agent1",
            skip_duplicate_check=True,
        )
        entity_id = res.split("ID: ")[1].strip()

        chunk = tools.search_memory(entity_id=entity_id)
        self.assertIn("Full content text of target chunk", chunk)

    def test_store_memory_alias_resolution(self):
        res = tools.store_memory(
            text="Some valid long enough text content for testing quality gate",
            tag="#python",
            owner="user_test",
            skip_duplicate_check=True,
        )
        self.assertIn("stored successfully", res)

    def test_store_memory_check_duplicates_only(self):
        tools.store_memory(
            content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers",
            title="OAuth2 Authentication Core",
            owner_id="user_test",
            skip_duplicate_check=True,
        )
        dup_res = tools.store_memory(
            content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers",
            title="OAuth2 Authentication Core",
            owner_id="user_test",
            check_duplicates_only=True,
        )
        self.assertIsInstance(dup_res, dict)
        self.assertTrue(dup_res.get("duplicate_found", False))

    def test_log_event_alias_resolution(self):
        res = tools.log_event(
            agent="test_agent", event_type="decision", description="Decision logged via alias"
        )
        self.assertIn("logged successfully", res)

    def test_get_events_modes(self):
        tools.log_event(
            agent_id="test_agent", type="attempt", content="Event mode test", session_id="sess_123"
        )
        events = tools.get_events(agent_id="test_agent", mode="events")
        self.assertTrue(len(events) > 0)

        session_events = tools.get_events(session_id="sess_123", mode="session")
        self.assertTrue(len(session_events) > 0)

    def test_get_canonical_tags_alias_resolution(self):
        tools.store_memory(
            content="Tag test content",
            title="Tag Test",
            tags=["#database"],
            owner_id="user1",
            skip_duplicate_check=True,
        )
        tags = tools.get_canonical_tags(query="data")
        self.assertIsInstance(tags, list)

    def _tag_count_for_entity(self, entity_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM entity_tags WHERE entity_id = ?", (entity_id,)
        ).fetchone()[0]

    def test_store_memory_update_preserves_tags_when_omitted(self):
        res = tools.store_memory(
            content="Content for tag preservation test on update path",
            title="Tag Preservation Entity",
            tags=["#python", "#backend"],
            owner_id="user1",
            skip_duplicate_check=True,
        )
        entity_id = res.split("ID: ")[1].split()[0]
        self.assertEqual(self._tag_count_for_entity(entity_id), 2)

        update_res = tools.store_memory(
            entity_id=entity_id,
            content="Content for tag preservation test on update path",
            title="Tag Preservation Entity",
            memory_type="decision",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        self.assertIn("stored successfully", update_res)
        self.assertEqual(self._tag_count_for_entity(entity_id), 2)

    def test_store_memory_update_explicit_empty_tags_clears(self):
        res = tools.store_memory(
            content="Content for explicit tag clearing test on update path",
            title="Tag Clearing Entity",
            tags=["#python"],
            owner_id="user1",
            skip_duplicate_check=True,
        )
        entity_id = res.split("ID: ")[1].split()[0]
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)

        tools.store_memory(
            entity_id=entity_id,
            content="Content for explicit tag clearing test on update path",
            title="Tag Clearing Entity",
            tags=[],
            owner_id="user1",
            skip_duplicate_check=True,
        )
        self.assertEqual(self._tag_count_for_entity(entity_id), 0)

    def test_store_memory_update_explicit_tags_replaces(self):
        res = tools.store_memory(
            content="Content for explicit tag replacement test on update path",
            title="Tag Replacement Entity",
            tags=["#alpha"],
            owner_id="user1",
            skip_duplicate_check=True,
        )
        entity_id = res.split("ID: ")[1].split()[0]

        tools.store_memory(
            entity_id=entity_id,
            content="Content for explicit tag replacement test on update path",
            title="Tag Replacement Entity",
            tags=["#beta"],
            owner_id="user1",
            skip_duplicate_check=True,
        )
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)
        row = self.conn.execute(
            """
            SELECT t.name FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = ?
        """,
            (entity_id,),
        ).fetchone()
        self.assertEqual(row[0], "#beta")

    def test_ephemeral_memory_tool(self):
        store_res = tools.ephemeral_memory(
            action="store", key="secret_token", value="super_secret_123"
        )
        self.assertIn("stored successfully", store_res)

        get_res = tools.ephemeral_memory(action="get", key="secret_token")
        self.assertEqual(get_res, "super_secret_123")

    def test_polymorphic_archive_memory(self):
        res1 = tools.store_memory(
            content="Archive test single node",
            title="Single Node",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id1 = res1.split("ID: ")[1].strip()

        arch_res1 = tools.archive_memory(entity_id=id1)
        self.assertIn("successfully archived", arch_res1)

        res2 = tools.store_memory(
            content="Archive test bulk node 1",
            title="Bulk Node 1",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        res3 = tools.store_memory(
            content="Archive test bulk node 2",
            title="Bulk Node 2",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id2 = res2.split("ID: ")[1].strip()
        id3 = res3.split("ID: ")[1].strip()

        # Test passing stringified list / actual list
        arch_res2 = tools.archive_memory(entity_id=[id2, id3])
        self.assertIsInstance(arch_res2, list)

    def test_polymorphic_manage_relation(self):
        res1 = tools.store_memory(
            content="Source entity for relation",
            title="Source Entity",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        res2 = tools.store_memory(
            content="Target entity for relation",
            title="Target Entity",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertIn("Relation successfully stored", rel_res)

        bulk_rel_res = tools.manage_relation(
            relations=[{"source_id": id1, "target_id": id2, "predicate": "links_to"}]
        )
        self.assertIsInstance(bulk_rel_res, list)

    def test_get_canonical_predicates_tool(self):
        results = tools.get_canonical_predicates(query="elaborates")
        names = {r["name"] for r in results}
        self.assertIn("elaborates_on", names)

    def test_get_canonical_predicates_respects_explicit_limit_kwarg(self):
        for i in range(10):
            self.conn.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"limit-test-pred-{i}", f"limit_test_predicate_{i}", f"limit_test_predicate_{i}"),
            )
        self.conn.commit()

        results = tools.get_canonical_predicates(limit=3)
        self.assertEqual(len(results), 3)

    def test_get_canonical_tags_respects_explicit_limit_kwarg(self):
        for i in range(10):
            self.conn.execute(
                "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"limit-test-tag-{i}", f"#limit_test_tag_{i}", f"limittesttag{i}"),
            )
        self.conn.commit()

        results = tools.get_canonical_tags(limit=3)
        self.assertEqual(len(results), 3)

    def test_get_canonical_predicates_explicit_zero_limit_is_respected_not_defaulted(self):
        for i in range(5):
            self.conn.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"zero-limit-pred-{i}", f"zero_limit_predicate_{i}", f"zero_limit_predicate_{i}"),
            )
        self.conn.commit()

        results = tools.get_canonical_predicates(limit=0)
        self.assertEqual(
            len(results),
            0,
            "an explicit limit=0 must be honored (LIMIT 0), not silently replaced by the default 50",
        )

    def test_get_canonical_tags_explicit_zero_limit_is_respected_not_defaulted(self):
        for i in range(5):
            self.conn.execute(
                "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"zero-limit-tag-{i}", f"#zero_limit_tag_{i}", f"zerolimittag{i}"),
            )
        self.conn.commit()

        results = tools.get_canonical_tags(limit=0)
        self.assertEqual(
            len(results),
            0,
            "an explicit limit=0 must be honored (LIMIT 0), not silently replaced by the default 50",
        )

    def test_manage_relation_predicate_canonicalization(self):
        res1 = tools.store_memory(
            content="Source entity for predicate canonicalization test",
            title="Predicate Canon Source",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        res2 = tools.store_memory(
            content="Target entity for predicate canonicalization test",
            title="Predicate Canon Target",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="Depends-On")
        self.assertIn("Relation successfully stored", rel_res)

        row = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ?", (id1, id2)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            row[0],
            "depends_on",
            "manage_relation must persist the CANONICALIZED predicate, not the raw 'Depends-On' input",
        )

        rel_res2 = tools.manage_relation(source_id=id1, target_id=id2, predicate="Depends-On")
        self.assertIn("already exists", rel_res2)

    def test_manage_relation_surfaces_seeded_alias_substitution(self):
        res1 = tools.store_memory(
            content="Source entity for seeded alias substitution test",
            title="Alias Substitution Source",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        res2 = tools.store_memory(
            content="Target entity for seeded alias substitution test",
            title="Alias Substitution Target",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="relates_to")
        self.assertIn("elaborates_on", rel_res)
        self.assertIn(
            "relates_to",
            rel_res,
            "manage_relation must surface the originally requested predicate, not just the canonical one",
        )

    def test_store_memory_memory_type_round_trip(self):
        res = tools.store_memory(
            content="Content for memory_type tool round trip test",
            title="Memory Type Tool Entity",
            owner_id="user1",
            memory_type="preference",
            skip_duplicate_check=True,
        )
        self.assertIn("stored successfully", res)
        entity_id = res.split("ID: ")[1].strip()

        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], "preference")

        # Confirm it also round-trips through search_memory's echoed field.
        search_res = tools.search_memory(owner_id="user1", memory_type_filter="preference")
        ids = {r["id"] for r in search_res}
        self.assertIn(entity_id, ids)

    def test_store_memory_type_alias_resolves_to_memory_type(self):
        res = tools.store_memory(
            content="Content for the 'type' alias resolution test",
            title="Type Alias Entity",
            owner_id="user1",
            type="decision",
            skip_duplicate_check=True,
        )
        self.assertIn("stored successfully", res)
        entity_id = res.split("ID: ")[1].strip()

        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], "decision")

    def test_search_memory_memory_type_filter_round_trip(self):
        tools.store_memory(
            content="Fact-typed content for the memory_type_filter tool test",
            title="Fact Typed Tool Entity",
            owner_id="user1",
            memory_type="fact",
            skip_duplicate_check=True,
        )
        tools.store_memory(
            content="Event-typed content for the memory_type_filter tool test",
            title="Event Typed Tool Entity",
            owner_id="user1",
            memory_type="event",
            skip_duplicate_check=True,
        )

        results = tools.search_memory(memory_type_filter="fact", owner_id="user1")
        self.assertTrue(len(results) > 0)
        for r in results:
            self.assertEqual(r["memory_type"], "fact")

    def test_inspect_graph_modes(self):
        res1 = tools.store_memory(
            content="Root entity node title",
            title="Root Entity",
            owner_id="user1",
            skip_duplicate_check=True,
        )
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

        res1 = tools.store_memory(
            content="PIT MCP dependency source content",
            title="PIT MCP Source",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id1 = res1.split("ID: ")[1].strip()
        res2 = tools.store_memory(
            content="PIT MCP dependency target content",
            title="PIT MCP Target",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id2 = res2.split("ID: ")[1].strip()

        pit_before = datetime.now(UTC).isoformat()
        time.sleep(1.1)
        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertIn("successfully stored", rel_res)

        deps_before = tools.inspect_graph(
            entity_id=id1, mode="dependencies", point_in_time=pit_before
        )
        self.assertIsInstance(deps_before, dict)
        self.assertEqual(
            deps_before["total_dependencies_found"],
            0,
            "edge created after pit_before must not appear",
        )

        deps_now = tools.inspect_graph(entity_id=id1, mode="dependencies")
        self.assertEqual(deps_now["total_dependencies_found"], 1)

        # Lineage threading: consolidate two memories and confirm point_in_time excludes the
        # brand-new consolidated_from ancestry while an unrestricted (now) call includes it.
        res3 = tools.store_memory(
            content="PIT MCP lineage parent A content",
            title="PIT MCP Lineage A",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        a_id = res3.split("ID: ")[1].strip()
        res4 = tools.store_memory(
            content="PIT MCP lineage parent B content",
            title="PIT MCP Lineage B",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        b_id = res4.split("ID: ")[1].strip()

        pit_before_lineage = datetime.now(UTC).isoformat()
        time.sleep(1.1)
        cons_content = (
            "# PIT MCP Consolidated Lineage\n\n"
            "Synthesized summary combining PIT MCP lineage parent facts for point-in-time threading.\n"
            "- Detail alpha\n- Detail beta"
        )
        cons_res = tools.commit_consolidation(
            parent_ids=[a_id, b_id],
            title="PIT MCP Consolidated Lineage Entity",
            content=cons_content,
            owner_id="user1",
        )
        self.assertIn("Successfully committed", cons_res)
        c_id = cons_res.split("ID: ")[1].strip()

        lineage_before = tools.inspect_graph(
            entity_id=c_id, mode="lineage", point_in_time=pit_before_lineage
        )
        self.assertIsInstance(lineage_before, dict)
        self.assertEqual(lineage_before["total_ancestors"], 0)

        lineage_now = tools.inspect_graph(entity_id=c_id, mode="lineage")
        self.assertEqual(lineage_now["total_ancestors"], 2)

    def test_manage_relation_invalidate_mode(self):
        res1 = tools.store_memory(
            content="Source entity for relation invalidation test",
            title="Invalidate MCP Source",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        res2 = tools.store_memory(
            content="Target entity for relation invalidation test",
            title="Invalidate MCP Target",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertIn("Relation successfully stored", rel_res)
        rel_id = rel_res.split("ID: ")[1].rstrip(")")

        inv_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="depends_on", invalidate=True
        )
        self.assertIn("Relation invalidated", inv_res)

        row = self.conn.execute(
            "SELECT invalid_at, valid_to FROM relations WHERE id = ?", (rel_id,)
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNotNone(row[1])
        self.assertEqual(row[0], row[1])

    def test_manage_relation_valid_at_and_invalid_at_passthrough(self):
        res1 = tools.store_memory(
            content="Source entity for valid_at passthrough",
            title="ValidAt Source",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        res2 = tools.store_memory(
            content="Target entity for valid_at passthrough",
            title="ValidAt Target",
            owner_id="user1",
            skip_duplicate_check=True,
        )
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        custom_valid_at = "2025-02-01T00:00:00+00:00"
        rel_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="depends_on", valid_at=custom_valid_at
        )
        self.assertIn("Relation successfully stored", rel_res)
        rel_id = rel_res.split("ID: ")[1].rstrip(")")

        row = self.conn.execute("SELECT valid_at FROM relations WHERE id = ?", (rel_id,)).fetchone()
        self.assertEqual(row[0], custom_valid_at)

        custom_invalid_at = "2025-03-01T00:00:00+00:00"
        inv_res = tools.manage_relation(
            source_id=id1,
            target_id=id2,
            predicate="depends_on",
            invalidate=True,
            invalid_at=custom_invalid_at,
        )
        self.assertIn("Relation invalidated", inv_res)

        row2 = self.conn.execute(
            "SELECT invalid_at FROM relations WHERE id = ?", (rel_id,)
        ).fetchone()
        self.assertEqual(row2[0], custom_invalid_at)

    def _mk_vector_entity(self, title: str, vector: list) -> str:
        """Inserts a bare `entities` row plus a single matching entity_chunk_embeddings row
        (bypassing store_memory's async chunk-embed trigger), so this test controls each
        parent's centroid directly -- mirrors tests/test_relation_service.py's helper of the
        same name/contract."""
        import uuid
        from datetime import datetime, UTC
        import sqlite_vec

        entity_id = str(uuid.uuid4())
        content_hash = f"hash-{entity_id}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', 'raw', ?, ?, ?)",
            (entity_id, now, now, now, title, f"content body for {title}", content_hash),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, 0, 0, 10, ?)",
            (f"{entity_id}::0", entity_id, sqlite_vec.serialize_float32(vector), content_hash),
        )
        self.conn.commit()
        return entity_id

    def test_commit_consolidation_tool_forwards_override_justification(self):
        """override_justification must reach relation_service.commit_consolidation through the
        actual MCP tool wrapper, for both the single-item and bulk-item (per-item) shapes
        (memory-core rework Phase 3, Part A6)."""
        dim = 384

        def _axis(i):
            v = [0.0] * dim
            v[i] = 1.0
            return v

        # Single-item shape.
        a = self._mk_vector_entity("Override Tool A", _axis(0))
        b = self._mk_vector_entity("Override Tool B", _axis(1))  # orthogonal -> incohesive

        res_no_override = tools.commit_consolidation(
            parent_ids=[a, b],
            title="C Override Tool No Justification",
            content=(
                "# Consolidated Record\n\nSynthesized summary combining source facts.\n"
                "- Merged detail alpha\n- Merged detail beta"
            ),
            owner_id="agent_c",
        )
        self.assertTrue(res_no_override.startswith("Error: REJECT_LOW_COHESION"))

        res_with_override = tools.commit_consolidation(
            parent_ids=[a, b],
            title="C Override Tool With Justification",
            content=(
                "# Consolidated Record\n\nSynthesized summary combining source facts.\n"
                "- Merged detail alpha\n- Merged detail beta"
            ),
            owner_id="agent_c",
            override_justification="deliberately merging unrelated fixtures via the MCP tool wrapper",
        )
        self.assertIn("Successfully committed", res_with_override)
        consolidated_id = res_with_override.split("ID: ")[1].strip()
        content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (consolidated_id,)
        ).fetchone()[0]
        self.assertIn("[Consolidation Override]", content)

        # Bulk-item shape: override_justification lives per-item, not shared at the batch level.
        c = self._mk_vector_entity("Override Tool Bulk C", _axis(0))
        d = self._mk_vector_entity("Override Tool Bulk D", _axis(1))
        e = self._mk_vector_entity("Override Tool Bulk E", _axis(5))
        f = self._mk_vector_entity("Override Tool Bulk F", _axis(5))  # cohesive with E

        bulk_content = (
            "# Consolidated Bulk Record\n\nSynthesized summary combining bulk source facts.\n"
            "- Merged bulk detail alpha\n- Merged bulk detail beta"
        )
        bulk_results = tools.commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [c, d],
                    "title": "Bulk Override Item CD",
                    "content": bulk_content,
                    "override_justification": (
                        "deliberately merging unrelated bulk fixtures via the MCP tool wrapper"
                    ),
                },
                {
                    "parent_ids": [e, f],
                    "title": "Bulk Override Item EF",
                    "content": bulk_content,
                },
            ]
        )
        self.assertEqual(len(bulk_results), 2)
        self.assertEqual(bulk_results[0]["status"], "success", bulk_results)
        self.assertEqual(bulk_results[1]["status"], "success", bulk_results)

        cd_content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (bulk_results[0]["entity_id"],)
        ).fetchone()[0]
        ef_content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (bulk_results[1]["entity_id"],)
        ).fetchone()[0]
        self.assertIn("[Consolidation Override]", cd_content)
        self.assertNotIn("[Consolidation Override]", ef_content)

    def test_manage_relation_tool_forwards_override_justification_and_owner_id(self):
        """owner_id and override_justification must reach relation_service.store_relation's
        governance gate through the actual MCP tool wrapper (memory-core rework Phase 5), not
        just through direct service-level calls."""
        dim = 384

        def _axis(i):
            v = [0.0] * dim
            v[i] = 1.0
            return v

        a = self._mk_vector_entity("Relation Gate Tool A", _axis(0))
        b = self._mk_vector_entity("Relation Gate Tool B", _axis(1))  # orthogonal -> low similarity

        res_no_override = tools.manage_relation(source_id=a, target_id=b, predicate="elaborates_on")
        self.assertTrue(
            res_no_override.startswith("Error: REJECT_LOW_RELATION_SIMILARITY"), res_no_override
        )

        res_with_override = tools.manage_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            owner_id="agent_mcp_owner",
            override_justification="deliberately forcing a low-similarity relation via the MCP tool wrapper",
        )
        self.assertIn("Relation successfully stored", res_with_override)

        event = self.conn.execute(
            "SELECT agent_id, content FROM events WHERE type = 'relation_gate_override'"
        ).fetchone()
        self.assertEqual(event[0], "agent_mcp_owner")
        self.assertIn(
            "deliberately forcing a low-similarity relation via the MCP tool wrapper", event[1]
        )

    def test_dismiss_event_invalid(self):
        # Test blank reason
        with self.assertRaises(ValueError) as ctx:
            tools.dismiss_event(event_id="some-id", reason="   ")
        self.assertIn("cannot be empty", str(ctx.exception))

        # Test invalid type
        res = tools.log_event(agent_id="agent1", type="decision", content="foo")
        eid = res.split("ID: ")[1].strip()

        with self.assertRaises(ValueError) as ctx:
            tools.dismiss_event(event_id=eid, reason="bad type")
        self.assertIn("not dismissible types", str(ctx.exception))

        # Test nonexistent ID
        with self.assertRaises(ValueError) as ctx:
            tools.dismiss_event(event_id="fake-id", reason="missing")
        self.assertIn("Events not found", str(ctx.exception))

        # Test bulk atomicity rollback
        res2 = tools.log_event(agent_id="agent1", type="consolidation_request", content="{}")
        eid2 = res2.split("ID: ")[1].strip()

        with self.assertRaises(ValueError):
            tools.dismiss_event(event_id=[eid2, "fake-id2"], reason="rollback test")

        # Verify eid2 is not dismissed (no event_dismissed in db)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='event_dismissed'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_dismiss_event_success_and_idempotency(self):
        mem1 = tools.store_memory(
            content="# Valid Test Memory 1\nThis is a long enough markdown content to pass the quality gate and not get rejected.\n- Bullet point 1\n- Bullet point 2",
            title="Valid Memory 1",
            owner_id="a",
            skip_duplicate_check=True,
        )
        raw_id1 = mem1.split("ID: ")[1].strip()

        mem2 = tools.store_memory(
            content="# Valid Test Memory 2\nThis is another long enough markdown content to pass the quality gate.\n- Bullet point 1\n- Bullet point 2",
            title="Valid Memory 2",
            owner_id="a",
            skip_duplicate_check=True,
        )
        raw_id2 = mem2.split("ID: ")[1].strip()

        res1 = tools.log_event(
            agent_id="a", type="consolidation_request", content=f'{{"entity_ids":["{raw_id1}"]}}'
        )
        eid1 = res1.split("ID: ")[1].strip()

        # eid2 is a top-level `supersession_candidate` EVENT TYPE (the live
        # store_memory-dedup-path signal), distinct from a `consolidation_request`
        # whose `content.target` happens to be the string "supersession_candidate".
        # Per the approved feature contract it IS dismissible, same as
        # `consolidation_request`.
        res2 = tools.log_event(
            agent_id="a", type="supersession_candidate", content=f'{{"new_entity_id":"{raw_id2}"}}'
        )
        eid2 = res2.split("ID: ")[1].strip()

        # Initial status
        events_pre = tools.get_events(status_filter="pending")
        self.assertTrue(any(e["id"] == eid1 for e in events_pre))
        self.assertTrue(any(e["id"] == eid2 for e in events_pre))

        # Dismiss both
        out = tools.dismiss_event(event_id=[eid1, eid1, eid2], reason="obsolete")
        self.assertEqual(out, "Events dismissed successfully")

        # Check status changed
        events_post = tools.get_events(status_filter="dismissed")
        self.assertTrue(any(e["id"] == eid1 for e in events_post))
        self.assertTrue(any(e["id"] == eid2 for e in events_post))

        events_pending = tools.get_events(status_filter="pending")
        self.assertFalse(any(e["id"] == eid1 for e in events_pending))

        # Check idempotency
        out2 = tools.dismiss_event(event_id=eid1, reason="again")
        self.assertEqual(out2, "Events dismissed successfully")

        # Check owner_id alias resolution
        res3 = tools.log_event(
            agent_id="a", type="consolidation_request", content='{"entity_ids":["dummy"]}'
        )
        eid3 = res3.split("ID: ")[1].strip()
        tools.dismiss_event(event_id=eid3, reason="owner test", owner_id="review-owner")
        owner = self.conn.execute(
            "SELECT agent_id FROM events WHERE type='event_dismissed' AND json_extract(content, '$.target_event_id')=? ORDER BY rowid DESC LIMIT 1",
            (eid3,),
        ).fetchone()[0]
        self.assertEqual(owner, "review-owner")

    def test_get_events_status_derivation_malformed_and_resolved(self):
        # Empty payload
        res_empty = tools.log_event(agent_id="a", type="consolidation_request", content="{}")
        eid_empty = res_empty.split("ID: ")[1].strip()

        events = tools.get_events()
        empty_event = next(e for e in events if e["id"] == eid_empty)
        self.assertEqual(empty_event["status"], "pending")

        # Resolved naturally
        mem = tools.store_memory(
            content="# Quality Valid Content\nThis is a test content for resolution that is long enough and formatted nicely.\n- Point A\n- Point B",
            title="Test Res",
            owner_id="x",
            skip_duplicate_check=True,
        )
        raw_id = mem.split("ID: ")[1].strip()

        res_cr = tools.log_event(
            agent_id="a", type="consolidation_request", content=f'{{"entity_ids":["{raw_id}"]}}'
        )
        eid_cr = res_cr.split("ID: ")[1].strip()

        # Should be pending while raw
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_cr)["status"], "pending"
        )

        # Archive it to resolve
        tools.archive_memory(entity_id=raw_id)

        # Should be resolved now
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_cr)["status"], "resolved"
        )

        # But if we dismiss it, dismissal takes precedence
        tools.dismiss_event(event_id=eid_cr, reason="dismissed over resolved")
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_cr)["status"], "dismissed"
        )

        # Test legacy new_raw_entity_ids resolution
        res_legacy = tools.log_event(
            agent_id="a",
            type="consolidation_request",
            content=f'{{"new_raw_entity_ids":["{raw_id}"]}}',
        )
        eid_legacy = res_legacy.split("ID: ")[1].strip()
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_legacy)["status"], "resolved"
        )

        # Test supersession natural resolution
        res_sup = tools.log_event(
            agent_id="a", type="supersession_candidate", content=f'{{"new_entity_id":"{raw_id}"}}'
        )
        eid_sup = res_sup.split("ID: ")[1].strip()
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_sup)["status"], "resolved"
        )

        # Test malformed payload types
        res_malf1 = tools.log_event(
            agent_id="a", type="consolidation_request", content='{"entity_ids": "not-a-list"}'
        )
        eid_malf1 = res_malf1.split("ID: ")[1].strip()
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_malf1)["status"], "pending"
        )

        # Test empty/whitespace IDs list
        res_empty_list = tools.log_event(
            agent_id="a", type="consolidation_request", content='{"entity_ids": ["", "   "]}'
        )
        eid_empty_list = res_empty_list.split("ID: ")[1].strip()
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_empty_list)["status"], "pending"
        )

        # Test mixed-type IDs list: a non-string member must invalidate the whole list
        # rather than being silently filtered out (regression guard for a bug where a
        # payload like {"entity_ids": ["non-raw-id", 7]} was sanitized down to
        # ["non-raw-id"] and incorrectly reported as "resolved").
        res_mixed = tools.log_event(
            agent_id="a",
            type="consolidation_request",
            content='{"entity_ids": ["non-raw-id", 7]}',
        )
        eid_mixed = res_mixed.split("ID: ")[1].strip()
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_mixed)["status"], "pending"
        )

        # Test valid fallback when entity_ids is empty
        res_fallback = tools.log_event(
            agent_id="a",
            type="consolidation_request",
            content=f'{{"entity_ids": [], "new_raw_entity_ids": ["{raw_id}"]}}',
        )
        eid_fallback = res_fallback.split("ID: ")[1].strip()
        self.assertEqual(
            next(e for e in tools.get_events() if e["id"] == eid_fallback)["status"], "resolved"
        )

        # Verify source immutability
        tools.dismiss_event(event_id=eid_malf1, reason="dismiss")
        orig_content = self.conn.execute(
            "SELECT content FROM events WHERE id=?", (eid_malf1,)
        ).fetchone()[0]
        self.assertEqual(orig_content, '{"entity_ids": "not-a-list"}')

    def test_get_events_pagination_and_filtering(self):
        # Add a bunch of events
        for _ in range(5):
            tools.log_event(agent_id="pag_test", type="consolidation_request", content="{}")

        # Get only pending
        pending = tools.get_events(status_filter="pending", limit=2, offset=0, agent_id="pag_test")
        self.assertEqual(len(pending), 2)

        pending_page2 = tools.get_events(
            status_filter="pending", limit=2, offset=2, agent_id="pag_test"
        )
        self.assertEqual(len(pending_page2), 2)

        self.assertNotEqual(pending[0]["id"], pending_page2[0]["id"])

        # Test filtering through nonmatching rows before matching results
        # We add 150 resolved events (which means they have no source IDs)
        # and interleave some pending ones to force multiple batches
        for i in range(120):
            tools.log_event(
                agent_id="pag_deep", type="consolidation_request", content="{}"
            )  # pending
            tools.dismiss_event(
                event_id=tools.get_events(limit=1, type_filter="consolidation_request")[0]["id"],
                reason="resolved",
            )  # make it dismissed
            if i % 30 == 0:
                tools.log_event(
                    agent_id="pag_deep", type="consolidation_request", content='{"malformed": true}'
                )  # pending

        deep_pending = tools.get_events(agent_id="pag_deep", status_filter="pending", limit=2)
        self.assertEqual(len(deep_pending), 2)

        deep_pending_page2 = tools.get_events(
            agent_id="pag_deep", status_filter="pending", limit=2, offset=2
        )
        self.assertEqual(len(deep_pending_page2), 2)
        self.assertNotEqual(deep_pending[0]["id"], deep_pending_page2[0]["id"])

    def test_mcp_tool_count_regression_guard(self):
        registered_count = len(tools.mcp._tool_manager._tools)
        self.assertEqual(
            registered_count,
            13,
            f"MCP server tool count must be exactly 13, got {registered_count}",
        )


if __name__ == "__main__":
    unittest.main()
