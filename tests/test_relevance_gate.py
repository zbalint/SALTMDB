"""Tests for Part B (evidence extraction + accept_or_abstain, search_memory mode="strict") --
plans/scalable-strolling-stallman.md, SALTMDB memory `9c199005`/`c27792a1`/`b9b75764`.

_build_candidate_evidence/accept_or_abstain are pure functions (no DB) -- unit-tested directly.
The search_memory(mode=...) seam tests below use the same controlled-seam pattern as
tests/test_topic_rerank.py / tests/test_search_ranking_flags.py: patch _run_fts_search/
semantic_search so the pre-fusion pool is fully deterministic, no sqlite-vec model load needed.
"""

import unittest
import tempfile
import os
import shutil
import uuid
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    _build_candidate_evidence,
    _run_fts_search,
    accept_or_abstain,
    search_memory,
)


class TestAcceptOrAbstain(unittest.TestCase):
    """Pure unit tests -- no DB, no embeddings."""

    def test_dual_channel_accepted(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": True,
            "in_fts_and": True,
            "in_fts_or_only": False,
            "in_semantic": True,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "dual_channel")

    def test_fts_only_accepted(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": True,
            "in_fts_or_only": False,
            "in_semantic": False,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "fts_match")

    def test_semantic_only_same_specific_topic_accepted(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": False,
            "in_semantic": True,
            "semantic_verdict": "SAME_SPECIFIC_TOPIC",
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "semantic_only_same_specific_topic")

    def test_semantic_only_broadly_related_rejected(self):
        """The exact 'sluszkulcs' failure shape (SALTMDB memory `c27792a1`): weak, uncorroborated
        vector-only proximity must NOT be treated as sufficient grounding."""
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": False,
            "in_semantic": True,
            "semantic_verdict": "BROADLY_RELATED_THEMES",
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)
        self.assertEqual(reason, "semantic_only_insufficient_topic_grounding")

    def test_semantic_only_without_topic_score_rejected(self):
        """No topic_score computed at all (topic_score stays optional -- Part B cost note) must
        not be treated as an implicit pass."""
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": False,
            "in_semantic": True,
            "semantic_verdict": None,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)

    def test_no_evidence_abstains(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": False,
            "in_semantic": False,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_evidence")

    # -- H1 fix: FTS OR-fallback-only matches (memory `ed7cc8d5`) --

    def test_fts_or_fallback_only_without_topic_grounding_rejected(self):
        """An OR-fallback-only match (present only because the AND-joined query found nothing and
        _run_fts_search silently retried with OR) with no semantic_verdict at all must be rejected
        -- it is an incidental single-term hit, not a corroborated match."""
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": True,
            "in_semantic": False,
            "semantic_verdict": None,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)
        self.assertEqual(reason, "fts_or_fallback_insufficient_topic_grounding")

    def test_fts_or_fallback_only_broadly_related_rejected(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": True,
            "in_semantic": False,
            "semantic_verdict": "BROADLY_RELATED_THEMES",
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)
        self.assertEqual(reason, "fts_or_fallback_insufficient_topic_grounding")

    def test_fts_or_fallback_only_same_specific_topic_accepted(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": True,
            "in_semantic": False,
            "semantic_verdict": "SAME_SPECIFIC_TOPIC",
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "fts_or_fallback_same_specific_topic")

    def test_fts_or_fallback_plus_semantic_does_not_bypass_via_dual_channel(self):
        """The exact bypass H1's fix closes: an OR-fallback-only match that ALSO happens to land
        in the semantic pool must NOT take the dual_channel shortcut (dual_channel is correctly
        False here since it's keyed on in_fts_and, not the broader in_fts) -- it must go through
        the same SAME_SPECIFIC_TOPIC check as an OR-fallback-only match with no semantic hit."""
        base_evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts_and": False,
            "in_fts_or_only": True,
            "in_semantic": True,
        }
        rejected = {**base_evidence, "semantic_verdict": "BROADLY_RELATED_THEMES"}
        ok, reason = accept_or_abstain(rejected)
        self.assertFalse(ok)
        self.assertEqual(reason, "fts_or_fallback_insufficient_topic_grounding")

        accepted = {**base_evidence, "semantic_verdict": "SAME_SPECIFIC_TOPIC"}
        ok, reason = accept_or_abstain(accepted)
        self.assertTrue(ok)
        self.assertEqual(reason, "fts_or_fallback_same_specific_topic")

    # -- indirect (resolved supersession head) provenance --

    def test_indirect_ungrounded_predecessor_rejected(self):
        evidence = {"provenance": "indirect", "predecessor_grounded": False}
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)
        self.assertEqual(reason, "indirect_ungrounded_predecessor")

    def test_indirect_grounded_predecessor_accepted(self):
        evidence = {
            "provenance": "indirect",
            "predecessor_grounded": True,
            "semantic_verdict": None,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "indirect_grounded_predecessor")

    def test_indirect_grounded_predecessor_accepted_regardless_of_own_topic_verdict(self):
        """predecessor_grounded is the ONLY condition for an indirect candidate -- the head's own
        semantic_verdict (which the on-demand topic-grounding lookup assigns "DIFFERENT_TOPICS"
        by default even when there's simply no embedding data at all to score) must NOT veto an
        otherwise-grounded resolved head. See accept_or_abstain's own docstring for why an earlier
        version's extra check was reverted (a real bug caught by
        tests/test_search_pagination.py's dedup-collapse case)."""
        evidence = {
            "provenance": "indirect",
            "predecessor_grounded": True,
            "semantic_verdict": "DIFFERENT_TOPICS",
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "indirect_grounded_predecessor")


class TestBuildCandidateEvidence(unittest.TestCase):
    """Pure unit tests -- no DB."""

    def _fts_row(self, entity_id: str, bm25: float = -1.0) -> tuple:
        return (entity_id, "t", "c", 1, 0, bm25, "", "", "u", "s", "{}", None, "fact", 0, None)

    def test_direct_dual_channel_candidate(self):
        fts_rows = [self._fts_row("a")]
        semantic_rows = [("a", 0.1)]
        rrf_map = {"a": 0.5}
        ev = _build_candidate_evidence(["a"], rrf_map, fts_rows, semantic_rows, {}, {})
        self.assertEqual(ev["a"]["provenance"], "direct")
        self.assertTrue(ev["a"]["in_fts"])
        self.assertTrue(ev["a"]["in_semantic"])
        self.assertTrue(ev["a"]["dual_channel"])
        self.assertEqual(ev["a"]["fts_rank"], 0)
        self.assertEqual(ev["a"]["semantic_rank"], 0)

    def test_indirect_candidate_absent_from_pool(self):
        """A resolved head with no native fts/semantic row of its own -- must be classified
        indirect, and must NOT inherit fake fts_rank/semantic_rank/distance values."""
        rrf_map = {"resolved_head": 0.4}
        ev = _build_candidate_evidence(
            ["resolved_head"], rrf_map, [], [], {}, {"resolved_head": ["predecessor_id"]}
        )
        self.assertEqual(ev["resolved_head"]["provenance"], "indirect")
        self.assertFalse(ev["resolved_head"]["in_fts"])
        self.assertFalse(ev["resolved_head"]["in_semantic"])
        self.assertIsNone(ev["resolved_head"]["fts_rank"])
        self.assertIsNone(ev["resolved_head"]["semantic_distance"])

    def test_resolved_head_that_also_directly_matches_is_direct(self):
        """Coexistence case (Part B): a resolved head that ALSO independently appears in the
        native pool must be classified direct, not indirect, even though it's in resolved_from."""
        fts_rows = [self._fts_row("head")]
        rrf_map = {"head": 0.5}
        ev = _build_candidate_evidence(
            ["head"], rrf_map, fts_rows, [], {}, {"head": ["some_predecessor"]}
        )
        self.assertEqual(ev["head"]["provenance"], "direct")

    def test_topic_score_optional_and_absent_by_default(self):
        rrf_map = {"a": 0.5}
        ev = _build_candidate_evidence(["a"], rrf_map, [], [], {}, {})
        self.assertIsNone(ev["a"]["topic_score"])
        self.assertIsNone(ev["a"]["semantic_verdict"])

    def test_topic_score_populated_when_provided(self):
        rrf_map = {"a": 0.5}
        topic_map = {"a": {"topic_score": 0.9, "semantic_verdict": "SAME_SPECIFIC_TOPIC"}}
        ev = _build_candidate_evidence(["a"], rrf_map, [], [], topic_map, {})
        self.assertEqual(ev["a"]["topic_score"], 0.9)
        self.assertEqual(ev["a"]["semantic_verdict"], "SAME_SPECIFIC_TOPIC")

    def test_predecessor_grounded_map_threaded_through(self):
        rrf_map = {"head": 0.5}
        ev = _build_candidate_evidence(
            ["head"], rrf_map, [], [], {}, {"head": ["p1"]}, {"head": True}
        )
        self.assertTrue(ev["head"]["predecessor_grounded"])

    def test_cross_encoder_score_absent_by_default(self):
        """Roadmap ba2cf66f P1#7: cross_encoder_score defaults to None when the map param isn't
        passed at all -- proves the new optional field can't silently affect any existing caller
        that doesn't know about it."""
        rrf_map = {"a": 0.5}
        ev = _build_candidate_evidence(["a"], rrf_map, [], [], {}, {})
        self.assertIsNone(ev["a"]["cross_encoder_score"])

    def test_cross_encoder_score_populated_when_provided(self):
        rrf_map = {"a": 0.5, "b": 0.4}
        ce_map = {"a": 7.5}
        ev = _build_candidate_evidence(["a", "b"], rrf_map, [], [], {}, {}, None, ce_map)
        self.assertEqual(ev["a"]["cross_encoder_score"], 7.5)
        # "b" wasn't scored (e.g. beyond CROSS_ENCODER_MAX_CANDIDATES) -- None, not 0.0 or KeyError.
        self.assertIsNone(ev["b"]["cross_encoder_score"])

    def test_accept_or_abstain_ignores_cross_encoder_score(self):
        """Roadmap ba2cf66f P1#7 scope decision: cross_encoder_score is inert evidence this
        release -- a candidate with NO fts/semantic signal at all must still abstain regardless of
        how confident a cross-encoder score looks, since accept_or_abstain doesn't read the field
        yet (deferred to a future, separately-calibrated gate rule)."""
        rrf_map = {"a": 0.5}
        ce_map = {"a": 99.0}  # a wildly "confident"-looking score
        ev = _build_candidate_evidence(["a"], rrf_map, [], [], {}, {}, None, ce_map)
        ok, reason = accept_or_abstain(ev["a"])
        self.assertFalse(ok)
        self.assertEqual(reason, "no_evidence")

    # -- H1 fix: used_or_fallback threading (memory `ed7cc8d5`) --

    def test_used_or_fallback_false_marks_in_fts_and(self):
        """Default (used_or_fallback=False, the existing behavior every non-migrated caller still
        gets): a row present in fts_rows is a genuine AND match."""
        fts_rows = [self._fts_row("a")]
        rrf_map = {"a": 0.5}
        ev = _build_candidate_evidence(["a"], rrf_map, fts_rows, [], {}, {})
        self.assertTrue(ev["a"]["in_fts"])
        self.assertTrue(ev["a"]["in_fts_and"])
        self.assertFalse(ev["a"]["in_fts_or_only"])

    def test_used_or_fallback_true_marks_in_fts_or_only(self):
        """used_or_fallback=True: every row in fts_rows came from the OR-joined retry, not a
        genuine AND match -- in_fts stays True (broad/history-mode recall unaffected), but
        in_fts_and is False and in_fts_or_only is True."""
        fts_rows = [self._fts_row("a")]
        rrf_map = {"a": 0.5}
        ev = _build_candidate_evidence(["a"], rrf_map, fts_rows, [], {}, {}, used_or_fallback=True)
        self.assertTrue(ev["a"]["in_fts"])
        self.assertFalse(ev["a"]["in_fts_and"])
        self.assertTrue(ev["a"]["in_fts_or_only"])

    def test_dual_channel_false_when_or_fallback_even_with_semantic_hit(self):
        """The exact bypass H1 closed: a candidate present via both the OR-fallback FTS branch
        and the semantic pool must NOT be marked dual_channel -- dual_channel requires a genuine
        AND match, not just any FTS presence."""
        fts_rows = [self._fts_row("a")]
        semantic_rows = [("a", 0.1)]
        rrf_map = {"a": 0.5}
        ev = _build_candidate_evidence(
            ["a"], rrf_map, fts_rows, semantic_rows, {}, {}, used_or_fallback=True
        )
        self.assertTrue(ev["a"]["in_semantic"])
        self.assertTrue(ev["a"]["in_fts_or_only"])
        self.assertFalse(ev["a"]["dual_channel"])


class TestRunFtsSearchFallbackFlag(unittest.TestCase):
    """Direct unit tests on `_run_fts_search` itself (H1 fix, memory `ed7cc8d5`) -- exercises the
    real AND->OR fallback branch against a real FTS5 index, not just through a patched gate test
    (round-1 finding: testing only the gate risks missing a bug in the branch-detection itself)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(self, entity_id: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash, memory_type)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', 'raw',"
            " ?, ?, ?, 'fact')",
            (entity_id, entity_id, content, entity_id),
        )
        self.conn.commit()

    def test_and_match_found_no_fallback(self):
        self._insert_entity("both", "alpha beta together")
        rows, used_or_fallback = _run_fts_search(
            self.conn,
            "alpha beta",
            ["e.status != 'archived'"],
            [],
            10,
            0,
            return_fallback_flag=True,
        )
        self.assertEqual({r[0] for r in rows}, {"both"})
        self.assertFalse(used_or_fallback)

    def test_and_finds_nothing_or_fallback_fires(self):
        """No single entity has both terms -- the AND branch finds nothing, the OR retry finds
        both single-term matches, used_or_fallback must be True."""
        self._insert_entity("has_alpha", "alpha only text")
        self._insert_entity("has_beta", "beta only text")
        rows, used_or_fallback = _run_fts_search(
            self.conn,
            "alpha beta",
            ["e.status != 'archived'"],
            [],
            10,
            0,
            return_fallback_flag=True,
        )
        self.assertEqual({r[0] for r in rows}, {"has_alpha", "has_beta"})
        self.assertTrue(used_or_fallback)

    def test_and_and_or_both_find_nothing_used_or_fallback_false(self):
        """Codex review finding: used_or_fallback must be True only when the OR retry actually
        PRODUCED rows, not merely because the OR branch was attempted -- a query with no matching
        entity at all (neither AND nor OR finds anything) must report ([], False), matching this
        function's own docstring contract ("the OR-joined retry is what produced rows")."""
        self._insert_entity("unrelated", "completely different content, no overlap at all")
        rows, used_or_fallback = _run_fts_search(
            self.conn,
            "zzznomatch yyynomatch",
            ["e.status != 'archived'"],
            [],
            10,
            0,
            return_fallback_flag=True,
        )
        self.assertEqual(rows, [])
        self.assertFalse(used_or_fallback)

    def test_single_term_no_fallback_even_when_zero_rows(self):
        """A single-term query that matches nothing must NOT attempt a fallback (len(terms) > 1
        is required) -- used_or_fallback stays False, not just "unknown"."""
        rows, used_or_fallback = _run_fts_search(
            self.conn,
            "nonexistentterm",
            ["e.status != 'archived'"],
            [],
            10,
            0,
            return_fallback_flag=True,
        )
        self.assertEqual(rows, [])
        self.assertFalse(used_or_fallback)

    def test_empty_query_returns_empty_tuple_not_bare_list(self):
        """The early-return empty-terms path must honor return_fallback_flag too: ([], False),
        never a bare []."""
        rows, used_or_fallback = _run_fts_search(
            self.conn,
            "",
            ["e.status != 'archived'"],
            [],
            10,
            0,
            return_fallback_flag=True,
        )
        self.assertEqual(rows, [])
        self.assertFalse(used_or_fallback)

    def test_return_fallback_flag_default_false_keeps_plain_list(self):
        """Backward compatibility: callers that don't pass return_fallback_flag (the two
        benchmark scripts that call _run_fts_search directly) must keep getting a plain list."""
        self._insert_entity("both", "alpha beta together")
        rows = _run_fts_search(self.conn, "alpha beta", ["e.status != 'archived'"], [], 10, 0)
        self.assertIsInstance(rows, list)
        self.assertEqual({r[0] for r in rows}, {"both"})


class TestSearchMemoryModeStrictSeam(unittest.TestCase):
    """Controlled-seam integration tests -- patches _run_fts_search/semantic_search so the pool
    is fully deterministic (same pattern as test_topic_rerank.py / test_search_ranking_flags.py)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(self, entity_id: str, memory_type: str = "fact") -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash, memory_type)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', 'raw',"
            " ?, ?, ?, ?)",
            (entity_id, entity_id, f"content for {entity_id}", entity_id, memory_type),
        )
        self.conn.commit()

    def _fts_row(self, entity_id: str) -> tuple:
        return (entity_id, "t", "c", 1, 0, -1.0, "", "", "u", "s", "{}", None, "fact", 0, None)

    def test_strict_mode_default_broad_behavior_unchanged(self):
        """mode omitted (default "broad") must be byte-identical to pre-existing behavior --
        both fts- and semantic-matched candidates returned, no gating."""
        self._insert_entity("a")
        self._insert_entity("b")
        fts_rows = [self._fts_row("a")]
        semantic_rows = [("b", 0.1)]
        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            results = search_memory(query_keywords="q", db_path=self.db_path, include_related=False)
        self.assertEqual({r["id"] for r in results}, {"a", "b"})

    def test_strict_mode_drops_semantic_only_candidate_without_topic_grounding(self):
        """A candidate that only ever surfaces via the semantic channel, with no chunk-level
        topic_score support (rerank_candidates_by_topic returns {} when there are no chunk rows,
        and the B4 entity-level fallback also finds nothing when there's no ready embedding),
        must be dropped under mode="strict" but kept under the "broad" default."""
        self._insert_entity("fts_hit")
        self._insert_entity("semantic_only_no_grounding")
        fts_rows = [self._fts_row("fts_hit")]
        semantic_rows = [("fts_hit", 0.05), ("semantic_only_no_grounding", 0.3)]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
            patch(
                "saltmdb.domain.services.memory_service.rerank_candidates_by_topic",
                return_value={},
            ),
            patch(
                "saltmdb.domain.services.memory_service._batch_semantic_similarities",
                return_value={},
            ),
        ):
            broad_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False
            )
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )

        self.assertEqual(
            {r["id"] for r in broad_results}, {"fts_hit", "semantic_only_no_grounding"}
        )
        self.assertEqual({r["id"] for r in strict_results}, {"fts_hit"})

    def test_strict_mode_keeps_semantic_only_candidate_with_same_specific_topic(self):
        self._insert_entity("fts_hit")
        self._insert_entity("semantic_only_grounded")
        fts_rows = [self._fts_row("fts_hit")]
        semantic_rows = [("fts_hit", 0.05), ("semantic_only_grounded", 0.2)]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
            patch(
                "saltmdb.domain.services.memory_service.rerank_candidates_by_topic",
                return_value={
                    "semantic_only_grounded": {
                        "topic_score": 0.9,
                        "semantic_verdict": "SAME_SPECIFIC_TOPIC",
                    }
                },
            ),
        ):
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )

        self.assertEqual({r["id"] for r in strict_results}, {"fts_hit", "semantic_only_grounded"})

    def test_strict_mode_all_candidates_rejected_returns_empty_list(self):
        """The `[]` case (SALTMDB memory `c27792a1`) -- a genuinely empty, successful result."""
        self._insert_entity("semantic_only_no_grounding")
        semantic_rows = [("semantic_only_no_grounding", 0.3)]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search", return_value=([], False)
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
            patch(
                "saltmdb.domain.services.memory_service.rerank_candidates_by_topic",
                return_value={},
            ),
            patch(
                "saltmdb.domain.services.memory_service._batch_semantic_similarities",
                return_value={},
            ),
        ):
            results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )
        self.assertEqual(results, [])

    def test_history_mode_tags_superseded_without_hiding_or_reordering(self):
        self._insert_entity("older")
        self._insert_entity("newer")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, 'newer', 'older', 'supersedes', NULL)",
            (str(uuid.uuid4()),),
        )
        self.conn.commit()
        fts_rows = [self._fts_row("older"), self._fts_row("newer")]
        semantic_rows = [("older", 0.1), ("newer", 0.2)]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            # prefer_durable_types/demote_superseded pinned False explicitly: this test's subject
            # is mode="history" itself not reordering on its
            # own, independent of demote_superseded's own separate, mode-agnostic reordering
            # (which is real and by design -- see _apply_supersession_demotion) -- pinning False
            # isolates that so the assertion below stays about history mode specifically.
            results = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="history",
                prefer_durable_types=False,
                demote_superseded=False,
            )

        self.assertEqual(
            [r["id"] for r in results], ["older", "newer"], "history mode preserves order"
        )
        older_item = next(r for r in results if r["id"] == "older")
        newer_item = next(r for r in results if r["id"] == "newer")
        self.assertTrue(older_item.get("is_superseded"))
        self.assertNotIn("is_superseded", newer_item)

    def test_unknown_mode_falls_back_to_broad(self):
        self._insert_entity("a")
        fts_rows = [self._fts_row("a")]
        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch("saltmdb.domain.services.memory_service.semantic_search", return_value=[]),
        ):
            results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="bogus"
            )
        self.assertEqual([r["id"] for r in results], ["a"])

    # -- H1 fix: strict-mode gate on FTS OR-fallback matches (memory `ed7cc8d5`) --

    def test_strict_mode_rejects_or_fallback_only_without_topic_grounding(self):
        """An OR-fallback-only candidate (used_or_fallback=True from _run_fts_search) with no
        SAME_SPECIFIC_TOPIC verdict must be dropped under mode="strict" -- this is the exact
        false-accept mechanism H1 fixes (SALTMDB memory `6ee96334`)."""
        self._insert_entity("or_fallback_only")
        fts_rows = [self._fts_row("or_fallback_only")]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, True),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=[],
            ),
            patch(
                "saltmdb.domain.services.memory_service.rerank_candidates_by_topic",
                return_value={},
            ),
            patch(
                "saltmdb.domain.services.memory_service._batch_semantic_similarities",
                return_value={},
            ),
        ):
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )
        self.assertEqual(strict_results, [])

    def test_strict_mode_accepts_or_fallback_only_with_same_specific_topic(self):
        self._insert_entity("or_fallback_grounded")
        fts_rows = [self._fts_row("or_fallback_grounded")]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, True),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=[],
            ),
            patch(
                "saltmdb.domain.services.memory_service._score_topics_with_fallback",
                return_value={
                    "or_fallback_grounded": {
                        "topic_score": 0.9,
                        "semantic_verdict": "SAME_SPECIFIC_TOPIC",
                    }
                },
            ),
        ):
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )
        self.assertEqual({r["id"] for r in strict_results}, {"or_fallback_grounded"})

    def test_strict_mode_or_fallback_plus_semantic_does_not_bypass_dual_channel(self):
        """The exact bypass H1's fix closes, exercised through the full pipeline: an OR-fallback
        match that ALSO appears in the semantic pool must NOT take the dual_channel shortcut --
        it must still be held to the SAME_SPECIFIC_TOPIC bar."""
        self._insert_entity("or_fallback_plus_semantic")
        fts_rows = [self._fts_row("or_fallback_plus_semantic")]
        semantic_rows = [("or_fallback_plus_semantic", 0.2)]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, True),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
            patch(
                "saltmdb.domain.services.memory_service._score_topics_with_fallback",
                return_value={
                    "or_fallback_plus_semantic": {
                        "topic_score": 0.4,
                        "semantic_verdict": "BROADLY_RELATED_THEMES",
                    }
                },
            ),
        ):
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )
        self.assertEqual(
            strict_results,
            [],
            "an OR-fallback + semantic candidate without SAME_SPECIFIC_TOPIC must still be "
            "rejected, not accepted via the dual_channel shortcut",
        )

    def test_broad_mode_unaffected_by_or_fallback(self):
        """Explicit scope boundary: only mode="strict"'s gate changes behavior. broad mode keeps
        an OR-fallback match exactly as before, no gating."""
        self._insert_entity("or_fallback_only")
        fts_rows = [self._fts_row("or_fallback_only")]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, True),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=[],
            ),
        ):
            results = search_memory(query_keywords="q", db_path=self.db_path, include_related=False)
        self.assertEqual({r["id"] for r in results}, {"or_fallback_only"})

    def test_history_mode_unaffected_by_or_fallback(self):
        """Same scope boundary as broad mode: history mode's supersession tagging is unaffected
        by whether the FTS match came via AND or the OR-fallback."""
        self._insert_entity("or_fallback_only")
        fts_rows = [self._fts_row("or_fallback_only")]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, True),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=[],
            ),
        ):
            results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="history"
            )
        self.assertEqual({r["id"] for r in results}, {"or_fallback_only"})

    def test_strict_mode_predecessor_or_fallback_only_grounded_is_accepted(self):
        """Round-2-corrected predecessor path: a predecessor whose own evidence is OR-fallback-only
        must get an on-demand topic-verdict lookup BEFORE predecessor_grounded_map is built (not
        an empty topic map that silently always rejects it) -- SAME_SPECIFIC_TOPIC case accepts
        the resolved head via indirect_grounded_predecessor."""
        self._insert_entity("predecessor")
        self._insert_entity("head")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, 'head', 'predecessor', 'supersedes', NULL)",
            (str(uuid.uuid4()),),
        )
        self.conn.commit()
        fts_rows = [self._fts_row("predecessor")]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, True),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=[],
            ),
            patch(
                "saltmdb.domain.services.memory_service._score_topics_with_fallback",
                return_value={
                    "predecessor": {
                        "topic_score": 0.9,
                        "semantic_verdict": "SAME_SPECIFIC_TOPIC",
                    }
                },
            ),
        ):
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )
        self.assertEqual({r["id"] for r in strict_results}, {"head"})

    def test_strict_mode_predecessor_or_fallback_only_ungrounded_is_rejected(self):
        """Same setup as above, but the on-demand topic-verdict lookup returns a verdict below the
        SAME_SPECIFIC_TOPIC bar -- the resolved head must be dropped, not silently substituted in
        unconditionally."""
        self._insert_entity("predecessor")
        self._insert_entity("head")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, 'head', 'predecessor', 'supersedes', NULL)",
            (str(uuid.uuid4()),),
        )
        self.conn.commit()
        fts_rows = [self._fts_row("predecessor")]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, True),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=[],
            ),
            patch(
                "saltmdb.domain.services.memory_service._score_topics_with_fallback",
                return_value={
                    "predecessor": {
                        "topic_score": 0.3,
                        "semantic_verdict": "BROADLY_RELATED_THEMES",
                    }
                },
            ),
        ):
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )
        self.assertEqual(strict_results, [])

    def test_strict_mode_predecessor_true_and_match_skips_pre_pool_on_demand_scoring(self):
        """Cost-bounding property: a predecessor that is a genuine AND match (used_or_fallback
        False) must NOT trigger the PRE-POOL on-demand topic-scoring call at all -- it already has
        a sufficient DIRECT signal via the existing dual_channel/fts_match rule, so
        predecessor_grounded_map is resolved without ever topic-scoring "predecessor". The main
        pool's own separate ungrounded-id scoring for the substituted "head" (which has no native
        FTS/semantic signal of its own) is unrelated and still legitimately fires -- this test only
        asserts "predecessor" itself is never passed to _score_topics_with_fallback."""
        self._insert_entity("predecessor")
        self._insert_entity("head")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, 'head', 'predecessor', 'supersedes', NULL)",
            (str(uuid.uuid4()),),
        )
        self.conn.commit()
        fts_rows = [self._fts_row("predecessor")]

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=[],
            ),
            patch(
                "saltmdb.domain.services.memory_service._score_topics_with_fallback",
                return_value={},
            ) as mock_score_topics,
        ):
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )
        self.assertEqual({r["id"] for r in strict_results}, {"head"})
        for call in mock_score_topics.call_args_list:
            ids_arg = call.args[1] if len(call.args) > 1 else call.kwargs.get("ids")
            self.assertNotIn(
                "predecessor",
                ids_arg,
                "true-AND pre-pool predecessor must skip the on-demand topic-scoring call",
            )


if __name__ == "__main__":
    unittest.main()
