import unittest
import tempfile
import os
import shutil
import json
import math
from unittest.mock import patch

import sqlite_vec

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation, bulk_commit_consolidation
from saltmdb.domain.services.librarian_service import scout_consolidated_supersessions
from saltmdb.domain.services import embedding_service
from saltmdb.config import (
    CLUSTER_MIN_PAIRWISE_THRESHOLD,
    SUPERSESSION_MIN_OVERLAP_COUNT,
    SUPERSESSION_MIN_SIMILARITY_THRESHOLD,
)

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list:
    """Unit basis vector -- cosine(axis_vector(i), axis_vector(j)) is exactly 1.0 if i == j,
    else exactly 0.0 (orthogonal). Mirrors tests/test_cohesion_service.py's helper."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _mix_vector(cos_theta: float, axis: int = 1, base_axis: int = 0, dim: int = DIM) -> list:
    """Unit vector whose cosine similarity against axis_vector(base_axis) is exactly cos_theta,
    with the orthogonal component placed on `axis`. Two mix_vectors sharing the same base_axis
    but different `axis` values are, by construction, cos_theta^2-similar to EACH OTHER (their
    only shared component is base_axis) -- a clean way to build a mutually-cohesive raw-candidate
    cluster with hand-computable numbers. Two mix_vectors with DIFFERENT base_axis and disjoint
    axis values score exactly 0.0 against each other's base_axis, letting distinct
    consolidated-node candidate pools coexist in one test DB without cross-contaminating each
    other's Stage-1 similarity. Mirrors tests/test_cohesion_service.py's _mix_vector, generalized
    with axis/base_axis parameters."""
    sin_theta = (1.0 - cos_theta**2) ** 0.5
    v = [0.0] * dim
    v[base_axis] = cos_theta
    v[axis] = sin_theta
    return v


def _azimuth_vector(cos_theta: float, azimuth_deg: float, dim: int = DIM) -> list:
    """Unit vector at angle theta=arccos(cos_theta) from axis_vector(0), spread around the
    axis-1/axis-2 plane by azimuth_deg. Three such vectors at the same cos_theta but 0/120/240
    degrees apart are each cos_theta-similar to axis_vector(0) (Stage 1) but only
    cos_theta^2 + sin(theta)^2*cos(120deg)-similar to each other (Stage 2) -- the anti-chaining
    fixture from plans/eager-beaming-hippo.md D1/Benchmark, numerically verified against the
    locked threshold per Codex round-1 finding 3."""
    theta = math.acos(cos_theta)
    sin_theta = math.sin(theta)
    v = [0.0] * dim
    v[0] = cos_theta
    v[1] = sin_theta * math.cos(math.radians(azimuth_deg))
    v[2] = sin_theta * math.sin(math.radians(azimuth_deg))
    return v


class TestLibrarianService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_commit_consolidation_soft_archives_parents_and_links_lineage(self):
        res1 = store_memory(
            title="Parent Fact A",
            content="Detailed description of Fact A for testing consolidation",
            owner_id="agent1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        id1 = res1.split("ID: ")[1]

        res2 = store_memory(
            title="Parent Fact B",
            content="Detailed description of Fact B for testing consolidation",
            owner_id="agent1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        id2 = res2.split("ID: ")[1]

        c_res = commit_consolidation(
            parent_ids=[id1, id2],
            title="Consolidated Overview",
            content="Merged summary of A and B",
            tags=["#summary"],
            owner_id="agent1",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", c_res)

        # Verify parent status is archived
        p1 = self.conn.execute(
            "SELECT status, embedding_status FROM entities WHERE id = ?", (id1,)
        ).fetchone()
        p2 = self.conn.execute(
            "SELECT status, embedding_status FROM entities WHERE id = ?", (id2,)
        ).fetchone()
        self.assertEqual(p1[0], "archived")
        self.assertEqual(p1[1], "archived")
        self.assertEqual(p2[0], "archived")
        self.assertEqual(p2[1], "archived")

    def test_bulk_commit_consolidation_is_all_or_nothing(self):
        # Regression test for the bulk-atomicity fix: bulk_commit_consolidation wraps its
        # whole loop in ONE write_transaction_retrying block, so a failure on a later item
        # must roll back an earlier item's would-be-successful insert too -- previously each
        # item committed individually despite the function's docstring claiming atomicity.
        res1 = store_memory(
            title="Bulk Atomicity Parent A",
            content="Detailed description of a parent fact used for the bulk atomicity regression test",
            owner_id="agent1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        id1 = res1.split("ID: ")[1]

        results = bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [id1],
                    "title": "Would-Be Valid Consolidation",
                    "content": "This item is valid on its own and would succeed in isolation",
                },
                {
                    # Deliberately malformed: commit_consolidation already validates and
                    # rejects an empty parent_ids list with "Error: parent_ids must be a
                    # non-empty list of UUID strings."
                    "parent_ids": [],
                    "title": "Malformed Second Item",
                    "content": "This item is deliberately invalid to trigger a batch rollback",
                },
            ],
            db_connection=self.conn,
        )

        # Whole batch reports as a single top-level error, not a mixed success/error list.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

        # The first item's consolidated entity must NOT exist -- proving the whole batch
        # rolled back rather than item 1 silently committing while item 2 failed.
        row = self.conn.execute(
            "SELECT id FROM entities WHERE title = ?", ("Would-Be Valid Consolidation",)
        ).fetchone()
        self.assertIsNone(row)

        # Parent A must still be 'raw' (not archived), since the archiving UPDATE that
        # commit_consolidation performs for item 1 must also have been rolled back.
        p1_status = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (id1,)
        ).fetchone()[0]
        self.assertEqual(p1_status, "raw")


class TestScoutConsolidatedSupersessions(unittest.TestCase):
    """Unit tests for the memory-core rework Phase 4 rewrite of scout_consolidated_supersessions
    (plans/eager-beaming-hippo.md, Codex round-2 approved). Chunk vectors are inserted directly
    into entity_chunk_embeddings via axis-aligned/mix-vector constructions for hand-computable,
    exact cosine similarities -- mirrors tests/test_cohesion_service.py's pattern; fallback-path
    tests mock embedding_service.embed_texts directly for the same reason."""

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
        status: str,
        content_hash: str,
        created_at: str = "2024-01-01T00:00:00",
        valid_from: str = None,
        full_content: str = None,
        owner_id: str = "agent1",
    ) -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash, valid_from)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                created_at,
                created_at,
                created_at,
                owner_id,
                status,
                entity_id,
                full_content if full_content is not None else f"content for {entity_id}",
                content_hash,
                valid_from,
            ),
        )
        self.conn.commit()

    def _insert_chunk(
        self, entity_id: str, vector: list, content_hash: str, chunk_index: int = 0
    ) -> None:
        """Assumes the matching `entities` row already exists (via _insert_entity) -- unlike
        test_cohesion_service.py's helper of the same name, does not auto-create one, since these
        tests need explicit control over status/created_at/valid_from."""
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

    def _consolidation_requests(self, consolidated_entity_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT content FROM events WHERE type = 'consolidation_request'"
        ).fetchall()
        requests = []
        for (content_str,) in rows:
            data = json.loads(content_str)
            if (
                data.get("target") == "supersession_candidate"
                and data.get("consolidated_entity_id") == consolidated_entity_id
            ):
                requests.append(data)
        return requests

    def test_scout_supersession_proposes_on_cohesive_new_raw_cluster_near_stale_consolidated_node(
        self,
    ):
        self._insert_entity(
            "c1",
            "consolidated",
            content_hash="c1-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c1", _axis_vector(0), content_hash="c1-hash")

        for i, entity_id in enumerate(["r1", "r2", "r3"], start=1):
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
            )
            self._insert_chunk(
                entity_id, _mix_vector(0.9, axis=i), content_hash=f"{entity_id}-hash"
            )

        # [R2] Archived raw candidate seeded alongside the qualifying set: excluded by the
        # `WHERE e.status = 'raw'` SQL prefilter before get_fresh_entity_centroids is ever
        # called, so it cannot appear in the resulting centroids/unresolved sets or in the
        # proposal's new_raw_entity_ids -- redundant with the SQL prefilter, folded in here
        # instead of a standalone test per Codex round-1's minor note.
        self._insert_entity(
            "r-archived",
            "archived",
            content_hash="r-archived-hash",
            created_at="2024-06-01T00:00:00",
        )
        self._insert_chunk("r-archived", _axis_vector(0), content_hash="r-archived-hash")

        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        requests = self._consolidation_requests("c1")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["new_raw_entity_ids"], ["r1", "r2", "r3"])
        self.assertNotIn("r-archived", requests[0]["new_raw_entity_ids"])

    def test_scout_supersession_rejects_individually_similar_but_mutually_incohesive_raw_candidates(
        self,
    ):
        """The chaining-fix regression test. Concrete fixture, numerically verified against the
        locked threshold: 3 unit vectors at ~40.87 degrees from the consolidated centroid, spread
        0/120/240 degrees apart. Individually ~0.7562 similar to the centroid (clears Stage 1's
        locked SUPERSESSION_MIN_SIMILARITY_THRESHOLD=0.7557), but only ~0.3577 pairwise (fails
        Stage 2's CLUSTER_MIN_PAIRWISE_THRESHOLD=0.5108 cohesion floor). This is the test that
        would fail against the pre-rewrite implementation."""
        self._insert_entity(
            "c2",
            "consolidated",
            content_hash="c2-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c2", _axis_vector(0), content_hash="c2-hash")

        for azimuth, entity_id in zip([0, 120, 240], ["r1", "r2", "r3"]):
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
            )
            self._insert_chunk(
                entity_id, _azimuth_vector(0.7562, azimuth), content_hash=f"{entity_id}-hash"
            )

        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        self.assertEqual(self._consolidation_requests("c2"), [])

    def test_scout_supersession_falls_back_to_fresh_centroid_on_stale_chunk_row(self):
        """[R2, corrects Codex round-1 finding 2] A candidate with a stale/mismatched persisted
        content_hash chunk row does NOT get excluded -- get_fresh_entity_centroids intentionally
        falls back to computing a fresh centroid on demand for that entity. Each raw candidate's
        PERSISTED chunk row deliberately carries a mismatched content_hash and an orthogonal
        vector that would score 0.0 against the consolidated centroid if it were actually used --
        this can only produce a proposal if the fallback path (mocked embed_texts below) fires
        and computes a fresh centroid instead of the stale cached vector."""
        self._insert_entity(
            "c3",
            "consolidated",
            content_hash="c3-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c3", _axis_vector(0), content_hash="c3-hash")

        for i, entity_id in enumerate(["r1", "r2", "r3"], start=1):
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"fresh-{entity_id}",
                created_at="2024-06-01T00:00:00",
                full_content="short raw content",
            )
            self._insert_chunk(entity_id, _axis_vector(99), content_hash=f"stale-{entity_id}")

        with patch.object(
            embedding_service, "embed_texts", return_value=[_mix_vector(0.9, axis=1)]
        ):
            scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        requests = self._consolidation_requests("c3")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["new_raw_entity_ids"], ["r1", "r2", "r3"])
        # Similarity recorded in the payload matches the freshly-computed fallback vector
        # (~0.9), not the stale cached vector (which would have scored 0.0 -- orthogonal).
        for rid in ["r1", "r2", "r3"]:
            self.assertAlmostEqual(requests[0]["similarity_to_consolidated"][rid], 0.9, places=2)

    def test_scout_supersession_only_considers_raw_created_after_consolidated_valid_from(self):
        self._insert_entity(
            "c4",
            "consolidated",
            content_hash="c4-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-06-01T00:00:00",
        )
        self._insert_chunk("c4", _axis_vector(0), content_hash="c4-hash")

        # r-old predates c4's valid_from -- otherwise similar/cohesive, must still be excluded,
        # leaving only 2 qualifying candidates (below SUPERSESSION_MIN_OVERLAP_COUNT=3).
        self._insert_entity(
            "r-old", "raw", content_hash="r-old-hash", created_at="2024-01-15T00:00:00"
        )
        self._insert_chunk("r-old", _mix_vector(0.9, axis=1), content_hash="r-old-hash")

        for i, entity_id in enumerate(["r-new1", "r-new2"], start=2):
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-07-01T00:00:00",
            )
            self._insert_chunk(
                entity_id, _mix_vector(0.9, axis=i), content_hash=f"{entity_id}-hash"
            )

        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        self.assertEqual(self._consolidation_requests("c4"), [])

    def test_scout_supersession_degrades_gracefully_on_extension_load_failure(self):
        """Mirrors test_cohesion_service.py's extension-degradation test. [R2, minor note] Also
        mocks embedding_service.embed_texts so the fallback path this exercises can never fall
        through to loading the real embedding model -- keeps the test hermetic and fast
        regardless of extension/model availability in the CI environment."""
        self._insert_entity(
            "c5",
            "consolidated",
            content_hash="c5-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
            full_content="short consolidated summary",
        )
        for entity_id in ["r1", "r2", "r3"]:
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
                full_content="short raw content",
            )
        # No entity_chunk_embeddings rows inserted at all -- fresh path is bypassed entirely by
        # the patched extension-load failure below, so every id must go through fallback.

        with patch(
            "saltmdb.domain.services.cohesion_service.try_load_vector_extension",
            return_value=False,
        ):
            with patch.object(embedding_service, "embed_texts", return_value=[_axis_vector(0)]):
                scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        # Every id -- consolidated node and raw candidates alike -- falls through to the same
        # mocked fallback vector, so this still produces a proposal instead of the old bespoke
        # try/except silently abandoning the whole pass on a failed extension load.
        requests = self._consolidation_requests("c5")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["new_raw_entity_ids"], ["r1", "r2", "r3"])

    def test_scout_supersession_idempotent_pending_request_guard(self):
        self._insert_entity(
            "c6",
            "consolidated",
            content_hash="c6-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c6", _axis_vector(0), content_hash="c6-hash")
        for i, entity_id in enumerate(["r1", "r2", "r3"], start=1):
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
            )
            self._insert_chunk(
                entity_id, _mix_vector(0.9, axis=i), content_hash=f"{entity_id}-hash"
            )

        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)
        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        self.assertEqual(len(self._consolidation_requests("c6")), 1)

    def test_scout_supersession_min_overlap_count_boundary(self):
        below_count = SUPERSESSION_MIN_OVERLAP_COUNT - 1
        at_count = SUPERSESSION_MIN_OVERLAP_COUNT

        # Below the floor: no proposal.
        self._insert_entity(
            "c-below",
            "consolidated",
            content_hash="c-below-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c-below", _axis_vector(0), content_hash="c-below-hash")
        for i in range(1, below_count + 1):
            entity_id = f"b{i}"
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
            )
            self._insert_chunk(
                entity_id, _mix_vector(0.9, axis=i), content_hash=f"{entity_id}-hash"
            )

        # Exactly at the floor: one proposal. Anchored on a DIFFERENT centroid (axis 50, with
        # disjoint axis indices from the c-below group) so neither node's candidate pool
        # contaminates the other's Stage-1 similarity.
        self._insert_entity(
            "c-at",
            "consolidated",
            content_hash="c-at-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c-at", _axis_vector(50), content_hash="c-at-hash")
        for i in range(1, at_count + 1):
            entity_id = f"a{i}"
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
            )
            self._insert_chunk(
                entity_id,
                _mix_vector(0.9, axis=50 + i, base_axis=50),
                content_hash=f"{entity_id}-hash",
            )

        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        self.assertEqual(self._consolidation_requests("c-below"), [])
        self.assertEqual(len(self._consolidation_requests("c-at")), 1)

    def test_scout_supersession_event_payload_includes_similarity_and_cohesion_fields(self):
        self._insert_entity(
            "c8",
            "consolidated",
            content_hash="c8-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c8", _axis_vector(0), content_hash="c8-hash")
        for i, entity_id in enumerate(["r1", "r2", "r3"], start=1):
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
            )
            self._insert_chunk(
                entity_id, _mix_vector(0.9, axis=i), content_hash=f"{entity_id}-hash"
            )

        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        requests = self._consolidation_requests("c8")
        self.assertEqual(len(requests), 1)
        payload = requests[0]

        # Pre-existing field names/shapes stay unchanged -- backward compatibility, not just a
        # schema check.
        self.assertEqual(payload["target"], "supersession_candidate")
        self.assertEqual(payload["consolidated_entity_id"], "c8")
        self.assertEqual(payload["consolidated_title"], "c8")
        self.assertEqual(payload["new_raw_entity_ids"], ["r1", "r2", "r3"])

        # New fields, well-formed.
        self.assertEqual(set(payload["similarity_to_consolidated"].keys()), {"r1", "r2", "r3"})
        for score in payload["similarity_to_consolidated"].values():
            self.assertAlmostEqual(score, 0.9, places=2)
        self.assertGreaterEqual(payload["min_intra_raw_cohesion"], CLUSTER_MIN_PAIRWISE_THRESHOLD)
        self.assertEqual(payload["similarity_threshold"], SUPERSESSION_MIN_SIMILARITY_THRESHOLD)
        self.assertEqual(payload["cohesion_threshold"], CLUSTER_MIN_PAIRWISE_THRESHOLD)

    def test_scout_supersession_only_ever_writes_consolidation_request_event(self):
        """[R2, addresses Codex round-1 finding 4] The plan's most important safety invariant,
        with direct coverage: a qualifying run must not mutate entities (status/weight/is_core/
        anything) or relations at all -- the ONLY database write must be the single new
        consolidation_request event. Directly guards the alpha.47 regression this function must
        never reproduce (see memory_service._handle_supersession_candidate's docstring)."""
        self._insert_entity(
            "c9",
            "consolidated",
            content_hash="c9-hash",
            created_at="2024-01-01T00:00:00",
            valid_from="2024-01-01T00:00:00",
        )
        self._insert_chunk("c9", _axis_vector(0), content_hash="c9-hash")
        for i, entity_id in enumerate(["r1", "r2", "r3"], start=1):
            self._insert_entity(
                entity_id,
                "raw",
                content_hash=f"{entity_id}-hash",
                created_at="2024-06-01T00:00:00",
            )
            self._insert_chunk(
                entity_id, _mix_vector(0.9, axis=i), content_hash=f"{entity_id}-hash"
            )

        entities_before = self.conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
        relations_before = self.conn.execute("SELECT * FROM relations ORDER BY id").fetchall()
        events_before_ids = {row[0] for row in self.conn.execute("SELECT id FROM events")}

        scout_consolidated_supersessions(conn=self.conn, db_path=self.db_path)

        entities_after = self.conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
        relations_after = self.conn.execute("SELECT * FROM relations ORDER BY id").fetchall()
        events_after = self.conn.execute("SELECT id, type FROM events").fetchall()

        self.assertEqual(entities_before, entities_after)
        self.assertEqual(relations_before, relations_after)

        new_events = [row for row in events_after if row[0] not in events_before_ids]
        self.assertEqual(len(new_events), 1)
        self.assertEqual(new_events[0][1], "consolidation_request")


if __name__ == "__main__":
    unittest.main()
