"""Tests for Part C2 (pagination/cursor redesign under mode="strict") --
plans/scalable-strolling-stallman.md.

Covers cursor continuity across a rejection, a substitution, and a many-to-one dedup collapse --
the three interactions the plan explicitly calls out as untested by the happy path. Same
controlled-seam pattern as test_relevance_gate.py: _run_fts_search/semantic_search patched so the
raw candidate pool for a given `candidate_window` size is fully deterministic and hand-computable.
"""

import unittest
import tempfile
import os
import shutil
import uuid
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import search_memory
from saltmdb.config import STRICT_OVERFETCH_CANDIDATE_CAP


class TestStrictModePaginationContinuity(unittest.TestCase):
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
            " full_content, content_hash, memory_type)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', 'raw',"
            " ?, ?, ?, 'fact')",
            (entity_id, entity_id, f"content for {entity_id}", entity_id),
        )
        self.conn.commit()

    def _insert_supersedes(self, source_id: str, target_id: str) -> None:
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, ?, ?, 'supersedes', NULL)",
            (str(uuid.uuid4()), source_id, target_id),
        )
        self.conn.commit()

    def _fts_row(self, entity_id: str) -> tuple:
        return (entity_id, "t", "c", 1, 0, -1.0, "", "", "u", "s", "{}", None, "fact", 0, None)

    def test_pagination_continuity_across_rejection(self):
        """5 candidates all FTS-matched (so all pass the gate) and 5 more that are semantic-only,
        ungrounded (all rejected). offset:0/limit:2 and offset:2/limit:2 must together cover
        exactly the 5 accepted ids, with no skip and no repeat."""
        accepted_ids = [f"good_{i}" for i in range(5)]
        rejected_ids = [f"bad_{i}" for i in range(5)]
        for eid in accepted_ids + rejected_ids:
            self._insert_entity(eid)

        # FTS ranks accepted_ids first (best), then rejected_ids never appear in FTS at all --
        # only in the semantic channel, ungrounded.
        fts_rows = [self._fts_row(eid) for eid in accepted_ids]
        semantic_rows = [(eid, 0.05 * i) for i, eid in enumerate(accepted_ids)] + [
            (eid, 0.5 + 0.01 * i) for i, eid in enumerate(rejected_ids)
        ]

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
            page1 = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                limit=2,
            )
            self.assertTrue(page1)
            cursor = page1[-1]["cursor"]
            page2 = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                limit=2,
                cursor=cursor,
            )
            cursor2 = page2[-1]["cursor"]
            page3 = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                limit=2,
                cursor=cursor2,
            )

        all_ids = [r["id"] for r in page1] + [r["id"] for r in page2] + [r["id"] for r in page3]
        self.assertEqual(len(all_ids), len(set(all_ids)), "no id repeated across pages")
        self.assertEqual(
            set(all_ids), set(accepted_ids), "every accepted id covered, no rejected id leaked in"
        )

    def test_pagination_continuity_across_substitution_and_dedup(self):
        """Two distinct old candidates ('old_a', 'old_b') both resolve to the SAME live head
        ('shared_head') -- a many-to-one dedup collapse. Plus one independent, unrelated accepted
        candidate ('standalone'). The final deduped pool has exactly 2 entries
        (shared_head, standalone); pagination across it must not skip or repeat either."""
        self._insert_entity("old_a")
        self._insert_entity("old_b")
        self._insert_entity("shared_head")
        self._insert_entity("standalone")
        self._insert_supersedes("shared_head", "old_a")
        self._insert_supersedes("shared_head", "old_b")

        fts_rows = [
            self._fts_row("old_a"),
            self._fts_row("old_b"),
            self._fts_row("standalone"),
        ]
        semantic_rows = [("old_a", 0.05), ("old_b", 0.06), ("standalone", 0.07)]

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
            page1 = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                limit=1,
            )
            cursor = page1[-1]["cursor"]
            page2 = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                limit=1,
                cursor=cursor,
            )

        all_ids = [r["id"] for r in page1] + [r["id"] for r in page2]
        self.assertEqual(len(all_ids), len(set(all_ids)), "no id repeated across pages")
        self.assertEqual(set(all_ids), {"shared_head", "standalone"})
        self.assertNotIn("old_a", all_ids)
        self.assertNotIn("old_b", all_ids)

    def test_overfetch_widens_when_gate_shrinks_first_window_below_limit(self):
        """Regression guard for the Part C2 gap (and for a real bug Codex's review caught: an
        earlier version of the overfetch loop broke out early after a single doubling found zero
        ADDITIONAL survivors, which is wrong in general -- real accepted candidates can
        legitimately sit beyond the NEXT window too). 3 genuinely-accepted (semantic-only,
        SAME_SPECIFIC_TOPIC-grounded) candidates sit at semantic rank ~60-62, behind 60 rejected
        (ungrounded) candidates -- reaching them requires widening the window TWICE
        (20 -> 40 -> 80), and the intermediate 40-wide window contains ZERO additional accepted
        survivors versus the first 20-wide window (exactly the shape that would have tripped the
        old, incorrect no-progress early-exit). The mocked `semantic_search` genuinely honors the
        requested `limit` (candidate_window), unlike a naive always-return-everything mock, so this
        test actually exercises windowing rather than accidentally passing regardless of it."""
        accepted_ids = [f"good_{i}" for i in range(3)]
        rejected_ids = [f"bad_{i}" for i in range(60)]
        for eid in accepted_ids + rejected_ids:
            self._insert_entity(eid)

        # All semantic-only (no FTS row at all); rejected ranked ahead of accepted purely by
        # distance, so RRF fusion puts all 60 rejected ids before any accepted id.
        full_semantic_rows = [(eid, 0.001 * i) for i, eid in enumerate(rejected_ids)] + [
            (eid, 0.9 + 0.01 * i) for i, eid in enumerate(accepted_ids)
        ]

        def _semantic_search(_query, _where, _params, limit, _db_path, _offset=0):
            # Real semantic_search respects its own `limit` argument (SQL LIMIT) -- this mock must
            # too, or the test can't actually distinguish "widened enough" from "didn't".
            return full_semantic_rows[:limit]

        def _topic_scores(_query, ids, _db_path):
            return {
                eid: {"topic_score": 0.9, "semantic_verdict": "SAME_SPECIFIC_TOPIC"}
                for eid in ids
                if eid in accepted_ids
            }

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search", return_value=([], False)
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                side_effect=_semantic_search,
            ),
            patch(
                "saltmdb.domain.services.memory_service.rerank_candidates_by_topic",
                side_effect=_topic_scores,
            ),
            patch(
                "saltmdb.domain.services.memory_service._batch_semantic_similarities",
                return_value={},
            ),
        ):
            results = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                limit=3,
            )

        self.assertEqual(
            len(results), 3, "overfetch loop must widen until 3 accepted survivors are found"
        )
        self.assertEqual({r["id"] for r in results}, set(accepted_ids))

    def test_strict_initial_window_clamped_to_overfetch_cap(self):
        """Codex review round-2 P2 finding: base_window = max(offset+limit,
        RERANK_CANDIDATE_POOL_SIZE) can itself already exceed STRICT_OVERFETCH_CANDIDATE_CAP for a
        large requested `limit` -- the loop's own `window < CAP` condition only guards the later
        DOUBLING step, so the very first candidate_window passed to the FTS/semantic channels must
        be clamped too, not just subsequent ones."""
        self._insert_entity("a")
        requested_limit = STRICT_OVERFETCH_CANDIDATE_CAP + 50
        seen_windows = []

        def _semantic_search(_query, _where, _params, limit, _db_path, _offset=0):
            seen_windows.append(limit)
            return []

        with (
            patch(
                "saltmdb.domain.services.memory_service._run_fts_search", return_value=([], False)
            ),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                side_effect=_semantic_search,
            ),
        ):
            search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                limit=requested_limit,
            )

        self.assertTrue(seen_windows, "semantic_search must have been called at least once")
        self.assertLessEqual(
            seen_windows[0],
            STRICT_OVERFETCH_CANDIDATE_CAP,
            "the very first candidate_window must already be clamped to the cap",
        )


if __name__ == "__main__":
    unittest.main()
