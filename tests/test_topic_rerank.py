import unittest
import tempfile
import os
import shutil
import re
import time
from unittest.mock import patch

import sqlite_vec

from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.memory_service import (
    _rrf_gap_confident,
    rerank_candidates_by_topic,
    search_memory,
    semantic_search,
    store_memory,
)
from saltmdb.config import (
    RERANK_SAME_TOPIC_THRESHOLD,
    RERANK_BROAD_THEME_THRESHOLD,
    RERANK_GAP_SKIP_RATIO,
)

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list:
    """Unit basis vector -- cosine(axis_vector(i), axis_vector(j)) is exactly 1.0 if i == j,
    else exactly 0.0 (orthogonal). Gives hand-computable, exact cosine similarities."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _mix_vector(cos_theta: float, dim: int = DIM) -> list:
    """Unit vector whose cosine similarity against axis_vector(0) is exactly cos_theta (up to
    float32 rounding from sqlite_vec's serialize_float32) -- lets a test target a specific,
    known topic_score against real locked thresholds."""
    sin_theta = (1.0 - cos_theta**2) ** 0.5
    v = [0.0] * dim
    v[0] = cos_theta
    v[1] = sin_theta
    return v


def _extract_id(result: str) -> str:
    match = re.search(r"ID:\s*([a-f0-9\-]+)", result)
    assert match, f"Could not parse entity ID from result: {result}"
    return match.group(1)


class TestRerankCandidatesByTopic(unittest.TestCase):
    """Unit tests for rerank_candidates_by_topic (Phase 2 Part B2). Candidate-side chunk vectors
    are inserted directly into entity_chunk_embeddings (axis-aligned, exact cosine similarity by
    construction) and the query-side embedding is fully controlled via a mocked
    embedding_service.embed_texts -- both sides of every comparison are hand-computable, not just
    the DB side."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(self, entity_id: str, content_hash: str, status: str = "raw") -> None:
        """Inserts a bare `entities` row directly (bypassing store_memory's async chunk-embed
        trigger entirely) so the staleness/archived-status tests below control every chunk row
        themselves -- store_memory's real background _embed_pool job would otherwise race with
        the test's own manual INSERT/UPDATE of entity_chunk_embeddings for the same entity_id."""
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', ?, ?, ?, ?)",
            (entity_id, status, entity_id, f"content for {entity_id}", content_hash),
        )
        self.conn.commit()

    def _insert_chunk(
        self, entity_id: str, chunk_index: int, vector: list, content_hash: str = "test-hash"
    ) -> None:
        """Auto-creates a matching `entities` row (INSERT OR IGNORE, so a prior explicit
        `_insert_entity` call for the same id wins) with the same content_hash by default, since
        the reranker's SQL now joins to `entities` and requires `content_hash` equality plus a
        non-archived status (Codex post-implementation-review fix) -- every pre-existing test in
        this class that only cared about vector math still gets an automatically-matching parent
        row for free. Tests that specifically exercise the staleness/archived-status guard call
        `_insert_entity` first with a deliberately different hash or status."""
        self.conn.execute(
            "INSERT OR IGNORE INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', 'raw',"
            " ?, ?, ?)",
            (entity_id, entity_id, f"content for {entity_id}", content_hash),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"{entity_id}-chunk-{chunk_index}",
                entity_id,
                sqlite_vec.serialize_float32(vector),
                chunk_index,
                chunk_index * 1000,
                chunk_index * 1000 + 999,
                content_hash,
            ),
        )
        self.conn.commit()

    def test_single_query_chunk_picks_best_matching_candidate_chunk_not_average(self):
        """Candidate has one chunk identical to the query (cosine 1.0) and one orthogonal chunk
        (cosine 0.0). MIN(distance) must pick the best match (topic_score == 1.0), not average
        the two candidate chunks together (which would give 0.5) -- proves the SQL aggregate
        implements "max over candidate chunks", not "mean over candidate chunks"."""
        entity_id = "candidate-max-not-mean"
        self._insert_chunk(entity_id, 0, _axis_vector(0))
        self._insert_chunk(entity_id, 1, _axis_vector(1))

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
            results = rerank_candidates_by_topic("irrelevant query text", [entity_id], self.db_path)

        self.assertIn(entity_id, results)
        self.assertAlmostEqual(results[entity_id]["topic_score"], 1.0, places=5)

    def test_multi_query_chunk_averages_correctly(self):
        """Two query chunks: one matches the candidate's single chunk perfectly (cosine 1.0), the
        other is orthogonal (cosine 0.0). topic_score must be their mean (0.5), proving the "mean
        over query chunks" half of Mean(Max(...))."""
        entity_id = "candidate-mean-over-query"
        self._insert_chunk(entity_id, 0, _axis_vector(0))

        with patch.object(
            embedding_service, "embed_texts", return_value=[_axis_vector(0), _axis_vector(1)]
        ):
            results = rerank_candidates_by_topic("query text", [entity_id], self.db_path)

        self.assertIn(entity_id, results)
        self.assertAlmostEqual(results[entity_id]["topic_score"], 0.5, places=5)

    def test_candidate_with_zero_chunk_rows_is_absent_not_zero(self):
        """A candidate with no entity_chunk_embeddings rows is simply absent from the returned
        dict -- a documented contract, not a fabricated 0.0 score."""
        with_chunks = "candidate-with-chunks"
        without_chunks = "candidate-without-chunks"
        self._insert_chunk(with_chunks, 0, _axis_vector(0))

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
            results = rerank_candidates_by_topic(
                "query", [with_chunks, without_chunks], self.db_path
            )

        self.assertIn(with_chunks, results)
        self.assertNotIn(without_chunks, results)

    def test_stale_chunk_rows_are_excluded_not_scored(self):
        """Codex post-implementation-review fix: a chunk row whose content_hash no longer
        matches entities.content_hash (simulating a failed/in-flight async refresh, mirrors
        test_chunk_embedding_freshness.py's stale-but-present fixture) must NOT be scorable by
        the reranker, even though it's a perfect vector match. The candidate must come back
        absent from the dict -- the same "not yet chunk-embedded" contract zero-row candidates
        get -- so callers fall through to the _batch_semantic_similarities fallback tier instead
        of silently trusting stale content."""
        entity_id = "stale-chunk-rerank-guard"
        self._insert_entity(entity_id, content_hash="current-content-hash")
        # Perfect match vector -- if staleness weren't filtered, this would score topic_score=1.0.
        self._insert_chunk(
            entity_id, 0, _axis_vector(0), content_hash="stale-content-hash-does-not-match"
        )

        fresh_id = "fresh-control-candidate"
        self._insert_chunk(fresh_id, 0, _axis_vector(0))

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
            results = rerank_candidates_by_topic("query", [entity_id, fresh_id], self.db_path)

        self.assertNotIn(
            entity_id, results, "a candidate with only stale chunk rows must be excluded"
        )
        self.assertIn(fresh_id, results, "control candidate with fresh rows must still score")
        self.assertAlmostEqual(results[fresh_id]["topic_score"], 1.0, places=5)

    def test_archived_entity_chunk_rows_are_excluded_not_scored(self):
        """A candidate whose entity row has been archived must not be scorable by the reranker
        even if its (now-orphaned-in-spirit) chunk rows are still content_hash-current -- mirrors
        A4's "archive never deletes chunk rows" precedent, so the reranker itself is the
        boundary that must not surface archived content, not row deletion."""
        entity_id = "archived-entity-rerank-guard"
        self._insert_entity(entity_id, content_hash="current-content-hash", status="archived")
        self._insert_chunk(entity_id, 0, _axis_vector(0), content_hash="current-content-hash")

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
            results = rerank_candidates_by_topic("query", [entity_id], self.db_path)

        self.assertNotIn(entity_id, results, "an archived candidate must be excluded")

    def test_verdict_tiering_boundaries_against_locked_thresholds(self):
        """Verdict tiers must match B1's real, benchmark-locked threshold constants -- margins
        chosen comfortably wide (0.03) relative to float32 round-trip precision so this doesn't
        flake on rounding, while still exercising both boundaries."""
        cases = [
            ("clearly-same-topic", RERANK_SAME_TOPIC_THRESHOLD + 0.03, "SAME_SPECIFIC_TOPIC"),
            (
                "just-below-same-topic",
                RERANK_SAME_TOPIC_THRESHOLD - 0.03,
                "BROADLY_RELATED_THEMES",
            ),
            (
                "clearly-broad-theme",
                RERANK_BROAD_THEME_THRESHOLD + 0.03,
                "BROADLY_RELATED_THEMES",
            ),
            (
                "just-below-broad-theme",
                RERANK_BROAD_THEME_THRESHOLD - 0.03,
                "DIFFERENT_TOPICS",
            ),
        ]
        for entity_id, cos_theta, _expected in cases:
            self._insert_chunk(entity_id, 0, _mix_vector(cos_theta))

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
            results = rerank_candidates_by_topic("query", [c[0] for c in cases], self.db_path)

        for entity_id, _cos_theta, expected_verdict in cases:
            self.assertIn(entity_id, results)
            self.assertEqual(
                results[entity_id]["semantic_verdict"],
                expected_verdict,
                f"{entity_id}: topic_score={results[entity_id]['topic_score']}",
            )


class TestSearchMemoryRerankRobustness(unittest.TestCase):
    """search_memory(rerank_by_topic=...) integration-level robustness: backward compatibility,
    the missing-chunk-rows fallback tier, and graceful degradation. Real model/real async pool
    (no mocking) -- these test the pipeline wiring, not the scoring math (covered above)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title: str, content: str) -> str:
        res = store_memory(
            title=title,
            content=content,
            owner_id="test_user",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        return _extract_id(res)

    def _poll_for_chunk_rows(self, entity_id: str, tries: int = 50, interval: float = 0.1):
        for _ in range(tries):
            rows = self.conn.execute(
                "SELECT 1 FROM entity_chunk_embeddings WHERE entity_id = ?", (entity_id,)
            ).fetchall()
            if rows:
                return True
            time.sleep(interval)
        return False

    def test_backward_compatible_when_flag_omitted(self):
        self._store(
            "Rerank Backward Compat", "Some real content for the backward compatibility test."
        )
        time.sleep(0.5)

        results = search_memory(query_keywords="backward compatibility test", db_path=self.db_path)
        self.assertTrue(len(results) > 0)
        for item in results:
            self.assertNotIn("topic_score", item)
            self.assertNotIn("semantic_verdict", item)

    def test_rerank_does_not_drop_candidate_with_missing_chunk_rows(self):
        entity_id = self._store(
            "Rerank Fallback Tier",
            "Content whose chunk rows will be deleted to force the fallback tier.",
        )
        self.assertTrue(
            self._poll_for_chunk_rows(entity_id), "chunk rows never appeared before deletion"
        )
        self.conn.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id = ?", (entity_id,))
        self.conn.commit()

        results = search_memory(
            query_keywords="fallback tier chunk rows deleted",
            rerank_by_topic=True,
            db_path=self.db_path,
        )
        self.assertNotIn("error", results[0] if results else {})
        result_ids = [r["id"] for r in results]
        self.assertIn(
            entity_id,
            result_ids,
            "candidate with no chunk rows must still appear via the fallback tier, not vanish",
        )

    def test_search_memory_errors_when_semantic_search_disabled(self):
        """Part 0 (SALTMDB memory 870a1d4e follow-on): FTS-only fallback is retired -- a
        query-bearing search_memory call with semantic search disabled must fail loud (an
        error item), not silently degrade to FTS-only results presented as normal."""
        self._store(
            "Rerank Semantic Disabled", "Content for the semantic-search-disabled degrade test."
        )
        time.sleep(0.5)

        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            results = search_memory(
                query_keywords="semantic disabled degrade test",
                rerank_by_topic=True,
                db_path=self.db_path,
            )

        self.assertTrue(isinstance(results, list))
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("Semantic search is disabled", results[0]["error"])

    def test_search_memory_errors_when_semantic_search_raises(self):
        """P0 fix (Codex implementation review): a genuine semantic-search failure (embedding
        outage, sqlite-vec load failure, a bad vector SQL query, ...) must propagate to an error
        item exactly like the semantic-disabled case above -- never get silently RRF-merged with
        the (real, successful) FTS results and presented as ordinary hybrid results. Before this
        fix, semantic_search() caught the exception internally and returned [], so this call would
        have quietly succeeded with FTS-only results instead of failing loud."""
        self._store(
            "Rerank Semantic Failure", "Content for the semantic-search-raises degrade test."
        )
        time.sleep(0.5)

        with patch(
            "saltmdb.domain.services.memory_service.semantic_search",
            side_effect=RuntimeError("simulated embedding outage"),
        ):
            results = search_memory(
                query_keywords="semantic failure degrade test",
                db_path=self.db_path,
            )

        self.assertTrue(isinstance(results, list))
        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])
        self.assertIn("simulated embedding outage", results[0]["error"])

    def test_empty_query_browsing_unaffected_by_semantic_disabled(self):
        """Filter/tag-only browsing (no query_keywords) never reaches the hybrid pipeline, so it
        must keep working normally even when semantic search is disabled."""
        self._store("Browsable Entry", "Content for the empty-query browsing test.")
        time.sleep(0.5)

        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            results = search_memory(db_path=self.db_path, limit=5)

        self.assertTrue(isinstance(results, list))
        self.assertNotIn("error", results[0] if results else {})


class TestSemanticSearchFailurePropagation(unittest.TestCase):
    """Direct unit test for semantic_search() itself (Codex implementation review, P0, required
    test #2): an embedding/vector failure must raise out of the function, not be caught internally
    and converted to []. See TestSearchMemoryRerankRobustness's
    test_search_memory_errors_when_semantic_search_raises above for the search_memory()-level seam
    proof of the same fix."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_embedding_failure_propagates_instead_of_returning_empty(self):
        with patch(
            "saltmdb.domain.services.embedding_service.embed_text",
            side_effect=RuntimeError("simulated embedding outage"),
        ):
            with self.assertRaises(RuntimeError):
                semantic_search("query text", [], [], limit=5, db_path=self.db_path)


class TestRrfGapConfident(unittest.TestCase):
    """Pure unit tests for Part 1's rerank score-gap gate (_rrf_gap_confident, SALTMDB memory
    870a1d4e). Hand-built rrf_score_map/fts_ids/semantic_ids inputs -- doesn't exercise real RRF
    or the DB at all, purely the gate function's own decision logic."""

    def test_decisive_dual_channel_win_gates_off_rerank(self):
        # top1 present in both channels, ratio 2.0 (both rank-0) clears RERANK_GAP_SKIP_RATIO.
        rrf_score_map = {"a": 2 / 61, "b": 1 / 61}
        self.assertTrue(_rrf_gap_confident(rrf_score_map, {"a", "b"}, {"a", "b"}))

    def test_genuine_tie_falls_through_to_rerank(self):
        # top1 ("a") matched by FTS only, top2 ("b") matched by semantic only -- ratio 1.0.
        rrf_score_map = {"a": 1 / 61, "b": 1 / 61}
        self.assertFalse(_rrf_gap_confident(rrf_score_map, {"a"}, {"b"}))

    def test_high_ratio_alone_is_not_enough_without_dual_channel_top1(self):
        # Ratio alone clears the threshold, but top1 ("a") isn't in fts_ids -- must still gate
        # off (fall through to rerank), proving the dual-channel requirement actually bites
        # independently of the numeric ratio (Codex review: "an RRF ratio alone is not always a
        # universal confidence signal").
        rrf_score_map = {"a": 3.0, "b": 1.0}
        self.assertFalse(_rrf_gap_confident(rrf_score_map, {"b"}, {"a", "b"}))

    def test_single_candidate_cannot_be_gap_confident(self):
        self.assertFalse(_rrf_gap_confident({"a": 1 / 61}, {"a"}, {"a"}))

    def test_zero_or_negative_second_score_cannot_be_gap_confident(self):
        self.assertFalse(_rrf_gap_confident({"a": 1 / 61, "b": 0.0}, {"a", "b"}, {"a", "b"}))

    def test_ratio_just_below_threshold_falls_through(self):
        rrf_score_map = {"a": RERANK_GAP_SKIP_RATIO - 0.01, "b": 1.0}
        self.assertFalse(_rrf_gap_confident(rrf_score_map, {"a", "b"}, {"a", "b"}))


class TestRrfGapGateSearchMemorySeam(unittest.TestCase):
    """Controlled-seam integration test (Codex review): patches the FTS/semantic channels and
    rerank_candidates_by_topic directly so the RRF gap is exactly deterministic, rather than
    relying on real embedding output to land on a precise decisive-vs-ambiguous margin. Proves
    both paths exactly: a decisive dual-channel winner skips rerank_candidates_by_topic entirely;
    an ambiguous (single-channel-per-candidate) result still calls it."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title: str, content: str) -> str:
        res = store_memory(
            title=title,
            content=content,
            owner_id="test_user",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        return _extract_id(res)

    def test_decisive_dual_channel_winner_skips_rerank(self):
        entity_a = self._store("Gap Gate Decisive A", "First entity for the gap gate seam test.")
        entity_b = self._store("Gap Gate Decisive B", "Second entity for the gap gate seam test.")

        # Both channels agree entity_a is rank 0 -> RRF ratio 2.0, dual-channel top1.
        fts_rows = [(entity_a, "t", "c", 1, 0, 0, "", "", "u", "s", "{}", None, "fact", 0, None)]
        semantic_rows = [(entity_a, 0.1), (entity_b, 0.5)]

        with (
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
            patch(
                "saltmdb.domain.services.memory_service.rerank_candidates_by_topic"
            ) as mock_rerank,
        ):
            results = search_memory(
                query_keywords="gap gate seam test",
                rerank_by_topic=True,
                db_path=self.db_path,
            )

        mock_rerank.assert_not_called()
        self.assertNotIn("error", results[0] if results else {})
        for item in results:
            self.assertNotIn("topic_score", item)

    def test_ambiguous_single_channel_result_still_reranks(self):
        entity_a = self._store("Gap Gate Ambiguous A", "First entity for the ambiguous seam test.")
        entity_b = self._store("Gap Gate Ambiguous B", "Second entity for the ambiguous seam test.")

        # entity_a matched by FTS only, entity_b matched by semantic only -> RRF tie, ratio 1.0.
        fts_rows = [(entity_a, "t", "c", 1, 0, 0, "", "", "u", "s", "{}", None, "fact", 0, None)]
        semantic_rows = [(entity_b, 0.1)]

        with (
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
            patch(
                "saltmdb.domain.services.memory_service.rerank_candidates_by_topic",
                return_value={
                    entity_a: {"topic_score": 0.9, "semantic_verdict": "SAME_SPECIFIC_TOPIC"},
                    entity_b: {"topic_score": 0.1, "semantic_verdict": "DIFFERENT_TOPICS"},
                },
            ) as mock_rerank,
        ):
            results = search_memory(
                query_keywords="ambiguous seam test",
                rerank_by_topic=True,
                db_path=self.db_path,
            )

        mock_rerank.assert_called_once()
        self.assertNotIn("error", results[0] if results else {})


class TestRrfGapGateSmoke(unittest.TestCase):
    """Lightweight real-model smoke coverage only (Codex review: real-model tests are retained as
    smoke coverage, not the primary correctness proof for the gate -- see
    TestRrfGapGateSearchMemorySeam above for that). Just confirms rerank_by_topic=True doesn't
    crash and returns sane results against a real (if tiny) corpus."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title: str, content: str) -> str:
        res = store_memory(
            title=title,
            content=content,
            owner_id="test_user",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        return _extract_id(res)

    def test_gap_gate_does_not_crash_real_search(self):
        self._store("Gap Gate Smoke A", "Some real content for the gap gate smoke test.")
        self._store("Gap Gate Smoke B", "Some other real content, a different topic entirely.")
        time.sleep(0.5)

        results = search_memory(
            query_keywords="gap gate smoke test", rerank_by_topic=True, db_path=self.db_path
        )
        self.assertTrue(isinstance(results, list))
        self.assertNotIn("error", results[0] if results else {})


if __name__ == "__main__":
    unittest.main()
