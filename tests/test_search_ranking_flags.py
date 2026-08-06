"""Tests for Part 2's opt-in search_memory ranking flags (prefer_durable_types,
demote_superseded -- SALTMDB memory 870a1d4e's type-bias / supersession-graph-demotion item,
implemented with the source/target direction corrected during implementation review: `A
supersedes B` demotes the TARGET (B, the old/superseded memory), not the source)."""

import unittest
import tempfile
import os
import shutil
from unittest.mock import patch

import sqlite_vec  # noqa: F401 -- see test_topic_rerank.py; needed for sqlite-vec extension load

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    _apply_supersession_demotion,
    _apply_type_bias,
    search_memory,
)


class TestPart2RankingHelpers(unittest.TestCase):
    """Pure unit tests for _apply_type_bias / _apply_supersession_demotion -- direct sqlite
    fixtures, no search_memory involved."""

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

    def _insert_relation(
        self, source_id: str, target_id: str, predicate: str = "supersedes", valid_to=None
    ) -> None:
        import uuid

        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), source_id, target_id, predicate, valid_to),
        )
        self.conn.commit()

    # -- _apply_type_bias --

    def test_type_bias_empty_list_is_noop(self):
        self.assertEqual(_apply_type_bias([], self.conn), [])

    def test_type_bias_demotes_event_preserving_relative_order(self):
        self._insert_entity("a", "fact")
        self._insert_entity("b", "event")
        self._insert_entity("c", "decision")
        self._insert_entity("d", "event")
        result = _apply_type_bias(["a", "b", "c", "d"], self.conn)
        self.assertEqual(result, ["a", "c", "b", "d"])

    def test_type_bias_noop_when_nothing_is_event_typed(self):
        self._insert_entity("a", "fact")
        self._insert_entity("b", "decision")
        result = _apply_type_bias(["a", "b"], self.conn)
        self.assertEqual(result, ["a", "b"])

    # -- _apply_supersession_demotion --

    def test_supersession_demotion_empty_list_is_noop(self):
        self.assertEqual(_apply_supersession_demotion([], self.conn), [])

    def test_supersession_demotion_demotes_target_not_source(self):
        self._insert_entity("newer")
        self._insert_entity("older")
        self._insert_relation("newer", "older", predicate="supersedes")
        result = _apply_supersession_demotion(["older", "newer"], self.conn)
        self.assertEqual(
            result,
            ["newer", "older"],
            "the TARGET of the supersedes edge (older) must demote, not the source (newer)",
        )

    def test_supersession_demotion_noop_when_nothing_superseded(self):
        self._insert_entity("a")
        self._insert_entity("b")
        result = _apply_supersession_demotion(["a", "b"], self.conn)
        self.assertEqual(result, ["a", "b"])

    def test_supersession_demotion_ignores_no_longer_valid_edge(self):
        """A supersedes edge whose valid_to is in the past (already invalidated/no longer
        current) must not demote its old target -- only a currently-valid edge counts."""
        self._insert_entity("newer")
        self._insert_entity("older")
        self._insert_relation(
            "newer", "older", predicate="supersedes", valid_to="2020-01-01T00:00:00+00:00"
        )
        result = _apply_supersession_demotion(["older", "newer"], self.conn)
        self.assertEqual(result, ["older", "newer"])

    def test_supersession_demotion_ignores_other_predicates(self):
        self._insert_entity("a")
        self._insert_entity("b")
        self._insert_relation("a", "b", predicate="elaborates_on")
        result = _apply_supersession_demotion(["b", "a"], self.conn)
        self.assertEqual(result, ["b", "a"])

    # -- combined ordering (Codex review: type bias first, then supersession demotion) --

    def test_type_bias_then_supersession_demotion_ordering(self):
        """x: fact, not superseded. y: event, not superseded. z: fact, superseded (target of a
        live supersedes edge). Applying type-bias BEFORE supersession-demotion must yield
        [x, y, z] -- the superseded item sinks below the merely-event-typed one. Applying them in
        the reverse order would instead yield [x, z, y], proving order sensitivity."""
        self._insert_entity("x", "fact")
        self._insert_entity("y", "event")
        self._insert_entity("z", "fact")
        self._insert_entity("newer_than_z")
        self._insert_relation("newer_than_z", "z", predicate="supersedes")

        pool = ["x", "y", "z"]
        correct_order = _apply_supersession_demotion(_apply_type_bias(pool, self.conn), self.conn)
        self.assertEqual(correct_order, ["x", "y", "z"])

        reversed_order = _apply_type_bias(_apply_supersession_demotion(pool, self.conn), self.conn)
        self.assertEqual(
            reversed_order,
            ["x", "z", "y"],
            "sanity check that the two orderings genuinely differ on this fixture",
        )


class TestPart2SearchMemorySeam(unittest.TestCase):
    """Controlled-seam integration tests (same style as Part 1's TestRrfGapGateSearchMemorySeam):
    patches the FTS/semantic channels so the pre-partition pool order is deterministic, then
    confirms search_memory actually applies prefer_durable_types / demote_superseded end to end,
    and that both default to False (order unchanged) when omitted."""

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
        return (entity_id, "t", "c", 1, 0, 0, "", "", "u", "s", "{}", None, "fact", 0, None)

    def test_prefer_durable_types_reorders_event_behind_decision(self):
        self._insert_entity("event_entity", "event")
        self._insert_entity("decision_entity", "decision")
        # FTS naturally ranks event_entity first. Semantic channel agrees on the same order (not
        # [] -- an empty semantic result is now retired as a stand-in for "no additional matches";
        # see the P0 fix in memory_service.semantic_search, it must only ever mean a genuinely
        # successful zero-candidate query, not a masked failure) so RRF preserves this ordering.
        fts_rows = [self._fts_row("event_entity"), self._fts_row("decision_entity")]
        semantic_rows = [("event_entity", 0.1), ("decision_entity", 0.5)]

        with (
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            default_results = search_memory(
                query_keywords="type bias seam test", db_path=self.db_path
            )
            biased_results = search_memory(
                query_keywords="type bias seam test",
                prefer_durable_types=True,
                db_path=self.db_path,
            )

        self.assertEqual([r["id"] for r in default_results], ["event_entity", "decision_entity"])
        self.assertEqual([r["id"] for r in biased_results], ["decision_entity", "event_entity"])

    def test_demote_superseded_reorders_superseded_target_behind_current(self):
        self._insert_entity("superseded_entity")
        self._insert_entity("current_entity")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to)"
            " VALUES ('rel-1', 'current_entity', 'superseded_entity', 'supersedes', NULL)"
        )
        self.conn.commit()
        # FTS naturally ranks the (now-superseded) entity first. Semantic channel agrees on the
        # same order (not [] -- see the P0 fix note in the other seam test above) so RRF preserves
        # this ordering.
        fts_rows = [self._fts_row("superseded_entity"), self._fts_row("current_entity")]
        semantic_rows = [("superseded_entity", 0.1), ("current_entity", 0.5)]

        with (
            patch("saltmdb.domain.services.memory_service._run_fts_search", return_value=fts_rows),
            patch(
                "saltmdb.domain.services.memory_service.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            default_results = search_memory(
                query_keywords="supersession seam test", db_path=self.db_path
            )
            demoted_results = search_memory(
                query_keywords="supersession seam test",
                demote_superseded=True,
                db_path=self.db_path,
            )

        self.assertEqual(
            [r["id"] for r in default_results], ["superseded_entity", "current_entity"]
        )
        self.assertEqual(
            [r["id"] for r in demoted_results], ["current_entity", "superseded_entity"]
        )


if __name__ == "__main__":
    unittest.main()
