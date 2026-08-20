import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.mcp import tools
from saltmdb.mcp.identity import IdentityRebindRejected, SESSION_IDENTITY


class TestMCPToolsWrapper(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        # Track B (scratch/plans/track_b_daemon_detailed.md §8): tools.py's tool functions call
        # through a backend indirection now; inject the in-process DirectDispatchBackend so these
        # tests keep exercising tools.py's argument-normalization layer against this temp DB with
        # no daemon involved, exactly as before.
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_memory_fetches_full(self):
        res = tools.store_memory(
            content="Full content text of target chunk",
            title="Target Chunk",
            tags=["#fetch-full"],
            owner_id="agent1",
        )
        entity_id = res["data"]["id"]

        result = tools.get_memory(entity_id=entity_id, owner_id="agent1")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Full content text of target chunk", result["data"]["content"])

    def test_first_call_without_owner_id_explains_copyable_correction(self):
        """A fresh adapter must teach the caller how to establish identity before dispatch."""
        with self.assertRaises(ValueError) as ctx:
            tools.search_memory(query_keywords="first call identity probe")
        message = str(ctx.exception)
        self.assertIn("owner_id", message)
        self.assertIn("corrected_call", message)

    def test_owner_binding_rejects_mid_session_rebind(self):
        SESSION_IDENTITY.bind("agent_qa")
        with self.assertRaises(IdentityRebindRejected):
            tools.search_memory(query_keywords="identity rebind probe", owner_id="other-agent")

    def test_registered_mcp_schemas_have_no_kwargs_catchall(self):
        """The generated FastMCP schema and Python signatures must agree on explicit fields."""
        import inspect

        for name, registered in tools.mcp._tool_manager._tools.items():
            self.assertNotIn(
                "kwargs",
                registered.parameters.get("properties", {}),
                f"{name} still exposes the obsolete kwargs schema field",
            )
            self.assertNotIn(
                inspect.Parameter.VAR_KEYWORD,
                [param.kind for param in inspect.signature(registered.fn).parameters.values()],
                f"{name} still accepts an untyped **kwargs catchall",
            )

    def test_store_memory_exact_duplicate_is_rejected(self):
        tools.store_memory(
            content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers",
            title="OAuth2 Authentication Core",
            tags=["#auth"],
            owner_id="user_test",
        )
        dup_res = tools.store_memory(
            content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers",
            title="OAuth2 Authentication Core",
            tags=["#auth"],
            owner_id="user_test",
        )
        self.assertEqual(dup_res["status"], "rejected")
        self.assertEqual(dup_res["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")

    def test_get_events_filters_by_context_and_agent(self):
        # Phase 6 (plan §5.7): log_event/get_events no longer have `mode` -- context_id/
        # agent_id/event_type/session_id are all plain equality filters now.
        tools.log_event(
            event_type="attempt",
            content="Event mode test",
            owner_id="test_agent",
            context_id="ctx_get_events_modes_test",
        )
        events = tools.get_events(agent_id="test_agent")
        self.assertTrue(len(events) > 0)

        context_events = tools.get_events(context_id="ctx_get_events_modes_test")
        self.assertTrue(len(context_events) > 0)
        self.assertTrue(all(e["context_id"] == "ctx_get_events_modes_test" for e in context_events))

    def test_search_tags_alias_resolution(self):
        tools.store_memory(
            content="Tag test content",
            title="Tag Test",
            tags=["#database"],
            owner_id="user1",
        )
        tags = tools.search_tags(query="data")
        self.assertIsInstance(tags, list)

    def _tag_count_for_entity(self, entity_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM entity_tags WHERE entity_id = ?", (entity_id,)
        ).fetchone()[0]

    def test_store_memory_update_explicit_empty_tags_rejects_frozen_mutation(self):
        res = tools.store_memory(
            content="Content for explicit tag clearing test on update path",
            title="Tag Clearing Entity",
            tags=["#python"],
            owner_id="user1",
        )
        entity_id = res["data"]["id"]
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)

        rejected = tools.store_memory(
            entity_id=entity_id,
            content="Content for explicit tag clearing test on update path",
            title="Tag Clearing Entity",
            tags=[],
            owner_id="user1",
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["errors"][0]["code"], "IMMUTABLE_MEMORY")
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE id LIKE ?", (entity_id + "_h_%",)
            ).fetchone()[0],
            0,
        )

    def test_store_memory_update_explicit_tags_rejects_frozen_mutation(self):
        res = tools.store_memory(
            content="Content for explicit tag replacement test on update path",
            title="Tag Replacement Entity",
            tags=["#alpha"],
            owner_id="user1",
        )
        entity_id = res["data"]["id"]

        rejected = tools.store_memory(
            entity_id=entity_id,
            content="Content for explicit tag replacement test on update path",
            title="Tag Replacement Entity",
            tags=["#beta"],
            owner_id="user1",
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["errors"][0]["code"], "IMMUTABLE_MEMORY")
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)
        row = self.conn.execute(
            """
            SELECT t.name FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = ?
        """,
            (entity_id,),
        ).fetchone()
        self.assertEqual(row[0], "#alpha")

    def test_store_memory_entity_id_is_explicit_parameter(self):
        """MCP wrapper regression (SALTMDB rework Phase-8 live test, 2026-08-18): entity_id was
        only ever reachable through **kwargs, so FastMCP's auto-generated JSON Schema for the
        live MCP tool never declared it as a property -- real MCP clients calling through the
        schema-validated transport (unlike a direct in-process Python call, which happily
        forwards any keyword into **kwargs regardless of transport) could never actually get an
        explicit entity_id through. entity_id must be a named parameter, same as
        archive_memory/review_core_memory/inspect_graph/search_memory already are."""
        import inspect

        params = inspect.signature(tools.store_memory).parameters
        self.assertIn("entity_id", params)
        self.assertIs(params["entity_id"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)

    def test_store_memory_metadata_is_explicit_parameter(self):
        """MCP wrapper regression: metadata must be an explicit named parameter so FastMCP's
        auto-generated JSON Schema declares it over the real MCP wire protocol."""
        import inspect

        params = inspect.signature(tools.store_memory).parameters
        self.assertIn("metadata", params)
        self.assertIs(params["metadata"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)

    def test_store_memory_entity_id_bypasses_exact_duplicate_on_metadata_only_update(self):
        """Behavioral counterpart to the schema test above: an explicit entity_id alone (no
        explicit entity_id must let a metadata-only edit -- content byte-identical, only
        core_reason/core_exit_condition changing -- go through, matching store_memory's own
        docstring promise. Before the live incident this fixed, this exact call pattern (against
        the real MCP tool, not this direct Python call) returned REJECT_EXACT_DUPLICATE."""
        content = "Content for entity_id-targeted metadata-only update test, left unchanged."
        res = tools.store_memory(
            content=content,
            title="Metadata-Only Update Entity",
            tags=["#metadata"],
            owner_id="user1",
            is_core=True,
            core_reason="A" * 20,
            core_exit_condition="B" * 20,
        )
        entity_id = res["data"]["id"]

        update_res = tools.store_memory(
            entity_id=entity_id,
            content=content,
            title="Metadata-Only Update Entity",
            tags=["#metadata"],
            owner_id="user1",
            core_reason="C" * 25,
            core_exit_condition="D" * 25,
        )
        self.assertEqual(update_res["status"], "ok")

    def test_polymorphic_archive_memory(self):
        res1 = tools.store_memory(
            content="Archive test single node",
            title="Single Node",
            tags=["#archive"],
            owner_id="user1",
        )
        id1 = res1["data"]["id"]

        arch_res1 = tools.archive_memory(entity_id=id1, owner_id="user1")
        self.assertIn("successfully archived", arch_res1)

        res2 = tools.store_memory(
            content="Archive test bulk node 1",
            title="Bulk Node 1",
            tags=["#archive"],
            owner_id="user1",
        )
        res3 = tools.store_memory(
            content="Archive test bulk node 2",
            title="Bulk Node 2",
            tags=["#archive"],
            owner_id="user1",
        )
        id2 = res2["data"]["id"]
        id3 = res3["data"]["id"]

        # Test passing stringified list / actual list
        arch_res2 = tools.archive_memory(entity_id=[id2, id3], owner_id="user1")
        self.assertIsInstance(arch_res2, list)

    def test_polymorphic_manage_relation(self):
        res1 = tools.store_memory(
            content="Source entity for relation",
            title="Source Entity",
            tags=["#relation"],
            owner_id="user1",
        )
        res2 = tools.store_memory(
            content="Target entity for relation",
            title="Target Entity",
            tags=["#relation"],
            owner_id="user1",
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="depends_on", owner_id="user1"
        )
        self.assertIn("Relation successfully stored", rel_res)

        # 'part_of' -- an agent-selectable, non-strong canonical predicate -- so this exercises
        # the bulk-shape plumbing itself, not the predicate-vocabulary gate (covered separately).
        bulk_rel_res = tools.manage_relation(
            relations=[{"source_id": id1, "target_id": id2, "predicate": "part_of"}],
            owner_id="user1",
        )
        self.assertIsInstance(bulk_rel_res, list)

    def test_list_predicates_tool(self):
        results = tools.list_predicates(query="elaborates")
        names = {r["name"] for r in results}
        self.assertIn("elaborates_on", names)

    def test_list_predicates_respects_explicit_limit_kwarg(self):
        for i in range(10):
            self.conn.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"limit-test-pred-{i}", f"limit_test_predicate_{i}", f"limit_test_predicate_{i}"),
            )
        self.conn.commit()

        results = tools.list_predicates(limit=3)
        self.assertEqual(len(results), 3)

    def test_search_tags_respects_explicit_limit_kwarg(self):
        for i in range(10):
            self.conn.execute(
                "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"limit-test-tag-{i}", f"#limit_test_tag_{i}", f"limittesttag{i}"),
            )
        self.conn.commit()

        results = tools.search_tags(limit=3)
        self.assertEqual(len(results), 3)

    def test_list_predicates_explicit_zero_limit_is_respected_not_defaulted(self):
        for i in range(5):
            self.conn.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"zero-limit-pred-{i}", f"zero_limit_predicate_{i}", f"zero_limit_predicate_{i}"),
            )
        self.conn.commit()

        results = tools.list_predicates(limit=0)
        self.assertEqual(
            len(results),
            0,
            "an explicit limit=0 must be honored (LIMIT 0), not silently replaced by the default 50",
        )

    def test_search_tags_explicit_zero_limit_is_respected_not_defaulted(self):
        for i in range(5):
            self.conn.execute(
                "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"zero-limit-tag-{i}", f"#zero_limit_tag_{i}", f"zerolimittag{i}"),
            )
        self.conn.commit()

        results = tools.search_tags(limit=0)
        self.assertEqual(
            len(results),
            0,
            "an explicit limit=0 must be honored (LIMIT 0), not silently replaced by the default 50",
        )

    def test_manage_relation_predicate_canonicalization(self):
        res1 = tools.store_memory(
            content="Source entity for predicate canonicalization test",
            title="Predicate Canon Source",
            tags=["#predicate"],
            owner_id="user1",
        )
        res2 = tools.store_memory(
            content="Target entity for predicate canonicalization test",
            title="Predicate Canon Target",
            tags=["#predicate"],
            owner_id="user1",
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="Depends-On", owner_id="user1"
        )
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

        rel_res2 = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="Depends-On", owner_id="user1"
        )
        self.assertIn("already exists", rel_res2)

    def test_manage_relation_rejects_seeded_alias_with_resubmittable_corrected_call(self):
        # Phase 6 write-time gate (plan §5.8): manage_relation no longer silently substitutes a
        # drifted alias spelling -- it rejects the call with a corrected_call naming the
        # canonical replacement ('relates_to' now aliases 'related_to', the Phase 6 reversal).
        res1 = tools.store_memory(
            content="Source entity for seeded alias substitution test",
            title="Alias Substitution Source",
            tags=["#alias"],
            owner_id="user1",
        )
        res2 = tools.store_memory(
            content="Target entity for seeded alias substitution test",
            title="Alias Substitution Target",
            tags=["#alias"],
            owner_id="user1",
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="relates_to", owner_id="user1"
        )
        self.assertEqual(rel_res["status"], "rejected")
        self.assertEqual(rel_res["errors"][0]["code"], "NONCANONICAL_PREDICATE")
        corrected_call = rel_res["corrected_call"]
        self.assertEqual(corrected_call["predicate"], "related_to")
        self.assertEqual(corrected_call["source_id"], id1)
        self.assertEqual(corrected_call["target_id"], id2)

        resubmit = tools.manage_relation(**corrected_call)
        self.assertIn("Relation successfully stored", resubmit)

    def test_store_memory_memory_type_round_trip(self):
        res = tools.store_memory(
            content="Content for memory_type tool round trip test",
            title="Memory Type Tool Entity",
            tags=["#memory-type"],
            owner_id="user1",
            memory_type="preference",
        )
        self.assertEqual(res["status"], "ok")
        entity_id = res["data"]["id"]

        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], "preference")

        # Confirm it also round-trips through search_memory's echoed field.
        search_res = tools.search_memory(owner_id="user1", memory_type_filter="preference")
        ids = {r["id"] for r in search_res}
        self.assertIn(entity_id, ids)

    def test_search_memory_memory_type_filter_round_trip(self):
        tools.store_memory(
            content="Fact-typed content for the memory_type_filter tool test",
            title="Fact Typed Tool Entity",
            tags=["#memory-type"],
            owner_id="user1",
            memory_type="fact",
        )
        tools.store_memory(
            content="Event-typed content for the memory_type_filter tool test",
            title="Event Typed Tool Entity",
            tags=["#memory-type"],
            owner_id="user1",
            memory_type="event",
        )

        results = tools.search_memory(memory_type_filter="fact", owner_id="user1")
        self.assertTrue(len(results) > 0)
        for r in results:
            self.assertEqual(r["memory_type"], "fact")

    def test_graph_tools_are_split_by_entry_condition(self):
        res1 = tools.store_memory(
            content="Root entity node title",
            title="Root Entity",
            tags=["#graph"],
            owner_id="user1",
        )
        self.assertEqual(res1["status"], "ok")
        id1 = res1["data"]["id"]

        deps = tools.get_related_memories(entity_id=id1, owner_id="user1")
        self.assertIsInstance(deps, dict)

        lineage = tools.get_lineage(entity_id=id1, owner_id="user1")
        self.assertIsInstance(lineage, dict)

        memory = tools.get_memory(entity_id=id1, owner_id="user1")
        self.assertEqual(memory["status"], "ok")

    def test_graph_tools_honor_depth_limits(self):
        res1 = tools.store_memory(
            content="PIT MCP dependency source content",
            title="PIT MCP Source",
            tags=["#pit"],
            owner_id="user1",
        )
        id1 = res1["data"]["id"]
        res2 = tools.store_memory(
            content="PIT MCP dependency target content",
            title="PIT MCP Target",
            tags=["#pit"],
            owner_id="user1",
        )
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="depends_on", owner_id="user1"
        )
        self.assertIn("successfully stored", rel_res)

        deps_now = tools.get_related_memories(entity_id=id1, max_depth=1, owner_id="user1")
        self.assertEqual(deps_now["total_related_found"], 1)

        # Lineage threading: consolidate two memories and confirm point_in_time excludes the
        # brand-new consolidated_from ancestry while an unrestricted (now) call includes it.
        res3 = tools.store_memory(
            content="PIT MCP lineage parent A content",
            title="PIT MCP Lineage A",
            tags=["#pit"],
            owner_id="user1",
        )
        a_id = res3["data"]["id"]
        res4 = tools.store_memory(
            content="PIT MCP lineage parent B content",
            title="PIT MCP Lineage B",
            tags=["#pit"],
            owner_id="user1",
        )
        b_id = res4["data"]["id"]

        cons_content = (
            "# PIT MCP Consolidated Lineage\n\n"
            "Synthesized summary combining PIT MCP lineage parent facts for point-in-time threading.\n"
            "- Detail alpha\n- Detail beta"
        )
        cons_res = tools.consolidate_memories(
            parent_ids=[a_id, b_id],
            title="PIT MCP Consolidated Lineage Entity",
            content=cons_content,
            owner_id="user1",
        )
        self.assertEqual(cons_res["status"], "ok")
        c_id = cons_res["data"]["entity_id"]

        lineage_now = tools.get_lineage(entity_id=c_id, owner_id="user1")
        self.assertEqual(lineage_now["total"], 2)

    def test_manage_relation_invalidate_mode(self):
        res1 = tools.store_memory(
            content="Source entity for relation invalidation test",
            title="Invalidate MCP Source",
            tags=["#relation"],
            owner_id="user1",
        )
        res2 = tools.store_memory(
            content="Target entity for relation invalidation test",
            title="Invalidate MCP Target",
            tags=["#relation"],
            owner_id="user1",
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="depends_on", owner_id="user1"
        )
        self.assertIn("Relation successfully stored", rel_res)
        rel_id = rel_res.split("ID: ")[1].rstrip(")")

        inv_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="depends_on", invalidate=True, owner_id="user1"
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
            tags=["#relation"],
            owner_id="user1",
        )
        res2 = tools.store_memory(
            content="Target entity for valid_at passthrough",
            title="ValidAt Target",
            tags=["#relation"],
            owner_id="user1",
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        custom_valid_at = "2025-02-01T00:00:00+00:00"
        rel_res = tools.manage_relation(
            source_id=id1,
            target_id=id2,
            predicate="depends_on",
            valid_at=custom_valid_at,
            owner_id="user1",
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
            owner_id="user1",
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

        res_no_override = tools.consolidate_memories(
            parent_ids=[a, b],
            title="C Override Tool No Justification",
            content=(
                "# Consolidated Record\n\nSynthesized summary combining source facts.\n"
                "- Merged detail alpha\n- Merged detail beta"
            ),
            owner_id="agent_c",
        )
        self.assertEqual(res_no_override["status"], "rejected")
        self.assertEqual(res_no_override["errors"][0]["code"], "REJECT_LOW_COHESION")

        res_with_override = tools.consolidate_memories(
            parent_ids=[a, b],
            title="C Override Tool With Justification",
            content=(
                "# Consolidated Record\n\nSynthesized summary combining source facts.\n"
                "- Merged detail alpha\n- Merged detail beta"
            ),
            owner_id="agent_c",
            override_justification="deliberately merging unrelated fixtures via the MCP tool wrapper",
        )
        self.assertEqual(res_with_override["status"], "ok")
        consolidated_id = res_with_override["data"]["entity_id"]
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
            ],
            owner_id="agent_c",
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

    def test_commit_consolidation_tool_forwards_owner_id_and_context_id(self):
        """owner_id/context_id must reach relation_service.bulk_commit_consolidation through the
        actual MCP tool wrapper for the bulk (`consolidations`) shape: a top-level value is the
        batch-wide default, while an item's own value overrides it per item. Regression for the
        bulk-consolidation ownership-attribution gap found 2026-08-19 live-testing: dispatch.py's
        bulk branch was forwarding only `consolidations`, silently dropping owner_id/context_id,
        so every bulk-consolidated entity landed with relation_service's fallback
        owner_id="system" and context_id=None regardless of what was requested."""
        dim = 384

        def _axis(i):
            v = [0.0] * dim
            v[i] = 1.0
            return v

        a = self._mk_vector_entity("Owner Default Bulk A", _axis(0))
        b = self._mk_vector_entity("Owner Default Bulk B", _axis(0))  # same axis -> cohesive
        c = self._mk_vector_entity("Owner Override Bulk C", _axis(1))
        d = self._mk_vector_entity("Owner Override Bulk D", _axis(1))

        bulk_content = (
            "# Consolidated Bulk Record\n\nSynthesized summary combining bulk source facts.\n"
            "- Merged bulk detail alpha\n- Merged bulk detail beta"
        )
        bulk_results = tools.commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [a, b],
                    "title": "Bulk Owner Default Item AB",
                    "content": bulk_content,
                    # no owner_id/context_id here -> must inherit the top-level batch defaults
                },
                {
                    "parent_ids": [c, d],
                    "title": "Bulk Owner Override Item CD",
                    "content": bulk_content,
                    "owner_id": "agent_override",
                    "context_id": "ctx_override",
                },
            ],
            owner_id="agent_batch_default",
            context_id="ctx_batch_default",
        )
        self.assertEqual(bulk_results[0]["status"], "success", bulk_results)
        self.assertEqual(bulk_results[1]["status"], "success", bulk_results)

        row_ab = self.conn.execute(
            "SELECT owner_id, context_id FROM entities WHERE id = ?",
            (bulk_results[0]["entity_id"],),
        ).fetchone()
        row_cd = self.conn.execute(
            "SELECT owner_id, context_id FROM entities WHERE id = ?",
            (bulk_results[1]["entity_id"],),
        ).fetchone()
        self.assertEqual(row_ab, ("agent_batch_default", "ctx_batch_default"))
        self.assertEqual(row_cd, ("agent_override", "ctx_override"))

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

        res_no_override = tools.manage_relation(
            source_id=a, target_id=b, predicate="elaborates_on", owner_id="agent_mcp_owner"
        )
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

    def test_get_events_pagination(self):
        # Phase 6 (plan §5.7): get_events collapsed to one unconditional SELECT ... LIMIT ?
        # OFFSET ? -- no more status_filter/dismiss-driven pagination semantics (dismiss_event
        # itself was removed this phase). Extracted from the old
        # test_get_events_pagination_and_filtering, keeping only the plain limit/offset
        # assertion that is still meaningful under the new contract.
        for _ in range(5):
            tools.log_event(
                event_type="consolidation_request",
                content="{}",
                owner_id="pag_test",
            )

        page1 = tools.get_events(agent_id="pag_test", limit=2, offset=0)
        self.assertEqual(len(page1), 2)

        page2 = tools.get_events(agent_id="pag_test", limit=2, offset=2)
        self.assertEqual(len(page2), 2)

        self.assertNotEqual(page1[0]["id"], page2[0]["id"])

    def test_mcp_tool_count_regression_guard(self):
        registered_count = len(tools.mcp._tool_manager._tools)
        self.assertEqual(
            registered_count,
            16,
            f"MCP server tool count must be exactly 16 after Phase 7 (ephemeral_memory and "
            f"export_corpus_snapshot removed, following Phase 6's dismiss_event removal), "
            f"got {registered_count}",
        )


class TestConsolidateMemoriesOutputSchema(unittest.IsolatedAsyncioTestCase):
    """Live-verification regression (2026-08-19): every prior test called
    tools.consolidate_memories(...) as a plain Python function, which never exercises FastMCP's
    own output-schema Pydantic validation -- only the real mcp.call_tool(...) round trip does
    that. That gap let consolidate_memories's `-> str | list` return annotation (missing `dict`,
    unlike store_memory/revise_memory/supersede_memory's `str | dict` siblings) go undetected:
    the singular/non-bulk path's actual dict envelope failed validation on every live call despite
    the underlying consolidation succeeding server-side every time. Fixed by widening the
    annotation to `str | list | dict` (tools.py:671)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_singular_consolidate_survives_real_call_tool_output_validation(self):
        a = tools.store_memory(
            content="Parent A content for the real call_tool output-schema regression probe",
            title="Output Schema Probe Parent A",
            tags=["#probe"],
            owner_id="user1",
        )
        b = tools.store_memory(
            content="Parent B content, closely related, for the same output-schema probe",
            title="Output Schema Probe Parent B",
            tags=["#probe"],
            owner_id="user1",
        )

        # This must go through tools.mcp.call_tool (the real FastMCP protocol entry point, not a
        # direct tools.consolidate_memories(...) call) -- only call_tool runs the output-schema
        # Pydantic validation that the return-type annotation feeds.
        result = await tools.mcp.call_tool(
            "consolidate_memories",
            {
                "parent_ids": [a["data"]["id"], b["data"]["id"]],
                "title": "Output Schema Probe Consolidated",
                "content": "Consolidated probe content combining A and B.",
                "owner_id": "user1",
            },
        )
        structured = result[1]["result"]
        self.assertEqual(structured["status"], "ok")
        self.assertIn("entity_id", structured["data"])


class TestReviewCoreMemoryTool(unittest.TestCase):
    """End-to-end coverage of the review_core_memory MCP tool through tools.py's own argument-
    normalization layer and daemon/dispatch.py's DISPATCH_TABLE entry -- core_governance_service's
    own logic already has dedicated unit coverage in test_core_governance_service.py."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store_core(self, title):
        res = tools.store_memory(
            content=f"Distinct fixture content body for {title}, not a near-duplicate.",
            title=title,
            tags=["#core-test"],
            owner_id="tester",
            is_core=True,
            core_reason="A" * 20,
            core_exit_condition="B" * 20,
        )
        self.assertEqual(res["status"], "ok")
        return res["data"]["id"]

    def test_retain_via_mcp_tool(self):
        entity_id = self._store_core("MCP Retain Core")
        result = tools.review_core_memory(
            entity_id=entity_id,
            outcome="retain",
            review_rationale="C" * 20,
            owner_id="reviewer_agent",
        )
        self.assertIn("retained as core", result)

    def test_demote_via_mcp_tool(self):
        entity_id = self._store_core("MCP Demote Core")
        result = tools.review_core_memory(
            entity_id=entity_id,
            outcome="demote",
            review_rationale="C" * 20,
            owner_id="reviewer_agent",
        )
        self.assertIn("demoted", result)
        row = self.conn.execute(
            "SELECT is_core FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertFalse(bool(row[0]))

    def test_archive_via_mcp_tool(self):
        entity_id = self._store_core("MCP Archive Core")
        result = tools.review_core_memory(
            entity_id=entity_id,
            outcome="archive",
            review_rationale="C" * 20,
            owner_id="reviewer_agent",
        )
        self.assertIn("archived", result)

    def test_missing_required_fields_rejected(self):
        # Matches dismiss_event's own established convention (mcp/tools.py): a genuinely missing
        # required field raises ValueError at the dispatch boundary rather than returning an
        # "Error: ..." string -- a malformed/incomplete *value* (e.g. review_rationale too
        # short) still returns a string, exercised by the core_governance_service unit tests.
        entity_id = self._store_core("MCP Missing Fields Core")
        with self.assertRaises(ValueError):
            tools.review_core_memory(entity_id=entity_id, outcome="retain")

    def test_get_core_bootstrap_digest_dispatch_entry(self):
        """Not a public MCP tool -- exercised directly through the daemon dispatch table, the
        same internal read path saltmdb-cli's bootstrap-digest command calls in production."""
        from saltmdb.daemon import dispatch

        self._store_core("Dispatch Digest Core")
        digest = dispatch.DISPATCH_TABLE["get_core_bootstrap_digest"]()
        self.assertIn("<saltmdb-digest>", digest)
        self.assertIn("Dispatch Digest Core", digest)


class TestStrictIsCoreAtAdapterBoundary(unittest.TestCase):
    """Core-memory governance resolved gap #6: the MCP adapter layer must reject ambiguous
    is_core values outright rather than silently coercing them to False."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ambiguous_is_core_string_rejected_not_coerced(self):
        result = tools.store_memory(
            content="Content long enough to clear the quality gate minimum length.",
            title="Ambiguous Is Core Value",
            tags=["#core-test"],
            owner_id="tester",
            is_core="yes",
        )
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("Error"), result)

    def test_true_still_creates_a_core_with_lifecycle_fields(self):
        result = tools.store_memory(
            content="Content long enough to clear the quality gate minimum length.",
            title="Explicit Boolean True",
            tags=["#core-test"],
            owner_id="tester",
            is_core=True,
            core_reason="A" * 20,
            core_exit_condition="B" * 20,
        )
        self.assertEqual(result["status"], "ok")


class TestManageRelationPredicateGate(unittest.TestCase):
    """End-to-end coverage of manage_relation's closed-vocabulary pre-flight gate (agent API
    redesign plan §5.8, Phase 6 item 25), through tools.py's real argument-normalization layer
    against a temp DB via DirectDispatchBackend -- same pattern as TestMCPToolsWrapper."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk_pair(self, label):
        res1 = tools.store_memory(
            content=f"Source entity content for {label} test",
            title=f"{label} Source",
            tags=["#gate"],
            owner_id="gate_tester",
        )
        res2 = tools.store_memory(
            content=f"Target entity content for {label} test",
            title=f"{label} Target",
            tags=["#gate"],
            owner_id="gate_tester",
        )
        return res1["data"]["id"], res2["data"]["id"]

    def test_selectable_predicate_succeeds(self):
        id1, id2 = self._mk_pair("selectable")
        res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="part_of", owner_id="gate_tester"
        )
        self.assertIsInstance(res, str)
        self.assertIn("Relation successfully stored", res)

    def test_each_reserved_predicate_is_refused_naming_its_lifecycle_tool_with_no_corrected_call(
        self,
    ):
        expected_tools = {
            "supersedes": "supersede_memory",
            "consolidated_from": "consolidate_memories",
            "revises": "revise_memory",
        }
        for predicate, lifecycle_tool in expected_tools.items():
            id1, id2 = self._mk_pair(f"reserved_{predicate}")
            res = tools.manage_relation(
                source_id=id1, target_id=id2, predicate=predicate, owner_id="gate_tester"
            )
            self.assertEqual(res["status"], "rejected", predicate)
            self.assertEqual(res["errors"][0]["code"], "RESERVED_PREDICATE", predicate)
            self.assertIn(lifecycle_tool, res["errors"][0]["message"], predicate)
            self.assertNotIn("corrected_call", res, predicate)

    def test_similar_to_is_refused_with_no_corrected_call(self):
        id1, id2 = self._mk_pair("similar_to")
        res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="similar_to", owner_id="gate_tester"
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "LEGACY_READONLY_PREDICATE")
        self.assertNotIn("corrected_call", res)

    def test_same_direction_alias_is_refused_and_corrected_call_resubmits_successfully(self):
        id1, id2 = self._mk_pair("same_direction_alias")
        res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="relates_to", owner_id="gate_tester"
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "NONCANONICAL_PREDICATE")
        corrected_call = res["corrected_call"]
        self.assertEqual(corrected_call["predicate"], "related_to")
        self.assertEqual(corrected_call["source_id"], id1)
        self.assertEqual(corrected_call["target_id"], id2)

        resubmit = tools.manage_relation(**corrected_call)
        self.assertIn("Relation successfully stored", resubmit)

    def test_swap_alias_is_refused_and_corrected_call_swaps_ids_and_resubmits_successfully(self):
        # 'affects' -> 'caused_by' with source_id/target_id swapped (A affects B -> B caused_by
        # A). caused_by is not a RELATION_GATE_STRONG_PREDICATE, so this exercises the swap
        # mechanics in isolation from the embedding-similarity gate.
        id1, id2 = self._mk_pair("swap_alias")
        res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="affects", owner_id="gate_tester"
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "NONCANONICAL_PREDICATE")
        corrected_call = res["corrected_call"]
        self.assertEqual(corrected_call["predicate"], "caused_by")
        self.assertEqual(corrected_call["source_id"], id2)
        self.assertEqual(corrected_call["target_id"], id1)

        resubmit = tools.manage_relation(**corrected_call)
        self.assertIn("Relation successfully stored", resubmit)

        row = self.conn.execute(
            "SELECT source_id, target_id FROM relations WHERE predicate = 'caused_by'"
        ).fetchone()
        self.assertEqual(row, (id2, id1))

    def test_unknown_predicate_is_refused_listing_valid_predicates_with_no_corrected_call(self):
        id1, id2 = self._mk_pair("unknown")
        res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="completely_made_up", owner_id="gate_tester"
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "UNKNOWN_PREDICATE")
        self.assertIn("depends_on", res["errors"][0]["message"])
        self.assertNotIn("corrected_call", res)

    def test_invalidate_true_bypasses_the_gate_entirely(self):
        # invalidate=True never runs the create-time gate (single-item shape) -- confirmed
        # intentional per manage_relation's own docstring: "This gate applies only to creating
        # a new edge, never to invalidate=True".
        id1, id2 = self._mk_pair("invalidate_bypass")
        create_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="related_to", owner_id="gate_tester"
        )
        self.assertIn("Relation successfully stored", create_res)

        # (a) invalidating with the already-canonical predicate works, unsurprisingly.
        inv_res = tools.manage_relation(
            source_id=id1,
            target_id=id2,
            predicate="related_to",
            invalidate=True,
            owner_id="gate_tester",
        )
        self.assertIn("Relation invalidated", inv_res)

        # (b) invalidating with a predicate that WOULD be gated on create ('relates_to' is an
        # alias, refused by test_same_direction_alias_is_refused_... above) still succeeds when
        # invalidate=True, because invalidate_relation's read-side canonicalization still
        # resolves the alias to the same underlying edge.
        id3, id4 = self._mk_pair("invalidate_bypass_alias")
        tools.manage_relation(
            source_id=id3, target_id=id4, predicate="related_to", owner_id="gate_tester"
        )
        inv_res2 = tools.manage_relation(
            source_id=id3,
            target_id=id4,
            predicate="relates_to",
            invalidate=True,
            owner_id="gate_tester",
        )
        self.assertIsInstance(
            inv_res2,
            str,
            "invalidate=True with a would-be-gated alias predicate must reach "
            "invalidate_relation directly, not the rejected-envelope gate",
        )
        self.assertIn("Relation invalidated", inv_res2)

    def test_bulk_one_valid_one_alias_rejects_whole_call_with_zero_side_effects_and_resubmits(
        self,
    ):
        id1, id2 = self._mk_pair("bulk_valid")
        id3, id4 = self._mk_pair("bulk_alias")

        res = tools.manage_relation(
            relations=[
                {"source_id": id1, "target_id": id2, "predicate": "part_of"},
                {"source_id": id3, "target_id": id4, "predicate": "relates_to"},
            ],
            owner_id="gate_tester",
        )
        self.assertEqual(res["status"], "rejected")
        codes = {e["code"] for e in res["errors"]}
        self.assertEqual(codes, {"NONCANONICAL_PREDICATE"})

        # Zero side effects: the valid item's edge must NOT have been created.
        row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id = ? AND target_id = ? AND predicate = 'part_of'",
            (id1, id2),
        ).fetchone()
        self.assertIsNone(row)

        corrected_call = res["corrected_call"]
        corrected_predicates = {r["predicate"] for r in corrected_call["relations"]}
        self.assertEqual(corrected_predicates, {"part_of", "related_to"})

        resubmit = tools.manage_relation(**corrected_call)
        self.assertIsInstance(resubmit, list)
        self.assertTrue(all(item["status"] == "success" for item in resubmit), resubmit)

    def test_bulk_one_alias_one_reserved_rejects_with_no_corrected_call(self):
        id1, id2 = self._mk_pair("bulk_alias_only")
        id3, id4 = self._mk_pair("bulk_reserved_only")

        res = tools.manage_relation(
            relations=[
                {"source_id": id1, "target_id": id2, "predicate": "relates_to"},
                {"source_id": id3, "target_id": id4, "predicate": "supersedes"},
            ],
            owner_id="gate_tester",
        )
        self.assertEqual(res["status"], "rejected")
        codes = {e["code"] for e in res["errors"]}
        self.assertEqual(codes, {"NONCANONICAL_PREDICATE", "RESERVED_PREDICATE"})
        self.assertNotIn(
            "corrected_call",
            res,
            "a batch with a non-derivable item (reserved/legacy_readonly/unknown) must not "
            "offer a corrected_call, even though another item in the same batch was a "
            "mechanically-derivable alias",
        )


class TestGetEventsEndToEnd(unittest.TestCase):
    """End-to-end coverage of get_events' reshaped filters (agent API redesign plan §5.7, Phase
    6 item 23): context_id/agent_id/event_type/session_id equality filters, order, limit/offset,
    and the removed-kwarg regression guard."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_context_id_filters_correctly(self):
        tools.log_event(
            event_type="issue", content="ctx A event", owner_id="agent_x", context_id="ctx_A"
        )
        tools.log_event(
            event_type="issue", content="ctx B event", owner_id="agent_x", context_id="ctx_B"
        )

        events = tools.get_events(context_id="ctx_A")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["context_id"], "ctx_A")

    def test_agent_id_filters_correctly(self):
        tools.log_event(event_type="issue", content="agent one event", owner_id="agent_one")
        # owner_id is bound per adapter session (immutable for its life, §4.5) -- reset between
        # calls attributed to a different agent, exactly like starting a new MCP connection.
        SESSION_IDENTITY.reset()
        tools.log_event(event_type="issue", content="agent two event", owner_id="agent_two")

        events = tools.get_events(agent_id="agent_one")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent_id"], "agent_one")

    def test_event_type_filters_correctly(self):
        tools.log_event(event_type="issue", content="an issue", owner_id="agent_evt_type")
        tools.log_event(event_type="decision", content="a decision", owner_id="agent_evt_type")

        events = tools.get_events(agent_id="agent_evt_type", event_type="decision")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "decision")

    def test_session_id_filters_correctly(self):
        SESSION_IDENTITY.bind_host_session_id("host-session-filter-test")
        tools.log_event(event_type="issue", content="session-bound event", owner_id="agent_sess")
        tools.log_event(event_type="issue", content="no session id from get_events' own filter arg")

        events = tools.get_events(session_id="host-session-filter-test")
        self.assertTrue(len(events) >= 1)
        self.assertTrue(all(e["session_id"] == "host-session-filter-test" for e in events))

    def test_order_oldest_first_vs_newest_first_changes_result_order(self):
        tools.log_event(event_type="issue", content="first", owner_id="agent_order")
        tools.log_event(event_type="issue", content="second", owner_id="agent_order")
        tools.log_event(event_type="issue", content="third", owner_id="agent_order")

        newest_first = tools.get_events(agent_id="agent_order", order="newest_first")
        oldest_first = tools.get_events(agent_id="agent_order", order="oldest_first")

        self.assertEqual(len(newest_first), 3)
        self.assertEqual(len(oldest_first), 3)
        self.assertEqual(list(reversed(newest_first)), oldest_first)
        self.assertNotEqual([e["id"] for e in newest_first], [e["id"] for e in oldest_first])

    def test_limit_and_offset_paginate_correctly(self):
        for i in range(5):
            tools.log_event(event_type="issue", content=f"paged {i}", owner_id="agent_page")

        page1 = tools.get_events(agent_id="agent_page", order="oldest_first", limit=2, offset=0)
        page2 = tools.get_events(agent_id="agent_page", order="oldest_first", limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        self.assertNotEqual([e["id"] for e in page1], [e["id"] for e in page2])

    def test_removed_kwargs_raise_type_error(self):
        for bad_kwargs in (
            {"mode": "events"},
            {"status_filter": "pending"},
            {"owner_id": "someone"},
        ):
            with self.assertRaises(TypeError, msg=bad_kwargs):
                tools.get_events(**bad_kwargs)


class TestLogEventEndToEnd(unittest.TestCase):
    """End-to-end coverage of log_event's Phase 6 reshaping (agent API redesign plan §5.7, item
    23): event_type as the parameter name, no agent_id parameter, owner binding as agent_id, and
    session_id auto-populated from SESSION_IDENTITY.host_session_id."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_event_type_is_the_parameter_name(self):
        res = tools.log_event(event_type="decision", content="picked option A", owner_id="agent_p")
        self.assertIn("Event logged successfully", res)

    def test_no_agent_id_parameter_exists(self):
        with self.assertRaises(TypeError):
            tools.log_event(
                agent_id="agent_p", event_type="decision", content="x", owner_id="agent_p"
            )

    def test_bound_owner_becomes_stored_agent_id(self):
        res = tools.log_event(
            event_type="issue", content="owner binding check", owner_id="agent_bound"
        )
        event_id = res.split("ID: ")[1].split()[0]
        row = self.conn.execute("SELECT agent_id FROM events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row[0], "agent_bound")

    def test_session_id_reflects_bound_host_session_id(self):
        SESSION_IDENTITY.bind_host_session_id("host-session-log-event-test")
        res = tools.log_event(
            event_type="issue", content="session id check", owner_id="agent_sess2"
        )
        event_id = res.split("ID: ")[1].split()[0]
        row = self.conn.execute(
            "SELECT session_id FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        self.assertEqual(row[0], "host-session-log-event-test")

    def test_session_id_is_none_when_unbound(self):
        res = tools.log_event(
            event_type="issue", content="no session bound", owner_id="agent_sess3"
        )
        event_id = res.split("ID: ")[1].split()[0]
        row = self.conn.execute(
            "SELECT session_id FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        self.assertIsNone(row[0])


class TestDismissEventRemovalRegression(unittest.TestCase):
    """Phase 6 item 24 removal regression: dismiss_event must be gone from every registry, while
    the still-legitimate internal dismiss_events (schema.py's migration sweep) must remain."""

    def test_not_a_registered_mcp_tool(self):
        self.assertNotIn("dismiss_event", tools.mcp._tool_manager._tools)

    def test_not_in_dispatch_table(self):
        from saltmdb.daemon import dispatch

        self.assertNotIn("dismiss_event", dispatch.DISPATCH_TABLE)

    def test_not_in_write_tools(self):
        from saltmdb.daemon import protocol

        self.assertNotIn("dismiss_event", protocol.WRITE_TOOLS)

    def test_domain_service_dismiss_events_still_importable_and_callable(self):
        from saltmdb.domain.services.event_service import dismiss_events

        self.assertTrue(callable(dismiss_events))


class TestSearchTagsListPredicatesRegistration(unittest.TestCase):
    """Phase 6 item 27 rename regression: search_tags/list_predicates are registered under
    their new names everywhere, and the old get_canonical_* names are gone everywhere."""

    def test_new_names_registered_in_mcp_tools(self):
        self.assertIn("search_tags", tools.mcp._tool_manager._tools)
        self.assertIn("list_predicates", tools.mcp._tool_manager._tools)
        self.assertNotIn("get_canonical_tags", tools.mcp._tool_manager._tools)
        self.assertNotIn("get_canonical_predicates", tools.mcp._tool_manager._tools)

    def test_new_names_registered_in_dispatch_table(self):
        from saltmdb.daemon import dispatch

        self.assertIn("search_tags", dispatch.DISPATCH_TABLE)
        self.assertIn("list_predicates", dispatch.DISPATCH_TABLE)
        self.assertNotIn("get_canonical_tags", dispatch.DISPATCH_TABLE)
        self.assertNotIn("get_canonical_predicates", dispatch.DISPATCH_TABLE)

    def test_new_names_registered_in_read_tools(self):
        from saltmdb.daemon import protocol

        self.assertIn("search_tags", protocol.READ_TOOLS)
        self.assertIn("list_predicates", protocol.READ_TOOLS)
        self.assertNotIn("get_canonical_tags", protocol.READ_TOOLS)
        self.assertNotIn("get_canonical_predicates", protocol.READ_TOOLS)


if __name__ == "__main__":
    unittest.main()
