import unittest
import tempfile
import os
import shutil
import json
import uuid
from saltmdb.db.schema import init_db
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class _CaptureBackend:
    def __init__(self):
        self.calls = []

    def call(self, tool_name, kwargs):
        self.calls.append((tool_name, kwargs))
        return {"tool": tool_name, "kwargs": kwargs}


class TestMCPToolsWrapper(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        # Track B (scratch/plans/track_b_daemon_detailed.md §8): tools.py's tool functions call
        # through a backend indirection now; inject the in-process DirectDispatchBackend so these
        # tests keep exercising tools.py's argument-normalization layer against this temp DB with
        # no daemon involved, exactly as before.
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_memory_fetches_full(self):
        res = tools.store_memory(
            content="Full content text of target chunk",
            title="Target Chunk",
            tags=["#fetch-full"],
        )
        entity_id = res["data"]["id"]

        result = tools.get_memory(entity_id=entity_id)
        self.assertEqual(result["status"], "ok")
        self.assertIn("Full content text of target chunk", result["data"]["content"])

    def test_missing_startup_owner_explains_environment_configuration(self):
        SESSION_IDENTITY.reset()
        with self.assertRaises(ValueError) as ctx:
            tools.search_memory(query_keywords="first call identity probe")
        message = str(ctx.exception)
        self.assertIn("SALTMDB_OWNER_ID", message)
        self.assertIn("restart", message)

    def test_owner_id_is_absent_from_every_public_schema(self):
        import inspect

        for registered in tools.mcp._tool_manager._tools.values():
            self.assertNotIn("owner_id", registered.parameters.get("properties", {}))
            self.assertNotIn("owner_id", inspect.signature(registered.fn).parameters)

    def test_owner_id_is_not_accepted_by_public_python_wrappers(self):
        """The Python wrapper surface must match the generated MCP schema exactly."""
        owner_tools = (
            tools.log_event,
            tools.store_memory,
            tools.search_memory,
            tools.archive_memory,
            tools.manage_relation,
            tools.consolidate_memories,
            tools.revise_memory,
            tools.supersede_memory,
            tools.get_memory,
            tools.get_lineage,
            tools.get_related_memories,
            tools.review_core_memory,
        )
        for tool in owner_tools:
            with self.assertRaises(TypeError, msg=tool.__name__):
                tool(owner_id="attacker")

    def test_bulk_item_owner_overrides_are_removed_before_dispatch(self):
        """A stale per-item owner is never allowed to cross the adapter boundary."""
        capture = _CaptureBackend()
        previous = tools._set_backend_for_test(capture)
        try:
            tools.manage_relation(
                relations=[
                    {
                        "source_id": "source",
                        "target_id": "target",
                        "predicate": "part_of",
                        "owner_id": "attacker",
                    }
                ]
            )
            tools.consolidate_memories(
                consolidations=[
                    {
                        "parent_ids": ["parent-a", "parent-b"],
                        "title": "Merged",
                        "content": "Merged content",
                        "owner_id": "attacker",
                    }
                ]
            )
        finally:
            tools._set_backend_for_test(previous)

        relation_kwargs = capture.calls[0][1]
        consolidation_kwargs = capture.calls[1][1]
        self.assertEqual(relation_kwargs["owner_id"], "test_agent")
        self.assertNotIn("owner_id", relation_kwargs["relations"][0])
        self.assertEqual(consolidation_kwargs["owner_id"], "test_agent")
        self.assertNotIn("owner_id", consolidation_kwargs["consolidations"][0])

    def test_daemon_dispatch_forwards_batch_owner_to_relation_service(self):
        """The daemon must retain the adapter-injected owner for bulk relation attribution."""
        from unittest.mock import patch
        from saltmdb.daemon import dispatch

        relations = [{"source_id": "source", "target_id": "target", "predicate": "part_of"}]
        with patch.object(
            dispatch.relation_service, "bulk_store_relations", return_value=[]
        ) as bulk_store:
            dispatch._dispatch_manage_relation(relations=relations, owner_id="test_agent")

        bulk_store.assert_called_once_with(
            relations=relations, owner_id="test_agent", invalidate=False
        )

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
        )
        dup_res = tools.store_memory(
            content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers",
            title="OAuth2 Authentication Core",
            tags=["#auth"],
        )
        self.assertEqual(dup_res["status"], "rejected")
        self.assertEqual(dup_res["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")

    def test_get_events_filters_by_context_and_agent(self):
        # Phase 6 (plan §5.7): log_event/get_events no longer have `mode` -- context_id/
        # agent_id/event_type/agent_session_id are all plain equality filters now.
        tools.log_event(
            event_type="attempt",
            content="Event mode test",
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
        )
        tags = tools.search_tags(query="data")
        self.assertEqual(tags["status"], "ok")
        self.assertIsInstance(tags["data"], list)

    def _tag_count_for_entity(self, entity_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM entity_tags WHERE entity_id = ?", (entity_id,)
        ).fetchone()[0]

    def test_store_memory_update_explicit_empty_tags_rejects_frozen_mutation(self):
        res = tools.store_memory(
            content="Content for explicit tag clearing test on update path",
            title="Tag Clearing Entity",
            tags=["#python"],
        )
        entity_id = res["data"]["id"]
        self.assertEqual(self._tag_count_for_entity(entity_id), 1)

        rejected = tools.store_memory(
            entity_id=entity_id,
            content="Content for explicit tag clearing test on update path",
            title="Tag Clearing Entity",
            tags=[],
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
        )
        entity_id = res["data"]["id"]

        rejected = tools.store_memory(
            entity_id=entity_id,
            content="Content for explicit tag replacement test on update path",
            title="Tag Replacement Entity",
            tags=["#beta"],
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

    def test_store_memory_omitted_memory_type_defaults_only_fresh_inserts_to_fact(self):
        """The MCP wrapper must forward omission as None so updates can preserve their type.

        Fresh inserts still become facts in the domain layer; putting ``"fact"`` on the wrapper
        signature would make omission indistinguishable from an explicit type change on updates.
        """
        import inspect

        memory_type_param = inspect.signature(tools.store_memory).parameters["memory_type"]
        self.assertIsNone(memory_type_param.default)

        inserted = tools.store_memory(
            content="Fresh MCP wrapper insert with an omitted memory type",
            title="Fresh Wrapper Fact Default",
            tags=["#memory-type"],
        )
        entity_id = inserted["data"]["id"]
        stored_type = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()[0]
        self.assertEqual(stored_type, "fact")

    def test_store_memory_metadata_is_explicit_parameter(self):
        """MCP wrapper regression: metadata must be an explicit named parameter so FastMCP's
        auto-generated JSON Schema declares it over the real MCP wire protocol."""
        import inspect

        params = inspect.signature(tools.store_memory).parameters
        self.assertIn("metadata", params)
        self.assertIs(params["metadata"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)

    def test_store_memory_entity_id_bypasses_exact_duplicate_on_metadata_only_update(self):
        """Behavioral counterpart to the schema test above: an explicit entity_id must let a
        metadata-only edit -- content byte-identical, only
        core_reason/core_exit_condition changing -- go through, matching store_memory's own
        docstring promise. Before the live incident this fixed, this exact call pattern (against
        the real MCP tool, not this direct Python call) returned REJECT_EXACT_DUPLICATE."""
        content = "Content for entity_id-targeted metadata-only update test, left unchanged."
        res = tools.store_memory(
            content=content,
            title="Metadata-Only Update Entity",
            tags=["#metadata"],
            memory_type="event",
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
            core_reason="C" * 25,
            core_exit_condition="D" * 25,
        )
        self.assertEqual(update_res["status"], "ok")
        stored_type = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()[0]
        self.assertEqual(stored_type, "event")

    def test_polymorphic_archive_memory(self):
        res1 = tools.store_memory(
            content="Archive test single node",
            title="Single Node",
            tags=["#archive"],
        )
        id1 = res1["data"]["id"]

        arch_res1 = tools.archive_memory(entity_id=id1)
        self.assertEqual(arch_res1["status"], "ok")
        self.assertIn("successfully archived", arch_res1["data"]["message"])

        res2 = tools.store_memory(
            content="Archive test bulk node 1",
            title="Bulk Node 1",
            tags=["#archive"],
        )
        res3 = tools.store_memory(
            content="Archive test bulk node 2",
            title="Bulk Node 2",
            tags=["#archive"],
        )
        id2 = res2["data"]["id"]
        id3 = res3["data"]["id"]

        # Test passing stringified list / actual list
        arch_res2 = tools.archive_memory(entity_id=[id2, id3])
        self.assertIsInstance(arch_res2, list)

    def test_polymorphic_manage_relation(self):
        res1 = tools.store_memory(
            content="Source entity for relation",
            title="Source Entity",
            tags=["#relation"],
        )
        res2 = tools.store_memory(
            content="Target entity for relation",
            title="Target Entity",
            tags=["#relation"],
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertEqual(rel_res["status"], "ok")
        self.assertIn("Relation successfully stored", rel_res["data"]["message"])

        # 'part_of' -- an agent-selectable, non-strong canonical predicate -- so this exercises
        # the bulk-shape plumbing itself, not the predicate-vocabulary gate (covered separately).
        bulk_rel_res = tools.manage_relation(
            relations=[{"source_id": id1, "target_id": id2, "predicate": "part_of"}],
        )
        self.assertIsInstance(bulk_rel_res, list)

    def test_bulk_manage_relation_invalidate_per_item(self):
        res1 = tools.store_memory(
            content="Source entity for bulk relation invalidation",
            title="Bulk Invalidate Source",
            tags=["#relation"],
        )
        res2 = tools.store_memory(
            content="Target entity for bulk relation invalidation",
            title="Bulk Invalidate Target",
            tags=["#relation"],
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="part_of")
        self.assertEqual(rel_res["status"], "ok")
        self.assertIn("Relation successfully stored", rel_res["data"]["message"])

        inv_res = tools.manage_relation(
            relations=[
                {
                    "source_id": id1,
                    "target_id": id2,
                    "predicate": "part_of",
                    "invalidate": True,
                }
            ]
        )
        self.assertIsInstance(inv_res, list)
        self.assertEqual(len(inv_res), 1)
        self.assertEqual(inv_res[0]["status"], "success")
        self.assertEqual(inv_res[0]["action"], "invalidate")

    def test_list_predicates_tool(self):
        results = tools.list_predicates(query="elaborates")["data"]
        names = {r["name"] for r in results}
        self.assertIn("elaborates_on", names)

    def test_list_predicates_respects_explicit_limit_kwarg(self):
        for i in range(10):
            self.conn.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"limit-test-pred-{i}", f"limit_test_predicate_{i}", f"limit_test_predicate_{i}"),
            )
        self.conn.commit()

        results = tools.list_predicates(limit=3)["data"]
        self.assertEqual(len(results), 3)

    def test_search_tags_respects_explicit_limit_kwarg(self):
        for i in range(10):
            self.conn.execute(
                "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"limit-test-tag-{i}", f"#limit_test_tag_{i}", f"limittesttag{i}"),
            )
        self.conn.commit()

        results = tools.search_tags(limit=3)["data"]
        self.assertEqual(len(results), 3)

    def test_list_predicates_explicit_zero_limit_is_respected_not_defaulted(self):
        for i in range(5):
            self.conn.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (f"zero-limit-pred-{i}", f"zero_limit_predicate_{i}", f"zero_limit_predicate_{i}"),
            )
        self.conn.commit()

        results = tools.list_predicates(limit=0)["data"]
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

        results = tools.search_tags(limit=0)["data"]
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
        )
        res2 = tools.store_memory(
            content="Target entity for predicate canonicalization test",
            title="Predicate Canon Target",
            tags=["#predicate"],
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="Depends-On")
        self.assertEqual(rel_res["status"], "ok")
        self.assertIn("Relation successfully stored", rel_res["data"]["message"])

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
        self.assertEqual(rel_res2["status"], "ok")
        self.assertIn("already exists", rel_res2["data"]["message"])

    def test_manage_relation_rejects_seeded_alias_with_resubmittable_corrected_call(self):
        # Phase 6 write-time gate (plan §5.8): manage_relation no longer silently substitutes a
        # drifted alias spelling -- it rejects the call with a corrected_call naming the
        # canonical replacement ('relates_to' now aliases 'related_to', the Phase 6 reversal).
        res1 = tools.store_memory(
            content="Source entity for seeded alias substitution test",
            title="Alias Substitution Source",
            tags=["#alias"],
        )
        res2 = tools.store_memory(
            content="Target entity for seeded alias substitution test",
            title="Alias Substitution Target",
            tags=["#alias"],
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="relates_to")
        self.assertEqual(rel_res["status"], "rejected")
        self.assertEqual(rel_res["errors"][0]["code"], "NONCANONICAL_PREDICATE")
        corrected_call = rel_res["corrected_call"]
        self.assertEqual(corrected_call["predicate"], "related_to")
        self.assertEqual(corrected_call["source_id"], id1)
        self.assertEqual(corrected_call["target_id"], id2)

        resubmit = tools.manage_relation(**corrected_call)
        self.assertEqual(resubmit["status"], "ok")
        self.assertIn("Relation successfully stored", resubmit["data"]["message"])

    def test_store_memory_memory_type_round_trip(self):
        res = tools.store_memory(
            content="Content for memory_type tool round trip test",
            title="Memory Type Tool Entity",
            tags=["#memory-type"],
            memory_type="preference",
        )
        self.assertEqual(res["status"], "ok")
        entity_id = res["data"]["id"]

        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], "preference")

        # Confirm it also round-trips through search_memory's echoed field.
        search_res = tools.search_memory(memory_type_filter="preference")
        ids = {r["id"] for r in search_res}
        self.assertIn(entity_id, ids)

    def test_search_memory_memory_type_filter_round_trip(self):
        tools.store_memory(
            content="Fact-typed content for the memory_type_filter tool test",
            title="Fact Typed Tool Entity",
            tags=["#memory-type"],
            memory_type="fact",
        )
        tools.store_memory(
            content="Event-typed content for the memory_type_filter tool test",
            title="Event Typed Tool Entity",
            tags=["#memory-type"],
            memory_type="event",
        )

        results = tools.search_memory(memory_type_filter="fact")
        self.assertTrue(len(results) > 0)
        for r in results:
            self.assertEqual(r["memory_type"], "fact")

    def test_graph_tools_are_split_by_entry_condition(self):
        res1 = tools.store_memory(
            content="Root entity node title",
            title="Root Entity",
            tags=["#graph"],
        )
        self.assertEqual(res1["status"], "ok")
        id1 = res1["data"]["id"]

        deps = tools.get_related_memories(entity_id=id1)
        self.assertIsInstance(deps, dict)

        lineage = tools.get_lineage(entity_id=id1)
        self.assertIsInstance(lineage, dict)

        memory = tools.get_memory(entity_id=id1)
        self.assertEqual(memory["status"], "ok")

    def test_graph_tools_honor_depth_limits(self):
        res1 = tools.store_memory(
            content="PIT MCP dependency source content",
            title="PIT MCP Source",
            tags=["#pit"],
        )
        id1 = res1["data"]["id"]
        res2 = tools.store_memory(
            content="PIT MCP dependency target content",
            title="PIT MCP Target",
            tags=["#pit"],
        )
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertEqual(rel_res["status"], "ok")
        self.assertIn("successfully stored", rel_res["data"]["message"])

        deps_now = tools.get_related_memories(entity_id=id1, max_depth=1)
        self.assertEqual(deps_now["data"]["total_related_found"], 1)

        # Lineage threading: consolidate two memories and confirm point_in_time excludes the
        # brand-new consolidated_from ancestry while an unrestricted (now) call includes it.
        res3 = tools.store_memory(
            content="PIT MCP lineage parent A content",
            title="PIT MCP Lineage A",
            tags=["#pit"],
        )
        a_id = res3["data"]["id"]
        res4 = tools.store_memory(
            content="PIT MCP lineage parent B content",
            title="PIT MCP Lineage B",
            tags=["#pit"],
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
        )
        self.assertEqual(cons_res["status"], "ok")
        c_id = cons_res["data"]["entity_id"]

        lineage_now = tools.get_lineage(entity_id=c_id)
        self.assertEqual(lineage_now["data"]["total"], 2)

    def test_inspect_memory_returns_snippet_not_content_with_lineage(self):
        content = (
            "# Inspect Snippet Heading\n\n"
            "First body line here.\n"
            "Second body line here.\n"
            "Third body line here.\n"
            "Fourth unique body line excluded from the snippet."
        )
        stored = tools.store_memory(
            content=content,
            title="Inspect Snippet Memory",
            tags=["#inspect"],
        )
        self.assertEqual(stored["status"], "ok")

        result = tools.inspect_memory(entity_id=stored["data"]["id"])

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("content", result["data"])
        self.assertIn("snippet", result["data"])
        self.assertIsInstance(result["data"]["snippet"], str)
        self.assertTrue(result["data"]["snippet"])
        self.assertNotIn("Fourth unique body line excluded", result["data"]["snippet"])
        self.assertIn("lineage", result["data"])

    def test_inspect_memory_field_parity_with_get_memory_minus_content(self):
        stored = tools.store_memory(
            content=(
                "# Field Parity Heading\n\n"
                "First parity body line.\n"
                "Second parity body line.\n"
                "Third parity body line.\n"
                "Fourth parity body line."
            ),
            title="Inspect Field Parity Memory",
            tags=["#inspect"],
        )
        self.assertEqual(stored["status"], "ok")
        entity_id = stored["data"]["id"]

        get_result = tools.get_memory(entity_id=entity_id)
        inspect_result = tools.inspect_memory(entity_id=entity_id)

        self.assertEqual(
            set(get_result["data"].keys()) - {"content"},
            set(inspect_result["data"].keys()) - {"snippet"},
        )

    def test_inspect_memory_unknown_entity_id_rejected(self):
        result = tools.inspect_memory(entity_id="totally-fake-id-000")

        self.assertEqual(result["status"], "rejected")

    def test_inspect_memory_ambiguous_prefix_returns_candidates_never_content(self):
        # No tags on these two: entity_tags rows carry a foreign key to entities.id, which
        # would block the raw `UPDATE entities SET id = ...` below used to force the collision
        # (mirrors test_entity_id_prefix_resolution.py's own tag-free collision fixture).
        stored_a = tools.store_memory(
            content="Secret content A that must not leak via an ambiguous prefix.",
            title="Inspect Collision Entity A",
            tags=[],
        )
        id_a = stored_a["data"]["id"]
        shared_prefix = id_a[:8]
        stored_b = tools.store_memory(
            content="Secret content B that must not leak via an ambiguous prefix.",
            title="Inspect Collision Entity B",
            tags=[],
        )
        id_b = stored_b["data"]["id"]
        forced_id_b = shared_prefix + id_b[8:]
        self.conn.execute("UPDATE entities SET id = ? WHERE id = ?", (forced_id_b, id_b))
        self.conn.commit()

        result = tools.inspect_memory(entity_id=shared_prefix)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "AMBIGUOUS_ID_PREFIX")
        candidates = result["errors"][0]["candidates"]
        candidate_ids = {c["id"] for c in candidates}
        self.assertEqual(candidate_ids, {id_a, forced_id_b})
        self.assertNotIn("snippet", result)
        self.assertNotIn("Secret content A", str(result))
        self.assertNotIn("Secret content B", str(result))

    def test_get_related_memories_include_inspect_false_is_unchanged_by_default(self):
        stored_a = tools.store_memory(
            content="Default related-memory source content for inspect regression testing.",
            title="Default Related Source",
            tags=["#related"],
        )
        stored_b = tools.store_memory(
            content="Default related-memory target content for inspect regression testing.",
            title="Default Related Target",
            tags=["#related"],
        )
        a_id = stored_a["data"]["id"]
        b_id = stored_b["data"]["id"]

        relation = tools.manage_relation(
            source_id=a_id,
            target_id=b_id,
            predicate="related_to",
        )
        self.assertEqual(relation["status"], "ok")
        self.assertIn("Relation successfully stored", relation["data"]["message"])

        result = tools.get_related_memories(entity_id=a_id, direction="outbound")["data"]

        self.assertTrue(result["related_memories"])
        for item in result["related_memories"]:
            self.assertEqual(set(item.keys()), {"id", "title", "depth"})

    def test_get_related_memories_include_inspect_true_embeds_fields_without_lineage(self):
        stored_a = tools.store_memory(
            content="Embedded inspect source content for related-memory regression testing.",
            title="Embedded Inspect Source",
            tags=["#related"],
        )
        stored_b = tools.store_memory(
            content="Embedded inspect target content for related-memory regression testing.",
            title="Embedded Inspect Target",
            tags=["#related"],
        )
        a_id = stored_a["data"]["id"]
        b_id = stored_b["data"]["id"]

        relation = tools.manage_relation(
            source_id=a_id,
            target_id=b_id,
            predicate="related_to",
        )
        self.assertEqual(relation["status"], "ok")
        self.assertIn("Relation successfully stored", relation["data"]["message"])

        result = tools.get_related_memories(
            entity_id=a_id,
            direction="outbound",
            include_inspect=True,
        )["data"]
        target = next(item for item in result["related_memories"] if item["id"] == b_id)

        for field in ("snippet", "status", "tags", "metadata"):
            self.assertIn(field, target)
        self.assertNotIn("lineage", target)
        self.assertNotIn("content", target)

    def test_get_related_memories_include_inspect_does_not_bump_last_accessed_at(self):
        stored_a = tools.store_memory(
            content="Access-time source content for related-memory regression testing.",
            title="Access-Time Source",
            tags=["#related"],
        )
        stored_b = tools.store_memory(
            content="Access-time target content for related-memory regression testing.",
            title="Access-Time Target",
            tags=["#related"],
        )
        a_id = stored_a["data"]["id"]
        b_id = stored_b["data"]["id"]

        relation = tools.manage_relation(
            source_id=a_id,
            target_id=b_id,
            predicate="related_to",
        )
        self.assertEqual(relation["status"], "ok")
        self.assertIn("Relation successfully stored", relation["data"]["message"])

        before = self.conn.execute(
            "SELECT last_accessed_at FROM entities WHERE id = ?", (b_id,)
        ).fetchone()[0]
        result = tools.get_related_memories(
            entity_id=a_id,
            direction="outbound",
            include_inspect=True,
        )["data"]
        after = self.conn.execute(
            "SELECT last_accessed_at FROM entities WHERE id = ?", (b_id,)
        ).fetchone()[0]

        self.assertTrue(any(item["id"] == b_id for item in result["related_memories"]))
        self.assertEqual(after, before)

    def test_manage_relation_invalidate_mode(self):
        res1 = tools.store_memory(
            content="Source entity for relation invalidation test",
            title="Invalidate MCP Source",
            tags=["#relation"],
        )
        res2 = tools.store_memory(
            content="Target entity for relation invalidation test",
            title="Invalidate MCP Target",
            tags=["#relation"],
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        rel_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="depends_on")
        self.assertEqual(rel_res["status"], "ok")
        self.assertIn("Relation successfully stored", rel_res["data"]["message"])
        rel_id = rel_res["data"]["relation_id"]

        inv_res = tools.manage_relation(
            source_id=id1, target_id=id2, predicate="depends_on", invalidate=True
        )
        self.assertEqual(inv_res["status"], "ok")
        self.assertIn("Relation invalidated", inv_res["data"]["message"])

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
        )
        res2 = tools.store_memory(
            content="Target entity for valid_at passthrough",
            title="ValidAt Target",
            tags=["#relation"],
        )
        id1 = res1["data"]["id"]
        id2 = res2["data"]["id"]

        custom_valid_at = "2025-02-01T00:00:00+00:00"
        rel_res = tools.manage_relation(
            source_id=id1,
            target_id=id2,
            predicate="depends_on",
            valid_at=custom_valid_at,
        )
        self.assertEqual(rel_res["status"], "ok")
        self.assertIn("Relation successfully stored", rel_res["data"]["message"])
        rel_id = rel_res["data"]["relation_id"]

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
        self.assertEqual(inv_res["status"], "ok")
        self.assertIn("Relation invalidated", inv_res["data"]["message"])

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
                    "content": bulk_content + "\n- Owner-default item",
                    "override_justification": (
                        "deliberately merging unrelated bulk fixtures via the MCP tool wrapper"
                    ),
                },
                {
                    "parent_ids": [e, f],
                    "title": "Bulk Override Item EF",
                    "content": bulk_content + "\n- Owner-override item",
                },
            ],
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

    def test_commit_consolidation_uses_configured_owner_and_context_id(self):
        """Bulk consolidation always uses the startup owner; per-item owner values cannot win.

        ``context_id`` remains a legitimate batch/item field, so this also guards that the owner
        migration does not accidentally remove unrelated bulk metadata.
        """
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
                    "content": bulk_content + "\n- Owner-default item",
                    # no context_id here -> must inherit the top-level batch default
                },
                {
                    "parent_ids": [c, d],
                    "title": "Bulk Owner Override Item CD",
                    "content": bulk_content + "\n- Owner-override item",
                    # A stale per-item owner must be ignored by the adapter boundary.
                    "owner_id": "agent_override",
                    "context_id": "ctx_override",
                },
            ],
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
        self.assertEqual(row_ab, ("test_agent", "ctx_batch_default"))
        self.assertEqual(row_cd, ("test_agent", "ctx_override"))

    def test_manage_relation_tool_uses_configured_owner_for_override_audit(self):
        """The relation override audit is attributed to the startup owner."""
        dim = 384

        def _axis(i):
            v = [0.0] * dim
            v[i] = 1.0
            return v

        a = self._mk_vector_entity("Relation Gate Tool A", _axis(0))
        b = self._mk_vector_entity("Relation Gate Tool B", _axis(1))  # orthogonal -> low similarity

        res_no_override = tools.manage_relation(source_id=a, target_id=b, predicate="elaborates_on")
        self.assertEqual(res_no_override["status"], "rejected", res_no_override)
        self.assertIn("REJECT_LOW_RELATION_SIMILARITY", res_no_override["errors"][0]["message"])

        res_with_override = tools.manage_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            override_justification="deliberately forcing a low-similarity relation via the MCP tool wrapper",
        )
        self.assertEqual(res_with_override["status"], "ok")
        self.assertIn("Relation successfully stored", res_with_override["data"]["message"])

        event = self.conn.execute(
            "SELECT agent_id, content FROM events WHERE type = 'relation_gate_override'"
        ).fetchone()
        self.assertEqual(event[0], "test_agent")
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
            )

        page1 = tools.get_events(agent_id="test_agent", limit=2, offset=0)
        self.assertEqual(len(page1), 2)

        page2 = tools.get_events(agent_id="test_agent", limit=2, offset=2)
        self.assertEqual(len(page2), 2)

        self.assertNotEqual(page1[0]["id"], page2[0]["id"])

    def test_mcp_tool_count_regression_guard(self):
        registered_count = len(tools.mcp._tool_manager._tools)
        self.assertEqual(
            registered_count,
            19,
            f"MCP server tool count must be exactly 19 after retrieve_context was added "
            f"(Milestone A slice A5) on top of the metadata update and inspect_memory tools "
            f"(ephemeral_memory and "
            f"export_corpus_snapshot removed, following Phase 6's dismiss_event removal), "
            f"got {registered_count}",
        )

    def test_public_descriptions_do_not_describe_tool_call_owner_binding(self):
        """The retired first-call owner-binding behavior must not be advertised anywhere."""
        registered = tools.mcp._tool_manager._tools
        for name in registered:
            description = registered[name].description or ""
            self.assertNotIn("first call within a session", description)


class TestPrivateMemoryMCPAccess(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())
        SESSION_IDENTITY.reset()

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def _call_as(self, owner, tool_name, arguments):
        # Reset models a separate configured MCP adapter process for each owner.
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner(owner)
        result = await tools.mcp.call_tool(tool_name, arguments)
        content = result[0] if isinstance(result[0], list) else result
        return json.loads(content[0].text)

    async def test_get_memory_hides_another_owners_private_full_id(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Owner A private title",
                "content": "Owner A private body holds a distinct private fact for the cross-owner access test.",
                "scope": "private",
            },
        )
        shared = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Owner A shared title",
                "content": "Owner A shared body holds a distinct shared fact for the cross-owner access test.",
                "scope": "shared",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        self.assertEqual(shared["status"], "ok", shared)
        private_id = private["data"]["id"]
        shared_id = shared["data"]["id"]

        owner_a_private = await self._call_as("owner_a", "get_memory", {"entity_id": private_id})
        owner_b_private = await self._call_as("owner_b", "get_memory", {"entity_id": private_id})
        unknown = await self._call_as("owner_b", "get_memory", {"entity_id": "missing-id"})
        owner_a_shared = await self._call_as("owner_a", "get_memory", {"entity_id": shared_id})
        owner_b_shared = await self._call_as("owner_b", "get_memory", {"entity_id": shared_id})

        self.assertIn("Owner A private body", owner_a_private["data"]["content"])
        self.assertEqual(owner_b_private["status"], unknown["status"])
        self.assertEqual(owner_b_private["errors"][0]["code"], unknown["errors"][0]["code"])
        self.assertNotIn("Owner A private title", str(owner_b_private))
        self.assertIn("Owner A shared body", owner_a_shared["data"]["content"])
        self.assertIn("Owner A shared body", owner_b_shared["data"]["content"])

    async def test_get_memory_hides_another_owners_private_prefix(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private prefix target",
                "content": "A unique private prefix target for the cross-owner access test.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        prefix = private["data"]["id"][:8]
        result = await self._call_as("owner_b", "get_memory", {"entity_id": prefix})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "UNKNOWN_ENTITY_ID")
        self.assertNotIn(private["data"]["id"], str(result))
        self.assertNotIn("Private prefix target", str(result))

    async def test_get_memory_prefix_candidates_hide_private_match(self):
        shared = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Shared prefix match",
                "content": "A shared match for the ambiguous prefix privacy test.",
                "scope": "shared",
            },
        )
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private prefix match",
                "content": "A private match for the ambiguous prefix privacy test.",
                "scope": "private",
            },
        )
        self.assertEqual(shared["status"], "ok", shared)
        self.assertEqual(private["status"], "ok", private)
        shared_id, private_id = shared["data"]["id"], private["data"]["id"]
        colliding_private_id = shared_id[:8] + private_id[8:]
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute(
            "UPDATE entities SET id = ? WHERE id = ?", (colliding_private_id, private_id)
        )
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = ON")
        result = await self._call_as(
            "owner_b",
            "get_memory",
            {"entity_id": shared_id[:8]},
        )
        self.assertNotIn(colliding_private_id, str(result))
        self.assertNotIn("Private prefix match", str(result))

    async def test_inspect_memory_hides_another_owners_private_id(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private inspection target",
                "content": "Private inspection content must stay invisible to another owner.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        private_id = private["data"]["id"]
        result = await self._call_as("owner_b", "inspect_memory", {"entity_id": private_id})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "UNKNOWN_ENTITY_ID")
        self.assertNotIn("Private inspection target", str(result))

    async def test_get_memory_hides_private_lineage_of_shared_memory(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private lineage predecessor",
                "content": "Private predecessor body for the shared revision lineage case.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        revised = await self._call_as(
            "owner_a",
            "revise_memory",
            {
                "entity_id": private["data"]["id"],
                "title": "Shared lineage successor",
                "content": "Shared successor body for the revised lineage case.",
                "reason": "Publish corrected shared fact",
                "scope": "shared",
            },
        )
        self.assertEqual(revised["status"], "ok", revised)
        shared_id = revised["data"]["new_id"]
        result = await self._call_as("owner_b", "get_memory", {"entity_id": shared_id})
        self.assertEqual(result["status"], "ok", result)
        self.assertNotIn(private["data"]["id"], str(result))
        self.assertNotIn("Private lineage predecessor", str(result))

    async def test_get_lineage_hides_private_predecessor_of_shared_memory(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private lineage graph node",
                "content": "A private predecessor for the public lineage graph test.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        revised = await self._call_as(
            "owner_a",
            "revise_memory",
            {
                "entity_id": private["data"]["id"],
                "title": "Shared lineage graph root",
                "content": "A shared successor for the public lineage graph test.",
                "reason": "Publish shared revision",
                "scope": "shared",
            },
        )
        self.assertEqual(revised["status"], "ok", revised)
        result = await self._call_as(
            "owner_b",
            "get_lineage",
            {"entity_id": revised["data"]["new_id"], "direction": "ancestors"},
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertNotIn(private["data"]["id"], str(result))
        self.assertNotIn("Private lineage graph node", str(result))

    async def test_get_related_memories_hides_private_neighbor(self):
        shared = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Shared relation root",
                "content": "A shared root for the public relation traversal test.",
                "scope": "shared",
            },
        )
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private relation neighbor",
                "content": "A private neighbor for the public relation traversal test.",
                "scope": "private",
            },
        )
        self.assertEqual(shared["status"], "ok", shared)
        self.assertEqual(private["status"], "ok", private)
        relation = await self._call_as(
            "owner_a",
            "manage_relation",
            {
                "source_id": shared["data"]["id"],
                "target_id": private["data"]["id"],
                "predicate": "related_to",
            },
        )
        self.assertEqual(relation["status"], "ok", relation)
        result = await self._call_as(
            "owner_b",
            "get_related_memories",
            {"entity_id": shared["data"]["id"]},
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertNotIn(private["data"]["id"], str(result))
        self.assertNotIn("Private relation neighbor", str(result))

    async def test_get_related_memories_resolves_own_title_despite_other_owners_newer_collision(
        self,
    ):
        # owner_b's private memory is stored first, so owner_a's same-titled memory below has a
        # strictly later updated_at -- the unfiltered title lookup that resolve_entity_id() alone
        # performs would pick owner_a's row, which must not shadow owner_b's own valid title match.
        owned = await self._call_as(
            "owner_b",
            "store_memory",
            {
                "title": "Duplicate Title Collision",
                "content": "Owner B's own memory, must resolve for owner_b by title.",
                "scope": "private",
            },
        )
        self.assertEqual(owned["status"], "ok", owned)
        colliding = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Duplicate Title Collision",
                "content": "Owner A's private memory, must never resolve for owner_b.",
                "scope": "private",
            },
        )
        self.assertEqual(colliding["status"], "ok", colliding)
        result = await self._call_as(
            "owner_b",
            "get_related_memories",
            {"entity_id": "Duplicate Title Collision"},
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertNotIn(colliding["data"]["id"], str(result))
        self.assertNotIn("Owner A's private memory", str(result))

    async def test_get_lineage_resolves_own_title_despite_other_owners_newer_collision(self):
        owned = await self._call_as(
            "owner_b",
            "store_memory",
            {
                "title": "Duplicate Lineage Title Collision",
                "content": "Owner B's own memory, must resolve for owner_b by title.",
                "scope": "private",
            },
        )
        self.assertEqual(owned["status"], "ok", owned)
        colliding = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Duplicate Lineage Title Collision",
                "content": "Owner A's private memory, must never resolve for owner_b.",
                "scope": "private",
            },
        )
        self.assertEqual(colliding["status"], "ok", colliding)
        result = await self._call_as(
            "owner_b",
            "get_lineage",
            {"entity_id": "Duplicate Lineage Title Collision", "direction": "ancestors"},
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertNotIn(colliding["data"]["id"], str(result))
        self.assertNotIn("Owner A's private memory", str(result))

    async def test_retrieve_context_local_rejects_private_anchor(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private local anchor",
                "content": "A private anchor for the public local context retrieval test.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        result = await self._call_as(
            "owner_b",
            "retrieve_context",
            {"entity_ids": [private["data"]["id"]]},
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "UNKNOWN_ENTITY_ID")
        self.assertNotIn("Private local anchor", str(result))

    async def test_retrieve_context_local_hides_private_neighbor(self):
        shared = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Shared local anchor",
                "content": "A shared anchor for the local context expansion privacy test.",
                "scope": "shared",
            },
        )
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private local neighbor",
                "content": "A private neighbor for the local context expansion privacy test.",
                "scope": "private",
            },
        )
        self.assertEqual(shared["status"], "ok", shared)
        self.assertEqual(private["status"], "ok", private)
        relation = await self._call_as(
            "owner_a",
            "manage_relation",
            {
                "source_id": shared["data"]["id"],
                "target_id": private["data"]["id"],
                "predicate": "depends_on",
            },
        )
        self.assertEqual(relation["status"], "ok", relation)
        result = await self._call_as(
            "owner_b",
            "retrieve_context",
            {"entity_ids": [shared["data"]["id"]]},
        )
        self.assertNotIn(private["data"]["id"], str(result))
        self.assertNotIn("Private local neighbor", str(result))

    async def test_retrieve_context_local_hides_private_lineage(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private local predecessor",
                "content": "A private predecessor for the local context lineage privacy test.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        revised = await self._call_as(
            "owner_a",
            "revise_memory",
            {
                "entity_id": private["data"]["id"],
                "title": "Shared local successor",
                "content": "A shared successor for the local context lineage privacy test.",
                "reason": "Publish shared revision",
                "scope": "shared",
            },
        )
        self.assertEqual(revised["status"], "ok", revised)
        result = await self._call_as(
            "owner_b",
            "retrieve_context",
            {"entity_ids": [revised["data"]["new_id"]]},
        )
        self.assertNotIn(private["data"]["id"], str(result))
        self.assertNotIn("Private local predecessor", str(result))

    async def test_retrieve_context_local_hides_private_conflict(self):
        shared = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Shared conflict anchor",
                "content": "A shared anchor for the local conflict privacy test.",
                "scope": "shared",
            },
        )
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private conflicting memory",
                "content": "A private contradiction for the local conflict privacy test.",
                "scope": "private",
            },
        )
        self.assertEqual(shared["status"], "ok", shared)
        self.assertEqual(private["status"], "ok", private)
        relation = await self._call_as(
            "owner_a",
            "manage_relation",
            {
                "source_id": shared["data"]["id"],
                "target_id": private["data"]["id"],
                "predicate": "contradicts",
            },
        )
        self.assertEqual(relation["status"], "ok", relation)
        result = await self._call_as(
            "owner_b",
            "retrieve_context",
            {"entity_ids": [shared["data"]["id"]]},
        )
        self.assertNotIn(private["data"]["id"], str(result))
        self.assertNotIn("Private conflicting memory", str(result))

    async def test_retrieve_context_global_hides_private_community_member(self):
        import sqlite_vec

        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private global community member",
                "content": "A private member for the global community retrieval privacy test.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        private_id = private["data"]["id"]
        community_id = str(uuid.uuid4())
        embedding = sqlite_vec.serialize_float32([1.0] + [0.0] * 383)
        self.conn.execute(
            "INSERT INTO communities (id, representative_entity_id, member_count, level, created_at) VALUES (?, ?, 1, 0, ?)",
            (community_id, private_id, "2026-09-22T00:00:00+00:00"),
        )
        self.conn.execute(
            "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, 0)",
            (private_id, community_id),
        )
        self.conn.execute(
            "INSERT INTO community_embeddings (community_id, embedding) VALUES (?, ?)",
            (community_id, embedding),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (private_id, embedding),
        )
        self.conn.commit()
        result = await self._call_as(
            "owner_b",
            "retrieve_context",
            {"strategy": "global", "query": "private global community member"},
        )
        self.assertNotIn(private_id, str(result))
        self.assertNotIn("Private global community member", str(result))

    async def test_retrieve_context_local_hides_private_orphan_community_match(self):
        import sqlite_vec

        shared = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Shared orphan anchor",
                "content": "A shared orphan anchor for the local community privacy test.",
                "scope": "shared",
            },
        )
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private orphan community member",
                "content": "A private community member for the local orphan privacy test.",
                "scope": "private",
            },
        )
        self.assertEqual(shared["status"], "ok", shared)
        self.assertEqual(private["status"], "ok", private)
        shared_id, private_id = shared["data"]["id"], private["data"]["id"]
        community_id = str(uuid.uuid4())
        embedding = sqlite_vec.serialize_float32([1.0] + [0.0] * 383)
        self.conn.execute(
            "INSERT INTO communities (id, representative_entity_id, member_count, level, created_at) VALUES (?, ?, 1, 0, ?)",
            (community_id, private_id, "2026-09-22T00:00:00+00:00"),
        )
        self.conn.execute(
            "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, 0)",
            (private_id, community_id),
        )
        self.conn.execute(
            "INSERT INTO community_embeddings (community_id, embedding) VALUES (?, ?)",
            (community_id, embedding),
        )
        for entity_id in (shared_id, private_id):
            self.conn.execute(
                "INSERT OR REPLACE INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                (entity_id, embedding),
            )
        self.conn.commit()
        result = await self._call_as(
            "owner_b",
            "retrieve_context",
            {"entity_ids": [shared_id]},
        )
        self.assertNotIn(private_id, str(result))
        self.assertNotIn("Private orphan community member", str(result))

    async def test_update_memory_metadata_rejects_other_owners_private_id(self):
        private = await self._call_as(
            "owner_a",
            "store_memory",
            {
                "title": "Private metadata target",
                "content": "A private target for the cross-owner metadata update test.",
                "scope": "private",
            },
        )
        self.assertEqual(private["status"], "ok", private)
        private_id = private["data"]["id"]
        result = await self._call_as(
            "owner_b",
            "update_memory_metadata",
            {"entity_id": private_id, "metadata": {"intruder": True}},
        )
        owner_a_view = await self._call_as(
            "owner_a",
            "get_memory",
            {"entity_id": private_id},
        )
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("intruder", owner_a_view["data"]["metadata"])


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
        SESSION_IDENTITY.configure_owner("agent_two")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_singular_consolidate_survives_real_call_tool_output_validation(self):
        a = tools.store_memory(
            content="Parent A content for the real call_tool output-schema regression probe",
            title="Output Schema Probe Parent A",
            tags=["#probe"],
        )
        b = tools.store_memory(
            content="Parent B content, closely related, for the same output-schema probe",
            title="Output Schema Probe Parent B",
            tags=["#probe"],
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
        SESSION_IDENTITY.configure_owner("test_agent")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store_core(self, title):
        res = tools.store_memory(
            content=f"Distinct fixture content body for {title}, not a near-duplicate.",
            title=title,
            tags=["#core-test"],
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
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("retained as core", result["data"]["message"])

    def test_demote_via_mcp_tool(self):
        entity_id = self._store_core("MCP Demote Core")
        result = tools.review_core_memory(
            entity_id=entity_id,
            outcome="demote",
            review_rationale="C" * 20,
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("demoted", result["data"]["message"])
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
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("archived", result["data"]["message"])

    def test_missing_required_fields_rejected(self):
        # review_core_memory's validation layer stops raising and starts returning per this
        # spec (§12.1) -- a missing review_rationale is now a rejected envelope, matching every
        # other validation failure this function reports, not a raised exception.
        entity_id = self._store_core("MCP Missing Fields Core")
        result = tools.review_core_memory(entity_id=entity_id, outcome="retain")
        self.assertEqual(result["status"], "rejected")

    def test_get_core_bootstrap_digest_dispatch_entry(self):
        """Not a public MCP tool -- exercised directly through the daemon dispatch table, the
        same internal read path saltmdb-cli's bootstrap-digest command calls in production."""
        from saltmdb.daemon import dispatch

        self._store_core("Dispatch Digest Core")
        digest = dispatch.DISPATCH_TABLE["get_core_bootstrap_digest"]()
        self.assertIn("<saltmdb-digest>", digest)
        self.assertIn("Dispatch Digest Core", digest)

    def test_get_last_session_digest_dispatch_entry(self):
        """Not a public MCP tool -- exercised directly through the daemon dispatch table.
        Tests the dispatch function returns the expected digest string for a seeded scenario."""
        from saltmdb.daemon import dispatch
        from saltmdb.db import agent_sessions
        import datetime

        # Record a prior session
        cwd = "/test/project"
        session_id = "prior-session-123"
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        # Create a memory in that session directly -- store_memory auto-stamps
        # agent_session_id from SESSION_IDENTITY and has no parameter to override it, so a
        # specific known session_id must be seeded via a direct insert like this.
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id,
            scope, status, title, memory_type, full_content, valid_from, agent_session_id)
            VALUES (?, ?, ?, ?, 'tester', 'shared', 'raw', ?, 'fact', 'body', ?, ?)""",
            ("prior-session-memory-id", now, now, now, "Prior Session Memory", now, session_id),
        )
        self.conn.commit()

        # Call the dispatch function
        digest = dispatch.DISPATCH_TABLE["get_last_session_digest"](cwd=cwd)
        self.assertIn("<saltmdb-last-session-digest", digest)
        self.assertIn(session_id, digest)
        self.assertIn("Prior Session Memory", digest)

    def test_get_last_session_digest_no_prior_session_returns_empty_envelope(self):
        """get_last_session_digest returns empty envelope when no prior session exists."""
        from saltmdb.daemon import dispatch

        # No prior session recorded
        digest = dispatch.DISPATCH_TABLE["get_last_session_digest"](cwd="/unknown/path")
        self.assertEqual(digest, "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>")


class TestUpdateMemoryMetadataTool(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store_memory(self, metadata=None):
        result = tools.store_memory(
            title="Metadata Update Memory",
            content="Sufficiently long content body for the quality gate to accept without issue here.",
            tags=["#metadata-test"],
            metadata=metadata,
        )
        self.assertEqual(result["status"], "ok")
        return result

    def _store_core(self, title):
        result = tools.store_memory(
            content=f"Distinct fixture content body for {title}, not a near-duplicate.",
            title=title,
            tags=["#core-test"],
            is_core=True,
            core_reason="A" * 20,
            core_exit_condition="B" * 20,
            metadata={"note": "initial"},
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def test_shallow_merge_overwrites_and_preserves_keys(self):
        stored = self._store_memory(metadata={"a": 1, "b": 2})
        entity_id = stored["data"]["id"]

        result = tools.update_memory_metadata(entity_id=entity_id, metadata={"b": 99, "c": 3})
        self.assertEqual(result["status"], "ok")
        self.assertIn("updated", result["data"]["message"])

        fetched = tools.get_memory(entity_id=entity_id)
        self.assertEqual(
            fetched["data"]["metadata"],
            {"a": 1, "b": 99, "c": 3},
        )

    def test_empty_patch_is_a_noop_and_reports_unchanged(self):
        stored = self._store_memory(metadata={"a": 1, "b": 2})
        entity_id = stored["data"]["id"]

        result = tools.update_memory_metadata(entity_id=entity_id, metadata={})
        self.assertEqual(result["status"], "ok")
        self.assertIn("unchanged", result["data"]["message"])

        fetched = tools.get_memory(entity_id=entity_id)
        self.assertEqual(fetched["data"]["metadata"], {"a": 1, "b": 2})

    def test_nonexistent_entity_id_returns_error_string(self):
        result = tools.update_memory_metadata(
            entity_id="definitely-not-a-real-id-00000", metadata={"x": 1}
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "NOT_FOUND")

    def test_non_dict_metadata_returns_error_string(self):
        stored = self._store_memory()
        entity_id = stored["data"]["id"]

        result = tools.update_memory_metadata(entity_id=entity_id, metadata="not-a-dict")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")

    def test_ambiguous_prefix_returns_error_string_naming_both_candidates(self):
        # No tags on these two: entity_tags rows carry a foreign key to entities.id, which
        # would block the raw `UPDATE entities SET id = ...` below used to force the collision
        # (mirrors test_entity_id_prefix_resolution.py's own tag-free collision fixture).
        stored_a = tools.store_memory(
            title="Metadata Update Collision Entity A",
            content="Distinct content body for collision entity A, not a near-duplicate of B.",
            tags=[],
            metadata={"a": 1},
        )
        self.assertEqual(stored_a["status"], "ok")
        id_a = stored_a["data"]["id"]
        shared_prefix = id_a[:8]
        stored_b = tools.store_memory(
            title="Metadata Update Collision Entity B",
            content="Distinct content body for collision entity B, not a near-duplicate of A.",
            tags=[],
            metadata={"b": 2},
        )
        self.assertEqual(stored_b["status"], "ok")
        id_b = stored_b["data"]["id"]
        forced_id_b = shared_prefix + id_b[8:]
        self.conn.execute("UPDATE entities SET id = ? WHERE id = ?", (forced_id_b, id_b))
        self.conn.commit()

        result = tools.update_memory_metadata(entity_id=shared_prefix, metadata={"x": 1})

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "AMBIGUOUS_ID_PREFIX")
        self.assertIn("multiple memories", result["errors"][0]["message"])

        # No partial/unintended write happened to either candidate.
        meta_a = self.conn.execute(
            "SELECT metadata FROM entities WHERE id = ?", (id_a,)
        ).fetchone()[0]
        meta_b = self.conn.execute(
            "SELECT metadata FROM entities WHERE id = ?", (forced_id_b,)
        ).fetchone()[0]
        self.assertEqual(json.loads(meta_a), {"a": 1})
        self.assertEqual(json.loads(meta_b), {"b": 2})

    def test_works_on_core_memory_without_touching_core_governance_fields(self):
        entity_id = self._store_core("Metadata Update Core")
        before = self.conn.execute(
            "SELECT is_core, core_reason, core_exit_condition, core_review_after "
            "FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()

        result = tools.update_memory_metadata(
            entity_id=entity_id, metadata={"note": "changed", "extra": True}
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("updated", result["data"]["message"])

        after = self.conn.execute(
            "SELECT is_core, core_reason, core_exit_condition, core_review_after "
            "FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(after, before)

        fetched = tools.get_memory(entity_id=entity_id)
        self.assertEqual(
            fetched["data"]["metadata"],
            {"note": "changed", "extra": True},
        )

    def test_store_memory_entity_id_metadata_path_still_works_unchanged(self):
        title = "Legacy Metadata Path Memory"
        content = (
            "Sufficiently long content body for the quality gate to accept without issue here."
        )
        tags = ["#metadata-test"]
        stored = tools.store_memory(title=title, content=content, tags=tags)
        self.assertEqual(stored["status"], "ok")

        result = tools.store_memory(
            entity_id=stored["data"]["id"],
            title=title,
            content=content,
            tags=tags,
            metadata={"z": 42},
        )
        self.assertEqual(result["status"], "ok")


class TestStrictIsCoreAtAdapterBoundary(unittest.TestCase):
    """Core-memory governance resolved gap #6: the MCP adapter layer must reject ambiguous
    is_core values outright rather than silently coercing them to False."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ambiguous_is_core_string_rejected_not_coerced(self):
        result = tools.store_memory(
            content="Content long enough to clear the quality gate minimum length.",
            title="Ambiguous Is Core Value",
            tags=["#core-test"],
            is_core="yes",
        )
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")

    def test_true_still_creates_a_core_with_lifecycle_fields(self):
        result = tools.store_memory(
            content="Content long enough to clear the quality gate minimum length.",
            title="Explicit Boolean True",
            tags=["#core-test"],
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
        SESSION_IDENTITY.configure_owner("test_agent")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk_pair(self, label):
        res1 = tools.store_memory(
            content=f"Source entity content for {label} test",
            title=f"{label} Source",
            tags=["#gate"],
        )
        res2 = tools.store_memory(
            content=f"Target entity content for {label} test",
            title=f"{label} Target",
            tags=["#gate"],
        )
        return res1["data"]["id"], res2["data"]["id"]

    def test_selectable_predicate_succeeds(self):
        id1, id2 = self._mk_pair("selectable")
        res = tools.manage_relation(source_id=id1, target_id=id2, predicate="part_of")
        self.assertEqual(res["status"], "ok")
        self.assertIn("Relation successfully stored", res["data"]["message"])

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
            res = tools.manage_relation(source_id=id1, target_id=id2, predicate=predicate)
            self.assertEqual(res["status"], "rejected", predicate)
            self.assertEqual(res["errors"][0]["code"], "RESERVED_PREDICATE", predicate)
            self.assertIn(lifecycle_tool, res["errors"][0]["message"], predicate)
            self.assertNotIn("corrected_call", res, predicate)

    def test_similar_to_is_refused_with_no_corrected_call(self):
        id1, id2 = self._mk_pair("similar_to")
        res = tools.manage_relation(source_id=id1, target_id=id2, predicate="similar_to")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "LEGACY_READONLY_PREDICATE")
        self.assertNotIn("corrected_call", res)

    def test_same_direction_alias_is_refused_and_corrected_call_resubmits_successfully(self):
        id1, id2 = self._mk_pair("same_direction_alias")
        res = tools.manage_relation(source_id=id1, target_id=id2, predicate="relates_to")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "NONCANONICAL_PREDICATE")
        corrected_call = res["corrected_call"]
        self.assertEqual(corrected_call["predicate"], "related_to")
        self.assertEqual(corrected_call["source_id"], id1)
        self.assertEqual(corrected_call["target_id"], id2)

        resubmit = tools.manage_relation(**corrected_call)
        self.assertEqual(resubmit["status"], "ok")
        self.assertIn("Relation successfully stored", resubmit["data"]["message"])

    def test_swap_alias_is_refused_and_corrected_call_swaps_ids_and_resubmits_successfully(self):
        # 'affects' -> 'caused_by' with source_id/target_id swapped (A affects B -> B caused_by
        # A). caused_by is not a RELATION_GATE_STRONG_PREDICATE, so this exercises the swap
        # mechanics in isolation from the embedding-similarity gate.
        id1, id2 = self._mk_pair("swap_alias")
        res = tools.manage_relation(source_id=id1, target_id=id2, predicate="affects")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "NONCANONICAL_PREDICATE")
        corrected_call = res["corrected_call"]
        self.assertEqual(corrected_call["predicate"], "caused_by")
        self.assertEqual(corrected_call["source_id"], id2)
        self.assertEqual(corrected_call["target_id"], id1)

        resubmit = tools.manage_relation(**corrected_call)
        self.assertEqual(resubmit["status"], "ok")
        self.assertIn("Relation successfully stored", resubmit["data"]["message"])

        row = self.conn.execute(
            "SELECT source_id, target_id FROM relations WHERE predicate = 'caused_by'"
        ).fetchone()
        self.assertEqual(row, (id2, id1))

    def test_unknown_predicate_is_refused_listing_valid_predicates_with_no_corrected_call(self):
        id1, id2 = self._mk_pair("unknown")
        res = tools.manage_relation(source_id=id1, target_id=id2, predicate="completely_made_up")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "UNKNOWN_PREDICATE")
        self.assertIn("depends_on", res["errors"][0]["message"])
        self.assertNotIn("corrected_call", res)

    def test_invalidate_true_bypasses_the_gate_entirely(self):
        # invalidate=True never runs the create-time gate (single-item shape) -- confirmed
        # intentional per manage_relation's own docstring: "This gate applies only to creating
        # a new edge, never to invalidate=True".
        id1, id2 = self._mk_pair("invalidate_bypass")
        create_res = tools.manage_relation(source_id=id1, target_id=id2, predicate="related_to")
        self.assertEqual(create_res["status"], "ok")
        self.assertIn("Relation successfully stored", create_res["data"]["message"])

        # (a) invalidating with the already-canonical predicate works, unsurprisingly.
        inv_res = tools.manage_relation(
            source_id=id1,
            target_id=id2,
            predicate="related_to",
            invalidate=True,
        )
        self.assertEqual(inv_res["status"], "ok")
        self.assertIn("Relation invalidated", inv_res["data"]["message"])

        # (b) invalidating with a predicate that WOULD be gated on create ('relates_to' is an
        # alias, refused by test_same_direction_alias_is_refused_... above) still succeeds when
        # invalidate=True, because invalidate_relation's read-side canonicalization still
        # resolves the alias to the same underlying edge.
        id3, id4 = self._mk_pair("invalidate_bypass_alias")
        tools.manage_relation(source_id=id3, target_id=id4, predicate="related_to")
        inv_res2 = tools.manage_relation(
            source_id=id3,
            target_id=id4,
            predicate="relates_to",
            invalidate=True,
        )
        self.assertEqual(
            inv_res2["status"],
            "ok",
            "invalidate=True with a would-be-gated alias predicate must reach "
            "invalidate_relation directly, not the rejected-envelope gate",
        )
        self.assertIn("Relation invalidated", inv_res2["data"]["message"])

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
    6 item 23): context_id/agent_id/event_type/agent_session_id equality filters, order, limit/offset,
    and the removed-kwarg regression guard."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_context_id_filters_correctly(self):
        tools.log_event(event_type="issue", content="ctx A event", context_id="ctx_A")
        tools.log_event(event_type="issue", content="ctx B event", context_id="ctx_B")

        events = tools.get_events(context_id="ctx_A")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["context_id"], "ctx_A")

    def test_agent_id_filters_correctly(self):
        tools.log_event(event_type="issue", content="agent one event")
        # Owner identity is startup configuration (immutable for the adapter process) -- reset
        # between calls attributed to a different agent, exactly like starting a new MCP process.
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("agent_two")
        tools.log_event(event_type="issue", content="agent two event")

        events = tools.get_events(agent_id="test_agent")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent_id"], "test_agent")

    def test_event_type_filters_correctly(self):
        tools.log_event(event_type="issue", content="an issue")
        tools.log_event(event_type="decision", content="a decision")

        events = tools.get_events(agent_id="test_agent", event_type="decision")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "decision")

    def test_agent_session_id_filters_correctly(self):
        session_id = SESSION_IDENTITY.agent_session_id
        from saltmdb.domain.services import event_service

        event_service.log_event(
            agent_id="agent_sess",
            content="session-bound event",
            agent_session_id=session_id,
            db_connection=self.conn,
        )

        events = tools.get_events(agent_session_id=session_id)
        self.assertTrue(len(events) >= 1)
        self.assertTrue(all(e["agent_session_id"] == session_id for e in events))

    def test_order_oldest_first_vs_newest_first_changes_result_order(self):
        tools.log_event(event_type="issue", content="first")
        tools.log_event(event_type="issue", content="second")
        tools.log_event(event_type="issue", content="third")

        newest_first = tools.get_events(agent_id="test_agent", order="newest_first")
        oldest_first = tools.get_events(agent_id="test_agent", order="oldest_first")

        self.assertEqual(len(newest_first), 3)
        self.assertEqual(len(oldest_first), 3)
        self.assertEqual(list(reversed(newest_first)), oldest_first)
        self.assertNotEqual([e["id"] for e in newest_first], [e["id"] for e in oldest_first])

    def test_limit_and_offset_paginate_correctly(self):
        for i in range(5):
            tools.log_event(event_type="issue", content=f"paged {i}")

        page1 = tools.get_events(agent_id="test_agent", order="oldest_first", limit=2, offset=0)
        page2 = tools.get_events(agent_id="test_agent", order="oldest_first", limit=2, offset=2)
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

    def test_full_content_flag_returns_untruncated_content(self):
        long_content = "x" * 1500
        tools.log_event(event_type="issue", content=long_content)

        truncated = tools.get_events(agent_id="test_agent")
        self.assertTrue(truncated[0]["content"].endswith("[TRUNCATED]"))
        self.assertEqual(len(truncated[0]["content"]), len("x" * 1000 + " [TRUNCATED]"))

        full = tools.get_events(agent_id="test_agent", full_content=True)
        self.assertEqual(full[0]["content"], long_content)
        self.assertNotIn("[TRUNCATED]", full[0]["content"])

    def test_default_still_truncates_long_content(self):
        # Regression guard: adding full_content/event_id must not change the existing default
        # (full_content omitted) truncation behavior for ordinary list calls.
        long_content = "y" * 2000
        tools.log_event(event_type="issue", content=long_content)

        events = tools.get_events(agent_id="test_agent")
        self.assertTrue(events[0]["content"].endswith("[TRUNCATED]"))
        self.assertLess(len(events[0]["content"]), len(long_content))

    def test_event_id_filter_returns_single_full_content_event(self):
        long_content = "z" * 1500
        tools.log_event(event_type="issue", content=long_content)
        tools.log_event(event_type="issue", content="short other event")

        # Discover the long event's id the way a real caller would: from a truncated list result.
        listed = tools.get_events(agent_id="test_agent", event_type="issue")
        target_id = next(e["id"] for e in listed if e["content"].endswith("[TRUNCATED]"))

        matched = tools.get_events(event_id=target_id)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["id"], target_id)
        self.assertEqual(matched[0]["content"], long_content)
        self.assertNotIn("[TRUNCATED]", matched[0]["content"])

    def test_event_id_filter_no_match_returns_empty_list(self):
        tools.log_event(event_type="issue", content="irrelevant")
        matched = tools.get_events(event_id="00000000-0000-0000-0000-000000000000")
        self.assertEqual(matched, [])


class TestLogEventEndToEnd(unittest.TestCase):
    """End-to-end coverage of log_event's Phase 6 reshaping (agent API redesign plan §5.7, item
    23): event_type as the parameter name, no agent_id parameter, owner binding as agent_id, and
    agent_session_id auto-populated from SESSION_IDENTITY.agent_session_id."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_event_type_is_the_parameter_name(self):
        res = tools.log_event(event_type="decision", content="picked option A")
        self.assertEqual(res["status"], "ok")
        self.assertIn("Event logged successfully", res["data"]["message"])

    def test_no_agent_id_parameter_exists(self):
        with self.assertRaises(TypeError):
            tools.log_event(agent_id="agent_p", event_type="decision", content="x")

    def test_bound_owner_becomes_stored_agent_id(self):
        res = tools.log_event(event_type="issue", content="owner binding check")
        event_id = res["data"]["id"]
        row = self.conn.execute("SELECT agent_id FROM events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row[0], "test_agent")

    def test_agent_session_id_is_minted_by_adapter(self):
        from saltmdb.domain.services import event_service

        res = event_service.log_event(
            agent_id="agent_sess2",
            content="session id check",
            agent_session_id=SESSION_IDENTITY.agent_session_id,
            db_connection=self.conn,
        )
        event_id = res["data"]["id"]
        row = self.conn.execute(
            "SELECT agent_session_id FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        self.assertEqual(row[0], SESSION_IDENTITY.agent_session_id)

    def test_agent_session_id_is_never_none(self):
        from saltmdb.domain.services import event_service

        res = event_service.log_event(
            agent_id="agent_sess3",
            content="no session bound",
            agent_session_id=SESSION_IDENTITY.agent_session_id,
            db_connection=self.conn,
        )
        event_id = res["data"]["id"]
        row = self.conn.execute(
            "SELECT agent_session_id FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        self.assertEqual(row[0], SESSION_IDENTITY.agent_session_id)


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
