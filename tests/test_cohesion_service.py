import unittest
import tempfile
import os
import shutil
from unittest.mock import patch

import sqlite_vec

from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.cohesion_service import (
    get_fresh_entity_centroids,
    min_pairwise_cohesion,
)

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list:
    """Unit basis vector -- cosine(axis_vector(i), axis_vector(j)) is exactly 1.0 if i == j,
    else exactly 0.0 (orthogonal). Gives hand-computable, exact cosine similarities. Mirrors
    tests/test_topic_rerank.py's helper of the same name/contract."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _mix_vector(cos_theta: float, dim: int = DIM) -> list:
    """Unit vector whose cosine similarity against axis_vector(0) is exactly cos_theta (up to
    float32 rounding from sqlite_vec's serialize_float32)."""
    sin_theta = (1.0 - cos_theta**2) ** 0.5
    v = [0.0] * dim
    v[0] = cos_theta
    v[1] = sin_theta
    return v


class TestCohesionService(unittest.TestCase):
    """Unit tests for the memory-core rework Phase 3 shared primitive (see plans/ and SALTMDB
    memory `5c09effa`). Fresh-path chunk vectors are inserted directly into
    entity_chunk_embeddings (axis-aligned, exact cosine similarity by construction) mirroring
    tests/test_topic_rerank.py's pattern; fallback-path tests control embedding_service.embed_texts
    directly so both paths are fully hand-computable, not just approximately checked."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(
        self, entity_id: str, content_hash: str, status: str = "raw", full_content: str = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', ?, ?, ?, ?)",
            (
                entity_id,
                status,
                entity_id,
                full_content if full_content is not None else f"content for {entity_id}",
                content_hash,
            ),
        )
        self.conn.commit()

    def _insert_chunk(
        self, entity_id: str, chunk_index: int, vector: list, content_hash: str = "test-hash"
    ) -> None:
        """Auto-creates a matching `entities` row (INSERT OR IGNORE) with the same content_hash
        by default -- mirrors tests/test_topic_rerank.py's helper of the same name."""
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

    def test_get_fresh_entity_centroids_uses_persisted_chunk_rows_when_fresh(self):
        """Two orthonormal fresh chunk rows (axis 0, axis 1) for one entity -> centroid must be
        the L2-normalized MEAN of the two, not e.g. just the last-inserted vector: cosine against
        axis_vector(0) should be exactly 1/sqrt(2), not 0.0 or 1.0."""
        self._insert_chunk("e1", 0, _axis_vector(0), content_hash="hash-a")
        self._insert_chunk("e1", 1, _axis_vector(1), content_hash="hash-a")

        centroids, unresolved, observed_state = get_fresh_entity_centroids(
            ["e1"], self.conn, self.db_path
        )

        self.assertEqual(unresolved, {})
        self.assertIn("e1", centroids)
        min_sim, _ = min_pairwise_cohesion({"e1": centroids["e1"], "axis0": _axis_vector(0)})
        self.assertAlmostEqual(min_sim, 0.7071, places=3)

    def test_get_fresh_entity_centroids_falls_back_in_memory_for_stale_or_missing_rows(self):
        """No entity_chunk_embeddings rows at all for this entity -> the fallback path computes
        an on-demand centroid via embedding_service.compute_entity_chunk_embeddings, using a
        mocked embed_texts so the resulting vector is hand-computable."""
        self._insert_entity("e2", content_hash="hash-b", full_content="short raw content")

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
            centroids, unresolved, observed_state = get_fresh_entity_centroids(
                ["e2"], self.conn, self.db_path
            )

        self.assertEqual(unresolved, {})
        self.assertIn("e2", centroids)
        self.assertAlmostEqual(centroids["e2"][0], 1.0, places=4)

    def test_get_fresh_entity_centroids_reports_unresolved_with_reason_on_fallback_failure(self):
        """An entity with empty full_content chunks to zero chunks -> compute_entity_chunk_embeddings
        returns [] -> unresolved carries a real string reason, not just presence in a bare list.

        P1 regression (Codex review bf4qtkp7j / 7a5eba85): this entity's row read succeeded
        (status='raw', an active/non-archived entity) even though embedding it failed -- that
        makes it observed_state, NOT observed_state-less. Dropping observed_state here made a
        valid override_justification unreachable downstream in commit_consolidation, since its
        TOCTOU revalidation hard-rejects any resolved parent with no observed_state entry at
        all. An unscorable-but-active entity must remain override-eligible."""
        self._insert_entity("e3", content_hash="hash-c", full_content="")

        centroids, unresolved, observed_state = get_fresh_entity_centroids(
            ["e3"], self.conn, self.db_path
        )

        self.assertNotIn("e3", centroids)
        self.assertIn("e3", unresolved)
        self.assertIsInstance(unresolved["e3"], str)
        self.assertTrue(len(unresolved["e3"]) > 0)
        self.assertEqual(observed_state["e3"], ("hash-c", "raw"))

    def test_get_fresh_entity_centroids_omits_observed_state_for_archived_unscorable_entity(self):
        """Contrast case for the fix above: an archived entity must NOT get an observed_state
        entry even though its row read succeeds -- archived parents stay hard-rejected, never
        override-eligible, and the missing observed_state entry is what enforces that downstream."""
        self._insert_entity(
            "e3-archived", content_hash="hash-archived", full_content="", status="archived"
        )

        centroids, unresolved, observed_state = get_fresh_entity_centroids(
            ["e3-archived"], self.conn, self.db_path
        )

        self.assertNotIn("e3-archived", centroids)
        self.assertIn("e3-archived", unresolved)
        self.assertNotIn("e3-archived", observed_state)

    def test_get_fresh_entity_centroids_omits_observed_state_when_entity_not_found(self):
        """An id with no entities row at all must stay fully unresolved with no observed_state
        entry -- there is no successful row read to recover state from."""
        centroids, unresolved, observed_state = get_fresh_entity_centroids(
            ["ghost-id"], self.conn, self.db_path
        )

        self.assertNotIn("ghost-id", centroids)
        self.assertEqual(unresolved.get("ghost-id"), "entity not found")
        self.assertNotIn("ghost-id", observed_state)

    def test_get_fresh_entity_centroids_degrades_gracefully_when_vector_extension_fails_to_load(
        self,
    ):
        """P2 (Codex review bf4qtkp7j / 4dc4f8b5): try_load_vector_extension's return value must
        actually gate the vec0 query against entity_chunk_embeddings -- a failed load must not
        let that query raise uncaught. Instead every requested id should fall through to the
        per-entity fallback path, which needs no vec0 access at all."""
        self._insert_chunk("fresh-ext", 0, _axis_vector(0), content_hash="ext-hash")

        with patch(
            "saltmdb.domain.services.cohesion_service.try_load_vector_extension",
            return_value=False,
        ):
            with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
                centroids, unresolved, observed_state = get_fresh_entity_centroids(
                    ["fresh-ext"], self.conn, self.db_path
                )

        # The fresh-join path was skipped entirely, but the fallback path still recovers a
        # centroid via compute_entity_chunk_embeddings/embed_texts -- no uncaught exception.
        self.assertIn("fresh-ext", centroids)
        self.assertEqual(unresolved, {})

    def test_get_fresh_entity_centroids_observed_state_matches_the_exact_read_used_for_the_centroid(
        self,
    ):
        """Directly asserts the A4 TOCTOU-fix invariant: observed_state[id] equals the
        content_hash/status actually present in the row(s) used to build that id's centroid, for
        both the fresh-join and fallback paths."""
        self._insert_chunk("fresh1", 0, _axis_vector(0), content_hash="fresh-hash")

        self._insert_entity("fallback1", content_hash="fallback-hash", full_content="short body")

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(1)]):
            centroids, unresolved, observed_state = get_fresh_entity_centroids(
                ["fresh1", "fallback1"], self.conn, self.db_path
            )

        self.assertEqual(observed_state["fresh1"], ("fresh-hash", "raw"))
        self.assertEqual(observed_state["fallback1"], ("fallback-hash", "raw"))

    def test_min_pairwise_cohesion_trivial_pass_below_two_entities(self):
        self.assertEqual(min_pairwise_cohesion({}), (1.0, None))
        self.assertEqual(min_pairwise_cohesion({"a": _axis_vector(0)}), (1.0, None))

    def test_min_pairwise_cohesion_finds_the_weakest_pair(self):
        """Three centroids with hand-computable pairwise cosines: a-b = cos_theta (0.9), a-c =
        0.0 (orthogonal, the true weakest pair), b-c also < a-b. min_pairwise_cohesion must
        return the true minimum and correctly identify which pair produced it."""
        centroids = {
            "a": _axis_vector(0),
            "b": _mix_vector(0.9),
            "c": _axis_vector(2),
        }
        min_sim, offending_pair = min_pairwise_cohesion(centroids)
        self.assertAlmostEqual(min_sim, 0.0, places=3)
        self.assertEqual(set(offending_pair), {"a", "c"})


if __name__ == "__main__":
    unittest.main()
