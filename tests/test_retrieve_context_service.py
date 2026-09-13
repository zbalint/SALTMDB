import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast
from unittest.mock import patch

from saltmdb import config
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.domain.services.conflict_set_service import assemble_conflict_sets
from saltmdb.domain.services.context_budget_service import pack_context_budget
from saltmdb.domain.services.context_expansion_service import PrimaryHit, expand_context_candidates
from saltmdb.domain.services.embedding_service import get_model
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import store_relation
from saltmdb.domain.services.retrieve_context_service import assemble_retrieve_context


def _memory_id(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        return cast(dict[str, str], result["data"])["id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class TestRetrieveContextService(unittest.TestCase):
    temp_dir: str = ""
    db_path: str = ""
    conn: sqlite3.Connection = cast(sqlite3.Connection, cast(object, None))

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _memory(
        self,
        title: str,
        content: str | None = None,
        *,
        owner_id: str = "retrieve-context-test",
        memory_type: Literal["fact", "event", "procedure", "decision", "preference"] = "fact",
    ) -> str:
        return _memory_id(
            store_memory(
                content=content or f"Retrieve context fixture content for {title} ({uuid.uuid4()})",
                title=title,
                owner_id=owner_id,
                memory_type=memory_type,
                db_connection=self.conn,
            )
        )

    def _relation(self, source_id: str, target_id: str, predicate: str) -> str:
        result = store_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            db_connection=self.conn,
        )
        self.assertIn("successfully stored", result, result)
        return _memory_id(result)

    def _raw_relation(self, source_id: str, target_id: str, predicate: str) -> str:
        relation_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (relation_id, source_id, target_id, predicate, now, now, now),
        )
        self.conn.commit()
        return relation_id

    def _assemble(
        self,
        query: str,
        *,
        limit: int | None = None,
        budget_tokens: int | None = None,
        owner_id: str = "retrieve-context-test",
    ) -> dict[str, Any]:
        return assemble_retrieve_context(
            query,
            owner_id,
            limit=limit,
            budget_tokens=budget_tokens,
            db_connection=self.conn,
        )

    def _content_tokens(self, entity_id: str) -> int:
        row = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        assert row is not None
        content = row[0]
        return 0 if content == "" else get_model().token_count(content)

    def _real_primary_hits(self, query: str, limit: int) -> list[dict[str, Any]]:
        result = memory_service.search_memory(
            owner_id="retrieve-context-test",
            query_keywords=query,
            limit=limit,
            mode="strict",
            include_related=False,
            db_connection=self.conn,
        )
        if not isinstance(result, list):
            self.fail(f"search fixture failed: {result}")
        return [{"id": hit["id"], "score": hit["score"]} for hit in result]

    def _real_budget_result(
        self, query: str, budget_tokens: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        primary_hits = self._real_primary_hits(query, limit=1)
        pit = datetime.now(UTC).isoformat()
        expansion_result = expand_context_candidates(
            cast(list[PrimaryHit], primary_hits),
            point_in_time=pit,
            db_connection=self.conn,
        )
        conflict_result = assemble_conflict_sets(
            expansion_result,
            primary_hits,
            point_in_time=pit,
            db_connection=self.conn,
        )
        budget_result = pack_context_budget(
            expansion_result,
            primary_hits,
            conflict_result,
            budget_tokens=budget_tokens,
            db_connection=self.conn,
        )
        return primary_hits, expansion_result, budget_result

    def _assert_empty_envelope(self, result: dict[str, Any], query: str) -> None:
        self.assertEqual(
            result,
            {
                "query": query,
                "memories": [],
                "edges": [],
                "lineage": {},
                "conflict_sets": [],
                "metadata": {
                    "strategy": "local",
                    "fan_out": {
                        "cap": 0,
                        "eligible_count": 0,
                        "truncated": False,
                        "dropped_count": 0,
                        "conflict_reserve": {
                            "cap": config.CONTEXT_EXPANSION_CONTRADICTS_CAP,
                            "eligible_count": 0,
                            "truncated": False,
                            "dropped_count": 0,
                        },
                    },
                    "budget": {
                        "unit": "tokens",
                        "limit": config.CONTEXT_BUDGET_DEFAULT_TOKENS,
                        "used": 0,
                        "primary_truncated": False,
                        "primary_dropped_count": 0,
                        "expansion_truncated": False,
                        "expansion_dropped_count": 0,
                        "conflict_reserve_tokens_used": 0,
                    },
                },
            },
        )

    def test_zero_primary_hits_compose_the_all_empty_envelope(self):
        query = "no matching primary hits"
        with patch(
            "saltmdb.domain.services.retrieve_context_service.memory_service.search_memory",
            return_value=[],
        ):
            result = self._assemble(query)

        self._assert_empty_envelope(result, query)

    def test_search_internal_error_sentinel_composes_as_empty_not_fatal(self):
        query = "upstream failure"
        with patch(
            "saltmdb.domain.services.retrieve_context_service.memory_service.search_memory",
            return_value=[{"error": "boom"}],
        ):
            result = self._assemble(query)

        self._assert_empty_envelope(result, query)

    def test_single_dependency_chain_surfaces_primary_and_expansion_without_conflicts(self):
        primary = self._memory(
            "Dependency chain primary",
            "single-dependency-query primary content",
        )
        expansion = self._memory("Dependency chain expansion")
        self._relation(primary, expansion, "depends_on")

        result = self._assemble("single-dependency-query", limit=1)

        self.assertEqual(
            [memory["entity_id"] for memory in result["memories"]], [primary, expansion]
        )
        primary_row, expansion_row = result["memories"]
        self.assertEqual(primary_row["inclusion"], "primary")
        self.assertEqual(
            primary_row["retrieval_provenance"],
            [{"reason": "primary_search", "rank": 1}],
        )
        self.assertEqual(primary_row["title"], "Dependency chain primary")
        self.assertEqual(primary_row["memory_type"], "fact")
        self.assertEqual(expansion_row["inclusion"], "expansion")
        self.assertEqual(
            expansion_row["retrieval_provenance"],
            [
                {
                    "reason": "graph_expansion",
                    "origin_hit_id": primary,
                    "predicate": "depends_on",
                    "direction": "outbound",
                    "hop_depth": 1,
                }
            ],
        )
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["conflict_sets"], [])

    def test_in_network_edge_surfaces_verbatim_between_two_primary_hits(self):
        query = "in-network-shared-query"
        primary_a = self._memory("In-network primary A", f"{query} first")
        primary_b = self._memory("In-network primary B", f"{query} second")
        relation_id = self._relation(primary_a, primary_b, "depends_on")

        result = self._assemble(query, limit=2)

        self.assertEqual(
            {
                memory["entity_id"]
                for memory in result["memories"]
                if memory["inclusion"] == "primary"
            },
            {primary_a, primary_b},
        )
        self.assertEqual(
            result["edges"],
            [
                {
                    "relation_id": relation_id,
                    "source_id": primary_a,
                    "target_id": primary_b,
                    "predicate": "depends_on",
                }
            ],
        )

    def test_three_generation_supersession_chain_appears_in_lineage_only(self):
        head = self._memory(
            "Supersession head",
            "supersession-chain-query current content",
        )
        ancestors = [self._memory(f"Supersession ancestor {index}") for index in range(1, 4)]
        predecessor = head
        for ancestor in ancestors:
            self._raw_relation(predecessor, ancestor, "supersedes")
            predecessor = ancestor

        result = self._assemble("supersession-chain-query", limit=1)

        self.assertEqual(set(result["lineage"]), {head})
        self.assertEqual(result["lineage"][head]["current"]["id"], head)
        self.assertEqual(
            [entry["id"] for entry in result["lineage"][head]["historical"]], ancestors
        )
        self.assertEqual(result["conflict_sets"], [])

    def test_budget_dropped_expansion_head_pruned_from_lineage(self):
        primary = self._memory(
            "Lineage prune primary",
            "lineage-prune-query primary content",
        )
        dropped_head = self._memory(
            "Lineage prune dropped expansion",
            "An expansion body deliberately sized to exceed the remaining budget.",
        )
        ancestor = self._memory("Lineage prune ancestor")
        self._relation(primary, dropped_head, "depends_on")
        self._raw_relation(dropped_head, ancestor, "supersedes")
        primary_tokens = self._content_tokens(primary)
        _, expansion_result, packed = self._real_budget_result(
            "lineage-prune-query", primary_tokens
        )
        self.assertIn(
            dropped_head,
            {candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]},
        )
        self.assertIn(dropped_head, packed["dropped_entity_ids"]["expansion"])

        result = self._assemble(
            "lineage-prune-query",
            limit=1,
            budget_tokens=primary_tokens,
        )

        visible_ids = {memory["entity_id"] for memory in result["memories"]}
        self.assertNotIn(dropped_head, visible_ids)
        self.assertNotIn(
            dropped_head,
            result["lineage"],
            "a lineage entry survived for a head that budget truncation dropped from memories[]",
        )

    def test_unresolved_contradiction_force_includes_conflict_only_member(self):
        primary = self._memory(
            "Unresolved contradiction primary",
            "unresolved-contradiction-query primary content",
        )
        conflict_only = self._memory("Unresolved contradiction member")
        self._raw_relation(primary, conflict_only, "contradicts")

        result = self._assemble("unresolved-contradiction-query", limit=1)

        rows_by_id = {memory["entity_id"]: memory for memory in result["memories"]}
        self.assertEqual(rows_by_id[conflict_only]["inclusion"], "conflict_only")
        self.assertEqual(
            rows_by_id[conflict_only]["retrieval_provenance"],
            [{"reason": "contradicts_force_include", "conflict_set_id": "cs-1"}],
        )
        self.assertEqual(len(result["conflict_sets"]), 1)
        self.assertEqual(result["conflict_sets"][0]["id"], "cs-1")
        self.assertEqual(
            {member["id"] for member in result["conflict_sets"][0]["members"]},
            {primary, conflict_only},
        )

    def test_lifecycle_resolved_contradiction_surfaces_via_lineage_not_conflict_sets(self):
        head = self._memory(
            "Resolved contradiction head",
            "resolved-contradiction-query current content",
        )
        ancestor = self._memory("Resolved contradiction ancestor")
        self._raw_relation(head, ancestor, "supersedes")
        self._raw_relation(head, ancestor, "contradicts")
        self.conn.execute("UPDATE entities SET status='archived' WHERE id=?", (ancestor,))
        self.conn.commit()

        result = self._assemble("resolved-contradiction-query", limit=1)

        self.assertEqual(result["conflict_sets"], [])
        lineage = result["lineage"][head]
        self.assertTrue(lineage["current"]["was_flagged_contradiction"])
        self.assertEqual(lineage["current"]["contradicted_with"], [ancestor])
        self.assertTrue(lineage["historical"][0]["was_flagged_contradiction"])
        self.assertEqual(lineage["historical"][0]["contradicted_with"], [head])

    def test_equal_visibility_reconciles_budget_dropped_conflict_expansion(self):
        primary = self._memory(
            "Equal visibility primary",
            "equal-visibility-query primary content",
        )
        expansion = self._memory(
            "Equal visibility expansion",
            "A deliberately nonempty expansion body for budget reconciliation.",
        )
        self._relation(primary, expansion, "depends_on")
        self._raw_relation(primary, expansion, "contradicts")
        primary_tokens = self._content_tokens(primary)
        primary_hits, expansion_result, packed = self._real_budget_result(
            "equal-visibility-query", primary_tokens
        )
        self.assertEqual([hit["id"] for hit in primary_hits], [primary])
        self.assertIn(
            expansion,
            {candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]},
        )
        self.assertIn(expansion, packed["dropped_entity_ids"]["expansion"])

        result = self._assemble(
            "equal-visibility-query",
            limit=1,
            budget_tokens=primary_tokens,
        )

        expansion_row = next(
            memory for memory in result["memories"] if memory["entity_id"] == expansion
        )
        self.assertEqual(expansion_row["inclusion"], "expansion")
        self.assertEqual(expansion_row["retrieval_provenance"][0]["reason"], "graph_expansion")
        self.assertFalse(result["metadata"]["budget"]["expansion_truncated"])
        self.assertEqual(result["metadata"]["budget"]["expansion_dropped_count"], 0)
        self.assertGreater(result["metadata"]["budget"]["used"], primary_tokens)

    def test_equal_visibility_reconciliation_preserves_unrelated_budget_drop(self):
        primary = self._memory(
            "Selective reconciliation primary",
            "selective-reconciliation-query primary content",
        )
        conflict_expansion = self._memory(
            "Selective reconciliation conflict expansion",
            "Conflict expansion body that exceeds the remaining budget.",
        )
        ordinary_expansion = self._memory(
            "Selective reconciliation ordinary expansion",
            "Ordinary expansion body that also exceeds the remaining budget.",
        )
        self._relation(primary, conflict_expansion, "depends_on")
        self._raw_relation(primary, conflict_expansion, "contradicts")
        self._relation(primary, ordinary_expansion, "depends_on")
        primary_tokens = self._content_tokens(primary)
        _, expansion_result, packed = self._real_budget_result(
            "selective-reconciliation-query", primary_tokens
        )
        self.assertEqual(
            {candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]},
            {conflict_expansion, ordinary_expansion},
        )
        self.assertEqual(
            set(packed["dropped_entity_ids"]["expansion"]),
            {conflict_expansion, ordinary_expansion},
        )

        result = self._assemble(
            "selective-reconciliation-query",
            limit=1,
            budget_tokens=primary_tokens,
        )

        visible_ids = {memory["entity_id"] for memory in result["memories"]}
        self.assertIn(conflict_expansion, visible_ids)
        self.assertNotIn(ordinary_expansion, visible_ids)
        self.assertTrue(result["metadata"]["budget"]["expansion_truncated"])
        self.assertEqual(result["metadata"]["budget"]["expansion_dropped_count"], 1)

    def test_fan_out_cap_truncation_passes_through_from_expansion(self):
        primary = self._memory(
            "Fan-out cap primary",
            "fan-out-cap-query primary content",
        )
        neighbors = [self._memory(f"Fan-out cap neighbor {index}") for index in range(12)]
        for neighbor in neighbors:
            self._relation(primary, neighbor, "depends_on")

        result = self._assemble("fan-out-cap-query", limit=1)

        fan_out = result["metadata"]["fan_out"]
        cap = config.CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS
        self.assertTrue(fan_out["truncated"])
        self.assertEqual(fan_out["dropped_count"], len(neighbors) - cap)
        self.assertEqual(
            [
                memory["entity_id"]
                for memory in result["memories"]
                if memory["inclusion"] == "expansion"
            ],
            sorted(neighbors)[:cap],
        )

    def test_limit_one_honors_primary_search_count_before_expansion(self):
        query = "primary-limit-query"
        primary_a = self._memory("Limit primary A", f"{query} first")
        primary_b = self._memory("Limit primary B", f"{query} second")

        result = self._assemble(query, limit=1)

        primary_rows = [memory for memory in result["memories"] if memory["inclusion"] == "primary"]
        self.assertEqual(len(primary_rows), 1)
        self.assertEqual(
            primary_rows[0]["retrieval_provenance"], [{"reason": "primary_search", "rank": 1}]
        )
        self.assertEqual(
            result["metadata"]["fan_out"]["cap"], config.CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS
        )
        self.assertIn(primary_rows[0]["entity_id"], {primary_a, primary_b})

    def test_budget_tokens_above_ceiling_reports_configured_maximum(self):
        primary = self._memory(
            "Budget ceiling primary",
            "budget-ceiling-query primary content",
        )

        result = self._assemble(
            "budget-ceiling-query",
            limit=1,
            budget_tokens=config.CONTEXT_BUDGET_MAX_TOKENS + 100_000,
        )

        self.assertEqual(result["metadata"]["budget"]["limit"], config.CONTEXT_BUDGET_MAX_TOKENS)
        self.assertIn(primary, {memory["entity_id"] for memory in result["memories"]})

    def test_stale_conflict_only_reference_uses_unknown_memory_type(self):
        primary = self._memory(
            "Stale conflict primary",
            "stale-conflict-query primary content",
        )
        missing = str(uuid.uuid4())
        _ = self.conn.execute("PRAGMA foreign_keys = OFF")
        relation_id = self._raw_relation(primary, missing, "contradicts")
        _ = self.conn.execute("PRAGMA foreign_keys = ON")
        primary_hits = [
            {
                "id": primary,
                "title": "Stale conflict primary",
                "memory_type": "fact",
                "score": 1.0,
            }
        ]
        expansion_result = {
            "in_network_edges": [],
            "expansion_candidates": [],
            "contradicts_edges": [
                {
                    "relation_id": relation_id,
                    "source_id": primary,
                    "target_id": missing,
                    "predicate": "contradicts",
                }
            ],
            "fan_out": {
                "cap": config.CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS * len(primary_hits),
                "eligible_count": 0,
                "truncated": False,
                "dropped_count": 0,
            },
        }
        with (
            patch(
                "saltmdb.domain.services.retrieve_context_service.expand_context_candidates",
                return_value=expansion_result,
            ) as expand_context_candidates_mock,
            patch(
                "saltmdb.domain.services.retrieve_context_service.memory_service.search_memory",
                return_value=primary_hits,
            ) as search_memory_mock,
        ):
            result = self._assemble("stale-conflict-query", limit=1)

        search_memory_mock.assert_called_once()
        expand_context_candidates_mock.assert_called_once()
        stale_row = next(memory for memory in result["memories"] if memory["entity_id"] == missing)
        self.assertEqual(stale_row["memory_type"], "unknown")

    def test_one_connection_is_shared_across_search_and_graph_pipeline(self):
        primary = self._memory(
            "Single connection primary",
            "single-connection-query primary content",
        )
        from saltmdb.db.connection import get_connection as real_get_connection

        with patch(
            "saltmdb.domain.services.retrieve_context_service.get_connection",
            wraps=real_get_connection,
        ) as get_connection:
            result = assemble_retrieve_context(
                "single-connection-query",
                "retrieve-context-test",
                limit=1,
                db_path=self.db_path,
            )

        self.assertIn(primary, {memory["entity_id"] for memory in result["memories"]})
        self.assertLessEqual(get_connection.call_count, 1)

    def test_connection_only_passes_connection_database_path_to_search(self):
        query = "connection-only-db-path"
        with patch(
            "saltmdb.domain.services.retrieve_context_service.memory_service.search_memory",
            return_value=[],
        ) as search_memory:
            result = assemble_retrieve_context(
                query,
                "retrieve-context-test",
                db_connection=self.conn,
            )

        self._assert_empty_envelope(result, query)
        self.assertEqual(search_memory.call_args.kwargs["db_path"], self.db_path)


if __name__ == "__main__":
    unittest.main()
