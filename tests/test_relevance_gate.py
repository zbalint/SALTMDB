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
    accept_or_abstain,
    search_memory,
)


class TestAcceptOrAbstain(unittest.TestCase):
    """Pure unit tests -- no DB, no embeddings."""

    def test_dual_channel_accepted(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": True,
            "in_fts": True,
            "in_semantic": True,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "dual_channel")

    def test_fts_only_accepted(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts": True,
            "in_semantic": False,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertTrue(ok)
        self.assertEqual(reason, "fts_match")

    def test_semantic_only_same_specific_topic_accepted(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts": False,
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
            "in_fts": False,
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
            "in_fts": False,
            "in_semantic": True,
            "semantic_verdict": None,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)

    def test_no_evidence_abstains(self):
        evidence = {
            "provenance": "direct",
            "dual_channel": False,
            "in_fts": False,
            "in_semantic": False,
        }
        ok, reason = accept_or_abstain(evidence)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_evidence")

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
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
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
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
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
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
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
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=[]),
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
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="history"
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
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
            patch("saltmdb.domain.services.memory_service.semantic_search", return_value=[]),
        ):
            results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="bogus"
            )
        self.assertEqual([r["id"] for r in results], ["a"])


if __name__ == "__main__":
    unittest.main()
