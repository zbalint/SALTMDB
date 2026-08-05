import unittest
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone, UTC
from unittest.mock import patch
import numpy as np
import sqlite_vec

from saltmdb.db.connection import get_connection
from saltmdb.db.schema import init_db
from saltmdb.db.vector_schema import init_vector_schema
from saltmdb.domain.services import embedding_service
from saltmdb.config import COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION
from saltmdb.domain.services.librarian_service import (
    extract_c_tfidf_tags,
    find_connected_vector_clusters,
    consolidate_vector_clusters,
)

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list:
    """Unit basis vector -- cosine(axis_vector(i), axis_vector(j)) is exactly 1.0 if i == j,
    else exactly 0.0 (orthogonal). Mirrors tests/test_topic_rerank.py's helper of the same
    name/contract."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _mix_vector(cos_theta: float, dim: int = DIM) -> list:
    """Unit vector whose cosine similarity against axis_vector(0) is exactly cos_theta."""
    sin_theta = (1.0 - cos_theta**2) ** 0.5
    v = [0.0] * dim
    v[0] = cos_theta
    v[1] = sin_theta
    return v


def _bridge_vectors():
    """A,B identical (axis0); C shares 0.75 cosine with A/B via axis0 plus an axis2 component;
    D shares only a weak (0.25, below any realistic threshold) axis0 component with A/B but an
    0.828 cosine with C via axis2 -- a single, genuine bridge edge (C-D) pulling one otherwise-
    unrelated node into the same connected component as a cohesive trio."""
    a = _axis_vector(0)
    b = _axis_vector(0)
    c = [0.75, 0.0, 0.6614378277661477] + [0.0] * (DIM - 3)
    d = [0.25, 0.0, 0.9682458365518543] + [0.0] * (DIM - 3)
    return a, b, c, d


class TestConnectedComponentsVectorClustering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_saltmdb.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        if self.conn:
            self.conn.close()
        self.temp_dir.cleanup()

    def test_find_connected_vector_clusters_small_batch(self):
        # 3 near-identical vectors (no background noise)
        v1 = np.ones(384, dtype=np.float32)
        v2 = np.ones(384, dtype=np.float32) * 1.01
        v3 = np.ones(384, dtype=np.float32) * 0.99

        valid_ids = ["e1", "e2", "e3"]
        vectors = [v1, v2, v3]

        clusters = find_connected_vector_clusters(
            valid_ids, vectors, min_cluster_size=3, similarity_threshold=0.75
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0][0]), ["e1", "e2", "e3"])
        self.assertGreaterEqual(clusters[0][1], 0.9)

    def test_find_connected_vector_clusters_off_diagonal_mean(self):
        # 3 vectors with known pairwise similarities
        v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([0.8, 0.6, 0.0], dtype=np.float32)
        v3 = np.array([0.8, 0.0, 0.6], dtype=np.float32)

        valid_ids = ["e1", "e2", "e3"]
        vectors = [v1, v2, v3]

        clusters = find_connected_vector_clusters(
            valid_ids, vectors, min_cluster_size=3, similarity_threshold=0.6
        )
        self.assertEqual(len(clusters), 1)
        # Off-diagonal mean should be around 0.7467 rather than inflated by 1.0 diagonals
        self.assertAlmostEqual(clusters[0][1], 0.7467, places=3)

    def test_find_connected_vector_clusters_extracts_cohesive_subset_from_bridged_component(self):
        """A-B-C mutually cohesive (>=0.75), D bridged into the same connected component only
        via a C-D edge (0.828) just above similarity_threshold, with A-D/B-D well below 0.3 --
        the returned cluster must be exactly {A,B,C}, D dropped entirely (memory-core rework
        Phase 3, Codex correction R3 -- the original single-linkage chaining bug, `3deae748`,
        would have merged all 4 into one proposed cluster instead)."""
        a, b, c, d = _bridge_vectors()
        valid_ids = ["A", "B", "C", "D"]
        vectors = [a, b, c, d]

        clusters = find_connected_vector_clusters(
            valid_ids,
            vectors,
            min_cluster_size=3,
            similarity_threshold=0.75,
            min_pairwise_cohesion=0.70,
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0][0]), ["A", "B", "C"])

    def test_find_connected_vector_clusters_extracts_two_disjoint_groups_from_one_bridged_component(
        self,
    ):
        """{A,B,C} and {E,F,G} are each internally identical (cosine 1.0) but every cross-group
        pair sits at a uniform 0.78 -- above similarity_threshold (so CC merges all 6 into one
        component) but below min_pairwise_cohesion (so extraction must peel BOTH groups out
        separately, not silently keep only one (Codex correction R3 -- multi-subset policy)."""
        group1 = [_axis_vector(0)] * 3
        group2 = [_mix_vector(0.78)] * 3
        valid_ids = ["A", "B", "C", "E", "F", "G"]
        vectors = group1 + group2

        clusters = find_connected_vector_clusters(
            valid_ids,
            vectors,
            min_cluster_size=3,
            similarity_threshold=0.75,
            min_pairwise_cohesion=0.80,
        )
        self.assertEqual(len(clusters), 2)
        cluster_sets = [frozenset(ids) for ids, _ in clusters]
        self.assertIn(frozenset({"A", "B", "C"}), cluster_sets)
        self.assertIn(frozenset({"E", "F", "G"}), cluster_sets)

    def test_find_connected_vector_clusters_extraction_is_deterministic_regardless_of_input_order(
        self,
    ):
        """Runs the same adversarial-tie fixture (uniform 0.78 cross-group ties) through
        find_connected_vector_clusters multiple times with the input entity list permuted
        differently each run -- results must be identical every time (Codex correction R4 --
        the fix must key ordering/tie-break decisions on the permutation-invariant entity_id
        string, not the caller-supplied positional index)."""
        base_ids = ["A", "B", "C", "E", "F", "G"]
        base_vectors = {
            "A": _axis_vector(0),
            "B": _axis_vector(0),
            "C": _axis_vector(0),
            "E": _mix_vector(0.78),
            "F": _mix_vector(0.78),
            "G": _mix_vector(0.78),
        }
        permutations = [
            base_ids,
            list(reversed(base_ids)),
            ["G", "A", "F", "B", "E", "C"],
        ]

        results = []
        for perm in permutations:
            vectors = [base_vectors[eid] for eid in perm]
            clusters = find_connected_vector_clusters(
                perm,
                vectors,
                min_cluster_size=3,
                similarity_threshold=0.75,
                min_pairwise_cohesion=0.80,
            )
            results.append(frozenset(frozenset(ids) for ids, _ in clusters))

        self.assertEqual(len(set(results)), 1, f"results differ across permutations: {results}")
        self.assertEqual(
            results[0], frozenset({frozenset({"A", "B", "C"}), frozenset({"E", "F", "G"})})
        )

    def test_find_connected_vector_clusters_drops_component_with_no_salvageable_subset(self):
        """A component where no subset of size >= min_cluster_size clears min_pairwise_cohesion
        must be dropped entirely, not proposed with an under-threshold cohesion score."""
        a = _axis_vector(0)
        b = _mix_vector(0.5)
        c = [0.5, 0.0, (1.0 - 0.25) ** 0.5] + [0.0] * (DIM - 3)  # cos(a,c)=0.5, cos(b,c)=0.25
        valid_ids = ["A", "B", "C"]
        vectors = [a, b, c]

        clusters = find_connected_vector_clusters(
            valid_ids,
            vectors,
            min_cluster_size=3,
            similarity_threshold=0.5,
            min_pairwise_cohesion=0.9,
        )
        self.assertEqual(clusters, [])

    def test_find_connected_vector_clusters_accepts_genuinely_cohesive_component(self):
        """Regression guard against over-pruning: an already-cohesive component (every pairwise
        similarity comfortably clears min_pairwise_cohesion) must survive intact as one cluster,
        not get needlessly whittled down."""
        a = _axis_vector(0)
        b = _mix_vector(0.97)
        c = _mix_vector(0.95)
        valid_ids = ["A", "B", "C"]
        vectors = [a, b, c]

        clusters = find_connected_vector_clusters(
            valid_ids,
            vectors,
            min_cluster_size=3,
            similarity_threshold=0.75,
            min_pairwise_cohesion=0.80,
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0][0]), ["A", "B", "C"])

    def test_find_connected_vector_clusters_skips_oversized_component_defensively(self):
        """A component larger than COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION must be skipped
        (logged, not proposed) rather than run through the O(k^4)-worst-case extraction, and the
        skip itself must complete quickly."""
        n = COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION + 5
        valid_ids = [f"e{i}" for i in range(n)]
        vectors = [_axis_vector(0) for _ in range(n)]  # all identical -> one giant component

        start = time.monotonic()
        with self.assertLogs("saltmdb.domain.services.librarian_service", level="WARNING") as cm:
            clusters = find_connected_vector_clusters(
                valid_ids, vectors, min_cluster_size=3, similarity_threshold=0.75
            )
        elapsed = time.monotonic() - start

        self.assertEqual(clusters, [])
        self.assertTrue(any("skipping component" in msg for msg in cm.output))
        self.assertLess(elapsed, 2.0, "oversized-component skip must complete quickly")

    def test_extract_c_tfidf_tags(self):
        self.conn.executemany(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content, status) "
            "VALUES (?, datetime('now'), datetime('now'), datetime('now'), ?, ?, 'raw')",
            [
                (
                    "e1",
                    "SQLite WAL Concurrency",
                    "Fixing sqlite wal transaction locking and retries",
                ),
                (
                    "e2",
                    "SQLite Transaction Retries",
                    "Handling wal mode lock contention in sqlite database",
                ),
                (
                    "e3",
                    "SQLite Lock Contention",
                    "Optimizing sqlite wal transaction retries under high concurrency",
                ),
            ],
        )
        self.conn.commit()

        tags, score = extract_c_tfidf_tags(self.conn, ["e1", "e2", "e3"], top_k=3)
        self.assertTrue(len(tags) > 0)
        self.assertIn("#sqlite", [t.lower() for t in tags])
        self.assertGreaterEqual(score, 0.5)


class TestConsolidateVectorClusters(unittest.TestCase):
    """Memory-core rework Phase 3, Part B (see plans/ and SALTMDB memory `5c09effa`):
    consolidate_vector_clusters rewritten onto entity_chunk_embeddings centroids instead of the
    doc-level entity_embeddings table. Real production schema throughout (init_db +
    init_vector_schema), not a hand-rolled plain-table fixture -- a plain table wouldn't require
    the sqlite_vec extension to be loaded, so it couldn't exercise (or catch a regression in)
    the vec0-backed code paths this function actually relies on in production."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_saltmdb.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        if self.conn:
            self.conn.close()
        self.temp_dir.cleanup()

    def _insert_raw_chunk_entity(
        self, entity_id: str, title: str, vector: list, content_hash: str = None
    ) -> str:
        content_hash = content_hash or f"hash-{entity_id}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'shared_owner', 'raw', ?, ?, ?)",
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

    @patch("saltmdb.domain.services.librarian_service.trigger_librarian")
    def test_consolidate_vector_clusters_reads_entity_chunk_embeddings_not_entity_embeddings(
        self, mock_trigger
    ):
        for i, name in enumerate(["Alpha", "Beta", "Gamma"]):
            self._insert_raw_chunk_entity(f"chunk-e{i}", f"Docker Topic {name}", _axis_vector(0))
        consolidate_vector_clusters(self.conn)

        # Never reads/needs the old doc-level entity_embeddings table -- confirm no row was
        # ever written or required there for this to work.
        doc_level_count = self.conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0]
        self.assertEqual(doc_level_count, 0)

        rows = self.conn.execute("SELECT type, content FROM events").fetchall()
        event_types = [r[0] for r in rows]
        self.assertIn("consolidation_request", event_types)

    @patch("saltmdb.domain.services.librarian_service.trigger_librarian")
    def test_consolidate_vector_clusters_excludes_stale_content_hash_chunks(self, mock_trigger):
        for i, name in enumerate(["Alpha", "Beta"]):
            self._insert_raw_chunk_entity(f"fresh-e{i}", f"Cohesive Topic {name}", _axis_vector(0))

        # D's persisted chunk row is STALE (content_hash mismatches entities.content_hash) and
        # deliberately orthogonal -- if the stale row were wrongly used, D would never cluster
        # with the other two. A mocked embed_texts stands in for the fallback recomputation
        # that must kick in instead (real content_hash -> fresh, cohesive vector).
        d_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'shared_owner', 'raw', 'Cohesive Topic Delta',"
            " 'short delta content', 'current-hash')",
            (d_id, now, now, now),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, 0, 0, 10, 'stale-hash')",
            (f"{d_id}::0", d_id, sqlite_vec.serialize_float32(_axis_vector(50))),
        )
        self.conn.commit()

        with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
            consolidate_vector_clusters(self.conn)

        rows = self.conn.execute(
            "SELECT content FROM events WHERE type = 'consolidation_request'"
        ).fetchall()
        self.assertTrue(rows)
        clustered_ids = set()
        for (content_str,) in rows:
            clustered_ids.update(json.loads(content_str)["entity_ids"])
        self.assertIn(
            d_id,
            clustered_ids,
            "the stale cached vector must be ignored in favor of a fresh recompute",
        )

    @patch("saltmdb.domain.services.librarian_service.trigger_librarian")
    def test_consolidate_vector_clusters_excludes_unresolvable_entities_without_failing_the_pass(
        self, mock_trigger
    ):
        for i, name in enumerate(["Alpha", "Beta", "Gamma"]):
            self._insert_raw_chunk_entity(f"ok-e{i}", f"Cohesive Topic {name}", _axis_vector(0))

        unresolvable_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'shared_owner', 'raw', 'Unresolvable Entity', '', 'empty-hash')",
            (unresolvable_id, now, now, now),
        )
        self.conn.commit()

        with self.assertLogs("saltmdb.domain.services.librarian_service", level="INFO") as cm:
            consolidate_vector_clusters(self.conn)

        self.assertTrue(
            any("excluding" in msg and unresolvable_id in msg for msg in cm.output),
            f"expected an excluding-with-reason log line, got: {cm.output}",
        )

        rows = self.conn.execute(
            "SELECT content FROM events WHERE type = 'consolidation_request'"
        ).fetchall()
        self.assertTrue(rows, "the pass must still complete and log the valid cluster")
        clustered_ids = set()
        for (content_str,) in rows:
            clustered_ids.update(json.loads(content_str)["entity_ids"])
        self.assertNotIn(unresolvable_id, clustered_ids)

    @patch("saltmdb.domain.services.librarian_service.trigger_librarian")
    def test_consolidate_vector_clusters_default_conn(self, mock_trigger):
        # Exercises the self-opened connection path (conn=None) against the real vec0 schema.
        self.conn.close()
        real_db_path = os.path.join(self.temp_dir.name, "real_vec0.db")
        conn = init_db(real_db_path)
        init_vector_schema(conn)

        now = datetime.now(timezone.utc).isoformat()
        for i, name in enumerate(["Setup", "Storage", "Networking"]):
            entity_id = f"e{i}"
            conn.execute(
                "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, title, full_content, status, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', ?)",
                (
                    entity_id,
                    now,
                    now,
                    now,
                    "claude",
                    f"Docker Container {name}",
                    f"Configuring Docker containers for WSL2 dev environment ({name})",
                    f"hash-{entity_id}",
                ),
            )
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
                " VALUES (?, ?, ?, 0, 0, 10, ?)",
                (
                    f"{entity_id}::0",
                    entity_id,
                    sqlite_vec.serialize_float32(_axis_vector(0)),
                    f"hash-{entity_id}",
                ),
            )
        conn.commit()
        conn.close()

        consolidate_vector_clusters(conn=None, db_path=real_db_path)

        self.conn = get_connection(real_db_path)
        rows = self.conn.execute(
            "SELECT type, content FROM events ORDER BY timestamp ASC"
        ).fetchall()
        event_types = [r[0] for r in rows]
        self.assertIn("consolidation_request", event_types)


if __name__ == "__main__":
    unittest.main()
