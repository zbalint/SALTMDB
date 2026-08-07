"""Tests for Part A (multi-hop supersession-chain resolution, search_memory mode="strict") --
plans/scalable-strolling-stallman.md, SALTMDB memory `9c199005`.

_resolve_supersession_chains / _substitute_resolved_heads are plain sqlite + Python -- no
sqlite-vec / embeddings involved, unlike test_topic_rerank.py's fixtures.
"""

import unittest
import tempfile
import os
import shutil
import uuid

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    _compute_superseded_ids_bitemporal,
    _resolve_supersession_chains,
    _substitute_resolved_heads,
)


class TestSupersessionChainResolution(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(
        self,
        entity_id: str,
        status: str = "raw",
        updated_at: str = "2026-01-01T00:00:00+00:00",
        created_at: str = "2026-01-01T00:00:00+00:00",
        owner_id: str = "test_user",
        is_core: bool = False,
        context_id: str = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash, is_core, context_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                created_at,
                updated_at,
                updated_at,
                owner_id,
                status,
                entity_id,
                f"content for {entity_id}",
                entity_id,
                1 if is_core else 0,
                context_id,
            ),
        )
        self.conn.commit()

    def _insert_supersedes(
        self,
        source_id: str,
        target_id: str,
        valid_from: str = None,
        valid_to: str = None,
        valid_at: str = None,
        invalid_at: str = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_from, valid_to,"
            " valid_at, invalid_at) VALUES (?, ?, ?, 'supersedes', ?, ?, ?, ?)",
            (str(uuid.uuid4()), source_id, target_id, valid_from, valid_to, valid_at, invalid_at),
        )
        self.conn.commit()

    # -- no supersession: absent from the returned dict --

    def test_no_supersedes_edge_absent_from_result(self):
        self._insert_entity("a")
        result = _resolve_supersession_chains(self.conn, ["a"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    def test_empty_candidate_pool_returns_empty_dict(self):
        result = _resolve_supersession_chains(self.conn, [], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    # -- single-hop and multi-hop resolution --

    def test_single_hop_resolves_to_source(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {"old": "new"})

    def test_multi_hop_chain_walks_to_terminal_head(self):
        # oldest <- middle <- newest (newest supersedes middle supersedes oldest)
        self._insert_entity("oldest")
        self._insert_entity("middle")
        self._insert_entity("newest")
        self._insert_supersedes("middle", "oldest")
        self._insert_supersedes("newest", "middle")
        result = _resolve_supersession_chains(self.conn, ["oldest"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {"oldest": "newest"})

    def test_seed_level_fork_tie_break_by_updated_at(self):
        # "old" (the SEED/root itself) is superseded by both "candidate_a" (older updated_at) and
        # "candidate_b" (newer updated_at). Tie-break must pick candidate_b.
        self._insert_entity("old")
        self._insert_entity("candidate_a", updated_at="2026-01-02T00:00:00+00:00")
        self._insert_entity("candidate_b", updated_at="2026-01-03T00:00:00+00:00")
        self._insert_supersedes("candidate_a", "old")
        self._insert_supersedes("candidate_b", "old")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {"old": "candidate_b"})

    def test_genuine_mid_chain_fork_tie_break(self):
        # "oldest" -> "middle" is a single, unambiguous first hop. The fork happens at the SECOND
        # hop: both "candidate_a" and "candidate_b" supersede "middle" -- not the seed itself
        # (distinct from test_seed_level_fork_tie_break_by_updated_at above; a fork mid-chain must
        # resolve via the same deterministic tie-break as one at the seed, per the plan's explicit
        # "applied greedily at EVERY hop" requirement).
        self._insert_entity("oldest")
        self._insert_entity("middle")
        self._insert_entity("candidate_a", updated_at="2026-01-02T00:00:00+00:00")
        self._insert_entity("candidate_b", updated_at="2026-01-03T00:00:00+00:00")
        self._insert_supersedes("middle", "oldest")
        self._insert_supersedes("candidate_a", "middle")
        self._insert_supersedes("candidate_b", "middle")
        result = _resolve_supersession_chains(self.conn, ["oldest"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {"oldest": "candidate_b"})

    def test_fork_tie_break_falls_back_to_created_at_then_id(self):
        self._insert_entity("old")
        # Same updated_at -- tie-break falls through to created_at.
        self._insert_entity(
            "candidate_a",
            updated_at="2026-01-02T00:00:00+00:00",
            created_at="2026-01-01T00:00:00+00:00",
        )
        self._insert_entity(
            "candidate_b",
            updated_at="2026-01-02T00:00:00+00:00",
            created_at="2026-01-02T00:00:00+00:00",
        )
        self._insert_supersedes("candidate_a", "old")
        self._insert_supersedes("candidate_b", "old")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {"old": "candidate_b"})

    def test_fork_tie_break_falls_back_to_id_when_both_timestamps_tie(self):
        self._insert_entity("old")
        # Both updated_at AND created_at tie -- tie-break falls through to the final, deterministic
        # id comparison (max() by id string).
        self._insert_entity(
            "aaa_candidate",
            updated_at="2026-01-02T00:00:00+00:00",
            created_at="2026-01-01T00:00:00+00:00",
        )
        self._insert_entity(
            "zzz_candidate",
            updated_at="2026-01-02T00:00:00+00:00",
            created_at="2026-01-01T00:00:00+00:00",
        )
        self._insert_supersedes("aaa_candidate", "old")
        self._insert_supersedes("zzz_candidate", "old")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(
            result,
            {"old": "zzz_candidate"},
            "id-based fallback must pick the lexicographically larger id",
        )

    # -- cycle guard --

    def test_cycle_abstains(self):
        self._insert_entity("a")
        self._insert_entity("b")
        self._insert_supersedes("b", "a")
        self._insert_supersedes("a", "b")
        result = _resolve_supersession_chains(self.conn, ["a"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {}, "a cycle must abstain, not substitute a non-terminal node")

    def test_self_loop_abstains(self):
        self._insert_entity("a")
        self._insert_supersedes("a", "a")
        result = _resolve_supersession_chains(self.conn, ["a"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    # -- depth cap --

    def test_depth_cap_breach_abstains(self):
        # 3-hop chain, max_depth=2 -- must abstain, not silently return the depth-2 node.
        self._insert_entity("hop0")
        self._insert_entity("hop1")
        self._insert_entity("hop2")
        self._insert_entity("hop3")
        self._insert_supersedes("hop1", "hop0")
        self._insert_supersedes("hop2", "hop1")
        self._insert_supersedes("hop3", "hop2")
        result = _resolve_supersession_chains(
            self.conn, ["hop0"], ["e.status != 'archived'"], [], max_depth=2
        )
        self.assertEqual(result, {})

    def test_chain_within_depth_cap_resolves(self):
        self._insert_entity("hop0")
        self._insert_entity("hop1")
        self._insert_entity("hop2")
        self._insert_supersedes("hop1", "hop0")
        self._insert_supersedes("hop2", "hop1")
        result = _resolve_supersession_chains(
            self.conn, ["hop0"], ["e.status != 'archived'"], [], max_depth=2
        )
        self.assertEqual(result, {"hop0": "hop2"})

    # -- inaccessible/archived intermediate node --

    def test_archived_intermediate_node_abstains(self):
        self._insert_entity("oldest")
        self._insert_entity("middle", status="archived")
        self._insert_entity("newest")
        self._insert_supersedes("middle", "oldest")
        self._insert_supersedes("newest", "middle")
        result = _resolve_supersession_chains(self.conn, ["oldest"], ["e.status != 'archived'"], [])
        self.assertEqual(
            result,
            {},
            "an archived intermediate hop must not be trusted even if the final node looks fine",
        )

    def test_archived_terminal_node_abstains(self):
        self._insert_entity("old")
        self._insert_entity("new", status="archived")
        self._insert_supersedes("new", "old")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    # -- bitemporal validity (all four columns) --

    def test_future_valid_from_excludes_edge(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", valid_from="2099-01-01T00:00:00+00:00")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    def test_past_valid_to_excludes_edge(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", valid_to="2020-01-01T00:00:00+00:00")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    def test_future_valid_at_excludes_edge(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", valid_at="2099-01-01T00:00:00+00:00")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    def test_past_invalid_at_excludes_edge(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", invalid_at="2020-01-01T00:00:00+00:00")
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {})

    def test_currently_valid_across_all_four_columns_resolves(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes(
            "new",
            "old",
            valid_from="2020-01-01T00:00:00+00:00",
            valid_to="2099-01-01T00:00:00+00:00",
            valid_at="2020-01-01T00:00:00+00:00",
            invalid_at="2099-01-01T00:00:00+00:00",
        )
        result = _resolve_supersession_chains(self.conn, ["old"], ["e.status != 'archived'"], [])
        self.assertEqual(result, {"old": "new"})

    # -- filter re-application --

    def test_filter_reapplication_rejects_head_outside_owner_filter(self):
        self._insert_entity("old", owner_id="alice")
        self._insert_entity("new", owner_id="bob")
        self._insert_supersedes("new", "old")
        # Original query's own where_clauses/params restrict to owner_id='alice' -- "new" (owned
        # by bob) must not be substituted in even though the chain walk itself succeeds.
        result = _resolve_supersession_chains(
            self.conn,
            ["old"],
            ["e.status != 'archived'", "e.owner_id = ?"],
            ["alice"],
        )
        self.assertEqual(result, {})

    def test_filter_reapplication_passes_head_matching_owner_filter(self):
        self._insert_entity("old", owner_id="alice")
        self._insert_entity("new", owner_id="alice")
        self._insert_supersedes("new", "old")
        result = _resolve_supersession_chains(
            self.conn,
            ["old"],
            ["e.status != 'archived'", "e.owner_id = ?"],
            ["alice"],
        )
        self.assertEqual(result, {"old": "new"})

    def test_filter_reapplication_rejects_head_outside_context_filter(self):
        self._insert_entity("old", context_id="project_a")
        self._insert_entity("new", context_id="project_b")
        self._insert_supersedes("new", "old")
        result = _resolve_supersession_chains(
            self.conn,
            ["old"],
            ["e.status != 'archived'", "e.context_id = ?"],
            ["project_a"],
        )
        self.assertEqual(result, {})

    def test_filter_reapplication_rejects_head_outside_is_core_filter(self):
        self._insert_entity("old", is_core=True)
        self._insert_entity("new", is_core=False)
        self._insert_supersedes("new", "old")
        result = _resolve_supersession_chains(
            self.conn,
            ["old"],
            ["e.status != 'archived'", "e.is_core = ?"],
            [1],
        )
        self.assertEqual(result, {})

    # -- multiple independent roots in one batched call --

    def test_multiple_roots_batched_in_one_call(self):
        self._insert_entity("old1")
        self._insert_entity("new1")
        self._insert_supersedes("new1", "old1")
        self._insert_entity("old2")
        self._insert_entity("new2")
        self._insert_supersedes("new2", "old2")
        self._insert_entity("untouched")
        result = _resolve_supersession_chains(
            self.conn, ["old1", "old2", "untouched"], ["e.status != 'archived'"], []
        )
        self.assertEqual(result, {"old1": "new1", "old2": "new2"})


class TestSubstituteResolvedHeads(unittest.TestCase):
    """Pure unit tests, no DB needed."""

    def test_noop_when_nothing_resolved(self):
        rrf = {"a": 0.5, "b": 0.3}
        result = _substitute_resolved_heads(rrf, {})
        self.assertEqual(result, {"a": 0.5, "b": 0.3})

    def test_single_substitution_preserves_score(self):
        rrf = {"old": 0.5, "b": 0.3}
        result = _substitute_resolved_heads(rrf, {"old": "new"})
        self.assertEqual(result, {"new": 0.5, "b": 0.3})

    def test_dedup_merges_to_max_score_not_sum(self):
        # Two different pool candidates both resolve to the same head -- must merge to MAX, never
        # sum (sum would let unrelated pool entries inflate the head's fused score).
        rrf = {"cand_a": 0.4, "cand_b": 0.6}
        result = _substitute_resolved_heads(rrf, {"cand_a": "head", "cand_b": "head"})
        self.assertEqual(result, {"head": 0.6})
        self.assertNotEqual(result.get("head"), 1.0, "must not sum 0.4 + 0.6")

    def test_result_resorted_by_score_descending(self):
        rrf = {"a": 0.1, "b": 0.9}
        result = _substitute_resolved_heads(rrf, {"a": "head", "b": "head2"})
        self.assertEqual(list(result.keys()), ["head2", "head"])


class TestComputeSupersededIdsBitemporal(unittest.TestCase):
    """mode="history"'s own bitemporal-aware superseded-ids check (Codex review P1 finding:
    reusing demote_superseded's single-column `_compute_superseded_ids` here would mislabel a
    not-currently-valid edge as superseding)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(self, entity_id: str) -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', 'raw',"
            " ?, ?, ?)",
            (entity_id, entity_id, f"content for {entity_id}", entity_id),
        )
        self.conn.commit()

    def _insert_supersedes(self, source_id, target_id, **temporal_cols) -> None:
        cols = ["valid_from", "valid_to", "valid_at", "invalid_at"]
        values = [temporal_cols.get(c) for c in cols]
        self.conn.execute(
            f"INSERT INTO relations (id, source_id, target_id, predicate, {', '.join(cols)})"
            f" VALUES (?, ?, ?, 'supersedes', {', '.join('?' for _ in cols)})",
            (str(uuid.uuid4()), source_id, target_id, *values),
        )
        self.conn.commit()

    def test_empty_pool_is_noop(self):
        self.assertEqual(_compute_superseded_ids_bitemporal([], self.conn), set())

    def test_currently_valid_edge_tags_target(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old")
        self.assertEqual(_compute_superseded_ids_bitemporal(["old"], self.conn), {"old"})

    def test_future_valid_from_does_not_tag(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", valid_from="2099-01-01T00:00:00+00:00")
        self.assertEqual(_compute_superseded_ids_bitemporal(["old"], self.conn), set())

    def test_past_valid_to_does_not_tag(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", valid_to="2020-01-01T00:00:00+00:00")
        self.assertEqual(_compute_superseded_ids_bitemporal(["old"], self.conn), set())

    def test_future_valid_at_does_not_tag(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", valid_at="2099-01-01T00:00:00+00:00")
        self.assertEqual(_compute_superseded_ids_bitemporal(["old"], self.conn), set())

    def test_past_invalid_at_does_not_tag(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_supersedes("new", "old", invalid_at="2020-01-01T00:00:00+00:00")
        self.assertEqual(_compute_superseded_ids_bitemporal(["old"], self.conn), set())


if __name__ == "__main__":
    unittest.main()
