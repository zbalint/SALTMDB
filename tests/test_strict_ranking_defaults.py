"""Tests for mode="strict"-only forced ranking defaults + correction-aware demotion --
SALTMDB roadmap `ba2cf66f` P1#6, design `1fddc04a`, plan `plans/amber-sifting-falcon.md`.

Covers: the generalized `_compute_bitemporal_target_ids` helper (both `supersedes` and
`corrects` predicates), `_apply_strict_ranking_defaults`'s stable-partition/union-dedup/
single-`now` behavior, `search_memory(mode="strict")` forcing durable-type preference and
supersession/correction safety demotion regardless of the opt-in flags, and the critical
regression invariant that `broad`/`history` are completely unaffected by any of this. Same
controlled-seam pattern as test_search_ranking_flags.py / test_relevance_gate.py.
"""

import unittest
import tempfile
import os
import shutil
import uuid
from unittest.mock import patch, Mock

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    _apply_strict_ranking_defaults,
    _compute_bitemporal_target_ids,
    _compute_superseded_ids_bitemporal,
    search_memory,
)


class TestComputeBitemporalTargetIds(unittest.TestCase):
    """Generalized helper -- parity with the pre-existing _compute_superseded_ids_bitemporal
    coverage in test_supersession_resolution.py, plus the new `corrects` predicate and
    cross-predicate isolation."""

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

    def _insert_relation(self, source_id, target_id, predicate, **temporal_cols) -> None:
        cols = ["valid_from", "valid_to", "valid_at", "invalid_at"]
        values = [temporal_cols.get(c) for c in cols]
        self.conn.execute(
            f"INSERT INTO relations (id, source_id, target_id, predicate, {', '.join(cols)})"
            f" VALUES (?, ?, ?, ?, {', '.join('?' for _ in cols)})",
            (str(uuid.uuid4()), source_id, target_id, predicate, *values),
        )
        self.conn.commit()

    def test_empty_pool_is_noop(self):
        self.assertEqual(
            _compute_bitemporal_target_ids([], self.conn, "corrects", "2026-01-01T00:00:00+00:00"),
            set(),
        )

    def test_currently_valid_corrects_edge_tags_target(self):
        self._insert_entity("wrong")
        self._insert_entity("right")
        self._insert_relation("right", "wrong", "corrects")
        now = "2026-01-01T00:00:00+00:00"
        self.assertEqual(
            _compute_bitemporal_target_ids(["wrong"], self.conn, "corrects", now), {"wrong"}
        )

    def test_future_valid_at_excludes_corrects_edge(self):
        self._insert_entity("wrong")
        self._insert_entity("right")
        self._insert_relation("right", "wrong", "corrects", valid_at="2099-01-01T00:00:00+00:00")
        now = "2026-01-01T00:00:00+00:00"
        self.assertEqual(
            _compute_bitemporal_target_ids(["wrong"], self.conn, "corrects", now), set()
        )

    def test_past_invalid_at_excludes_corrects_edge(self):
        self._insert_entity("wrong")
        self._insert_entity("right")
        self._insert_relation("right", "wrong", "corrects", invalid_at="2020-01-01T00:00:00+00:00")
        now = "2026-01-01T00:00:00+00:00"
        self.assertEqual(
            _compute_bitemporal_target_ids(["wrong"], self.conn, "corrects", now), set()
        )

    def test_predicate_isolation_supersedes_edge_not_seen_by_corrects_query(self):
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_relation("new", "old", "supersedes")
        now = "2026-01-01T00:00:00+00:00"
        self.assertEqual(_compute_bitemporal_target_ids(["old"], self.conn, "corrects", now), set())
        self.assertEqual(
            _compute_bitemporal_target_ids(["old"], self.conn, "supersedes", now), {"old"}
        )

    def test_thin_wrapper_matches_generalized_helper(self):
        """_compute_superseded_ids_bitemporal must keep behaving exactly like calling the
        generalized helper with predicate='supersedes' -- confirms the refactor preserved its
        existing mode="history" call-site contract."""
        self._insert_entity("old")
        self._insert_entity("new")
        self._insert_relation("new", "old", "supersedes")
        wrapper_result = _compute_superseded_ids_bitemporal(["old"], self.conn)
        self.assertEqual(wrapper_result, {"old"})


class TestApplyStrictRankingDefaults(unittest.TestCase):
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

    def _insert_relation(self, source_id: str, target_id: str, predicate: str) -> None:
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, ?, ?, ?, NULL)",
            (str(uuid.uuid4()), source_id, target_id, predicate),
        )
        self.conn.commit()

    def test_empty_pool_is_noop(self):
        self.assertEqual(_apply_strict_ranking_defaults([], self.conn), [])

    def test_noop_when_nothing_demoted_or_event_typed(self):
        self._insert_entity("a")
        self._insert_entity("b")
        result = _apply_strict_ranking_defaults(["a", "b"], self.conn)
        self.assertEqual(result, ["a", "b"])

    def test_corrects_target_demoted_under_strict_defaults(self):
        self._insert_entity("wrong")
        self._insert_entity("right")
        self._insert_relation("right", "wrong", "corrects")
        result = _apply_strict_ranking_defaults(["wrong", "right"], self.conn)
        self.assertEqual(result, ["right", "wrong"])

    def test_union_dedup_id_both_superseded_and_corrected_demoted_once(self):
        self._insert_entity("both")
        self._insert_entity("newer")
        self._insert_entity("corrector")
        self._insert_relation("newer", "both", "supersedes")
        self._insert_relation("corrector", "both", "corrects")
        result = _apply_strict_ranking_defaults(["both", "newer", "corrector"], self.conn)
        self.assertEqual(result.count("both"), 1)
        self.assertEqual(result[-1], "both", "the double-demoted id must still sink to the back")

    def test_type_bias_applied_before_demotion(self):
        """x: fact, untouched. y: event, untouched. z: fact, corrects-targeted. Type bias runs
        first (Part 2 precedent), so z (a stale fact) must sink below y (a merely event-typed,
        not-known-wrong memory) -- matching _apply_type_bias-then-supersession-demotion's existing
        ordering contract in test_search_ranking_flags.py."""
        self._insert_entity("x", "fact")
        self._insert_entity("y", "event")
        self._insert_entity("z", "fact")
        self._insert_entity("corrector")
        self._insert_relation("corrector", "z", "corrects")
        result = _apply_strict_ranking_defaults(["x", "y", "z"], self.conn)
        self.assertEqual(result, ["x", "y", "z"])

    def test_single_now_shared_across_both_predicate_lookups(self):
        """Codex plan-review round-1 finding: both bitemporal lookups inside
        _apply_strict_ranking_defaults must use exactly one captured `now`, not two independent
        datetime.now() samples. Mock datetime.now to a fixed value and assert it's called exactly
        once."""
        self._insert_entity("a")
        self._insert_entity("b")
        fixed_now = Mock()
        fixed_now.isoformat.return_value = "2026-01-01T00:00:00+00:00"
        with patch("saltmdb.domain.services.memory_service.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            _apply_strict_ranking_defaults(["a", "b"], self.conn)
        self.assertEqual(
            mock_datetime.now.call_count,
            1,
            "now must be captured exactly once and shared across the supersedes and corrects "
            "lookups, not sampled independently per predicate",
        )


class TestSearchMemoryStrictDefaultsSeam(unittest.TestCase):
    """Full search_memory(mode=...) integration -- confirms the forced defaults actually reach
    the pipeline, and that broad/history remain byte-identical (critical regression invariant)."""

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

    def _insert_relation(self, source_id: str, target_id: str, predicate: str) -> None:
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, ?, ?, ?, NULL)",
            (str(uuid.uuid4()), source_id, target_id, predicate),
        )
        self.conn.commit()

    def _fts_row(self, entity_id: str) -> tuple:
        return (entity_id, "t", "c", 1, 0, -1.0, "", "", "u", "s", "{}", None, "fact", 0, None)

    def test_strict_forces_type_bias_and_safety_demotion_without_opt_in_flags(self):
        """prefer_durable_types/demote_superseded both explicitly set to False (independent of
        search_memory's own default, which is True as of v0.1.0-alpha.70) -- mode="strict" must
        still reorder: durable types first, then the corrects-targeted stale item last."""
        self._insert_entity("event_entity", "event")
        self._insert_entity("wrong_fact", "fact")
        self._insert_entity("corrector", "fact")
        self._insert_relation("corrector", "wrong_fact", "corrects")
        fts_rows = [
            self._fts_row("wrong_fact"),
            self._fts_row("event_entity"),
            self._fts_row("corrector"),
        ]
        semantic_rows = [("wrong_fact", 0.1), ("event_entity", 0.2), ("corrector", 0.3)]

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
            strict_results = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                prefer_durable_types=False,
                demote_superseded=False,
            )

        # Type bias runs first (sinks event_entity behind the two durable-typed ids, preserving
        # their relative order: wrong_fact, corrector), then demotion sinks wrong_fact (the
        # corrects target) behind everything else -- same "type bias before demotion" ordering
        # contract as test_search_ranking_flags.py's existing precedent.
        self.assertEqual(
            [r["id"] for r in strict_results],
            ["corrector", "event_entity", "wrong_fact"],
            "durable-type preference AND corrects-demotion must both apply under strict, "
            "unconditionally",
        )

    def test_corrects_only_demoted_under_strict_untouched_under_broad_and_history(self):
        self._insert_entity("wrong")
        self._insert_entity("right")
        self._insert_relation("right", "wrong", "corrects")
        fts_rows = [self._fts_row("wrong"), self._fts_row("right")]
        semantic_rows = [("wrong", 0.1), ("right", 0.2)]

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
            # prefer_durable_types/demote_superseded pinned False explicitly (not omitted): this
            # test's whole point is "untouched under broad and history" regardless of the corrects
            # edge -- pinning False keeps that intent legible now that omission would mean True
            # (v0.1.0-alpha.70 default flip) instead of incidentally not mattering here (neither
            # entity is event-typed, and demote_superseded only checks `supersedes`, not
            # `corrects`, so the True default wouldn't have changed this test's outcome either way
            # -- pinned anyway for clarity, not because the outcome depends on it).
            broad_results = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                prefer_durable_types=False,
                demote_superseded=False,
            )
            history_results = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="history",
                prefer_durable_types=False,
                demote_superseded=False,
            )
            # mode="strict" forces its own durable-type/demotion policy unconditionally,
            # ignoring the caller's own flag values -- omitting them here is intentionally inert.
            strict_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )

        self.assertEqual([r["id"] for r in broad_results], ["wrong", "right"])
        self.assertEqual([r["id"] for r in history_results], ["wrong", "right"])
        self.assertEqual([r["id"] for r in strict_results], ["right", "wrong"])

        # history-mode payload invariance (Codex round-1 finding): a corrects-only edge must not
        # leak any new field into the result item -- this plan explicitly defers history-mode
        # correction tagging (no is_corrected key), unlike its existing is_superseded tagging.
        wrong_item = next(r for r in history_results if r["id"] == "wrong")
        self.assertNotIn("is_corrected", wrong_item)
        self.assertNotIn("is_superseded", wrong_item)

    def test_broad_mode_unaffected_by_presence_of_corrects_edges(self):
        """Explicit regression test (not just an absence-of-new-code argument): mode="broad"'s
        output must be identical whether or not a corrects edge exists in the DB at all."""
        self._insert_entity("wrong")
        self._insert_entity("right")
        fts_rows = [self._fts_row("wrong"), self._fts_row("right")]
        semantic_rows = [("wrong", 0.1), ("right", 0.2)]

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
            before_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False
            )

        self._insert_relation("right", "wrong", "corrects")

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
            after_results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False
            )

        # Full payload comparison, not just ids -- the entities themselves are untouched by the
        # added relation row, so the whole result (score, timestamps, memory_type, etc.) must be
        # byte-identical, not merely same-order.
        self.assertEqual(before_results, after_results)

    def test_strict_defaults_still_apply_when_opt_in_flags_explicitly_passed(self):
        """Passing prefer_durable_types=True/demote_superseded=True alongside mode="strict" must
        not break anything -- the legacy single-hop pass runs first (harmless/idempotent overlap),
        then the new forced pass runs on top."""
        self._insert_entity("wrong")
        self._insert_entity("right")
        self._insert_relation("right", "wrong", "corrects")
        fts_rows = [self._fts_row("wrong"), self._fts_row("right")]
        semantic_rows = [("wrong", 0.1), ("right", 0.2)]

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
            results = search_memory(
                query_keywords="q",
                db_path=self.db_path,
                include_related=False,
                mode="strict",
                prefer_durable_types=True,
                demote_superseded=True,
            )

        self.assertEqual([r["id"] for r in results], ["right", "wrong"])

    def test_residual_superseded_after_resolution_abstain_still_demoted(self):
        """A 2-node supersedes cycle: Part A's resolver abstains (test_cycle_abstains precedent in
        test_supersession_resolution.py), so 'a' stays in the pool under its own id, still
        bitemporally superseded by 'b'. The new safety net must still demote it under
        mode="strict", confirming the two mechanisms compose correctly."""
        self._insert_entity("a")
        self._insert_entity("b")
        self._insert_entity("unrelated")
        self._insert_relation("b", "a", "supersedes")
        self._insert_relation("a", "b", "supersedes")
        fts_rows = [self._fts_row("a"), self._fts_row("unrelated")]
        semantic_rows = [("a", 0.1), ("unrelated", 0.2)]

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
            # prefer_durable_types/demote_superseded omitted -- intentionally inert under
            # mode="strict", which forces its own durable-type/demotion policy unconditionally
            # regardless of the caller's own flag values (or their absence).
            results = search_memory(
                query_keywords="q", db_path=self.db_path, include_related=False, mode="strict"
            )

        self.assertEqual(
            [r["id"] for r in results],
            ["unrelated", "a"],
            "the cycle-abstained node ('a') must still be present (accepted on its own FTS "
            "evidence) but demoted behind the unrelated, non-superseded candidate",
        )

    def test_strict_pagination_continuity_across_demotion_boundary_shift(self):
        """A candidate demoted by the new safety net can shift across an offset/limit page
        boundary -- the next cursor page must show no duplicate and no skip (Part C2's existing
        pagination-continuity guarantee, SALTMDB memory 95a8c5b8, extended to this new demotion
        source)."""
        current_ids = [f"current_{i}" for i in range(4)]
        self._insert_entity("stale")
        self._insert_entity("corrector")
        for eid in current_ids:
            self._insert_entity(eid)
        self._insert_relation("corrector", "stale", "corrects")

        # FTS/semantic both rank 'stale' first (best raw score) -- without the safety net it would
        # be page 1's top result; with it, it must sink to the very back.
        all_ids = ["stale"] + current_ids + ["corrector"]
        fts_rows = [self._fts_row(eid) for eid in all_ids]
        semantic_rows = [(eid, 0.1 * i) for i, eid in enumerate(all_ids)]

        def _search(cursor=None):
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
                return search_memory(
                    query_keywords="q",
                    db_path=self.db_path,
                    include_related=False,
                    mode="strict",
                    limit=3,
                    cursor=cursor,
                )

        page1 = _search()
        page2 = _search(cursor="offset:3")

        page1_ids = [r["id"] for r in page1]
        page2_ids = [r["id"] for r in page2]
        self.assertNotIn("stale", page1_ids, "the demoted item must not appear on page 1")
        combined = page1_ids + page2_ids
        self.assertEqual(len(combined), len(set(combined)), "no duplicate across the two pages")
        self.assertEqual(
            combined[-1], "stale", "the demoted item must surface on page 2, at the very back"
        )


if __name__ == "__main__":
    unittest.main()
