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
    rerank_candidates_by_topic,
    search_memory,
    store_memory,
)
from saltmdb.config import RERANK_SAME_TOPIC_THRESHOLD, RERANK_BROAD_THEME_THRESHOLD

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

    def test_rerank_degrades_gracefully_when_semantic_search_disabled(self):
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
        self.assertNotIn("error", results[0] if results else {})
        for item in results:
            self.assertNotIn("topic_score", item)


if __name__ == "__main__":
    unittest.main()
