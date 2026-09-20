import os
import sqlite3
import re
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch

from saltmdb.config import CONTEXT_EXPANSION_CONTRADICTS_CAP
from saltmdb.db.schema import init_db
from saltmdb.domain.services.conflict_set_service import assemble_conflict_sets
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import store_relation


def _memory_id(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        return cast(dict[str, str], result["data"])["id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class TestConflictSetService(unittest.TestCase):
    temp_dir: str = ""
    db_path: str = ""
    conn: sqlite3.Connection = cast(sqlite3.Connection, cast(object, None))

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _memory(self, title: str) -> str:
        return _memory_id(
            store_memory(
                content=f"Conflict-set fixture content for {title} ({uuid.uuid4()})",
                title=title,
                owner_id="conflict-set-test",
                db_connection=self.conn,
            )
        )

    def _relation(self, source_id: str, target_id: str, predicate: str) -> str:
        result = store_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            db_connection=self.conn,
        )
        self.assertIn("successfully stored", result, result)
        return _memory_id(result)

    def _raw_relation(self, source_id: str, target_id: str, predicate: str) -> str:
        relation_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (relation_id, source_id, target_id, predicate, now, now, now),
        )
        self.conn.commit()
        return relation_id

    def _edge(
        self, source_id: str, target_id: str, predicate: str = "contradicts"
    ) -> dict[str, str]:
        return {
            "relation_id": self._raw_relation(source_id, target_id, predicate),
            "source_id": source_id,
            "target_id": target_id,
            "predicate": predicate,
        }

    @staticmethod
    def _expansion(
        edges: list[dict[str, str]], candidates: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "contradicts_edges": edges,
            "expansion_candidates": [{"entity_id": entity_id} for entity_id in (candidates or [])],
        }

    def _archive(self, entity_id: str) -> None:
        self.conn.execute("UPDATE entities SET status='archived' WHERE id=?", (entity_id,))
        self.conn.commit()

    def test_empty_input_returns_empty_conflict_sets_without_opening_connection(self):
        self.assertEqual(
            assemble_conflict_sets(
                {"contradicts_edges": [], "expansion_candidates": []},
                [],
            ),
            {
                "conflict_sets": [],
                "contradicts_cap": {
                    "cap": CONTEXT_EXPANSION_CONTRADICTS_CAP,
                    "eligible_count": 0,
                    "truncated": False,
                    "dropped_count": 0,
                },
            },
        )

    def test_single_unconnected_contradicts_edge_with_no_lineage_is_unresolved(self):
        primary = self._memory("Primary")
        neighbor = self._memory("N's title")
        edge = self._edge(primary, neighbor)

        result = assemble_conflict_sets(
            self._expansion([edge]),
            [{"id": primary, "score": 0.8}],
            db_connection=self.conn,
        )

        members = {member["id"]: member for member in result["conflict_sets"][0]["members"]}
        self.assertEqual(
            members,
            {
                neighbor: {
                    "id": neighbor,
                    "inclusion": "conflict_only",
                    "title": "N's title",
                    "status": "raw",
                },
                primary: {
                    "id": primary,
                    "inclusion": "primary",
                    "title": None,
                    "status": None,
                },
            },
        )
        self.assertEqual(result["conflict_sets"][0]["edges"], [edge])
        self.assertEqual(result["contradicts_cap"]["eligible_count"], 1)

    def test_lifecycle_resolved_via_supersedes_is_excluded(self):
        primary = self._memory("Primary")
        neighbor = self._memory("Superseded")
        edge = self._edge(primary, neighbor)
        self._raw_relation(primary, neighbor, "supersedes")
        self._archive(neighbor)

        result = assemble_conflict_sets(
            self._expansion([edge]),
            [{"id": primary, "score": 1.0}],
            db_connection=self.conn,
        )

        self.assertEqual(result["conflict_sets"], [])
        self.assertEqual(result["contradicts_cap"]["eligible_count"], 0)
        self.assertFalse(result["contradicts_cap"]["truncated"])

    def test_lifecycle_resolved_via_revises_and_via_consolidated_from_is_excluded(self):
        for predicate in ("revises", "consolidated_from"):
            with self.subTest(predicate=predicate):
                primary = self._memory(f"Primary {predicate}")
                neighbor = self._memory(f"Historical {predicate}")
                edge = self._edge(primary, neighbor)
                self._raw_relation(primary, neighbor, predicate)
                self._archive(neighbor)

                result = assemble_conflict_sets(
                    self._expansion([edge]),
                    [{"id": primary, "score": 1.0}],
                    db_connection=self.conn,
                )

                self.assertEqual(result["conflict_sets"], [])
                self.assertEqual(result["contradicts_cap"]["eligible_count"], 0)

    def test_connected_but_not_lifecycle_resolved_zero_non_archived_members_is_kept(self):
        primary = self._memory("Archived primary")
        neighbor = self._memory("Archived neighbor")
        edge = self._edge(primary, neighbor)
        self._raw_relation(primary, neighbor, "supersedes")
        self._archive(primary)
        self._archive(neighbor)

        result = assemble_conflict_sets(
            self._expansion([edge]),
            [{"id": primary, "score": 0.9}],
            db_connection=self.conn,
        )

        self.assertEqual(len(result["conflict_sets"]), 1)
        self.assertEqual(result["conflict_sets"][0]["edges"], [edge])
        self.assertEqual(result["contradicts_cap"]["eligible_count"], 1)

    def test_connected_but_not_lifecycle_resolved_two_non_archived_members_is_kept(self):
        primary = self._memory("Live primary")
        neighbor = self._memory("Live neighbor")
        edge = self._edge(primary, neighbor)
        self._raw_relation(primary, neighbor, "supersedes")

        result = assemble_conflict_sets(
            self._expansion([edge]),
            [{"id": primary, "score": 0.9}],
            db_connection=self.conn,
        )

        self.assertEqual(len(result["conflict_sets"]), 1)
        self.assertEqual(result["conflict_sets"][0]["edges"], [edge])
        self.assertEqual(result["contradicts_cap"]["eligible_count"], 1)

    def test_not_connected_at_all_remains_unresolved(self):
        primary = self._memory("Primary")
        neighbor = self._memory("Neighbor")
        unrelated_primary = self._memory("Unrelated primary lineage")
        unrelated_neighbor = self._memory("Unrelated neighbor lineage")
        edge = self._edge(primary, neighbor)
        self._raw_relation(primary, unrelated_primary, "supersedes")
        self._raw_relation(neighbor, unrelated_neighbor, "revises")

        result = assemble_conflict_sets(
            self._expansion([edge]),
            [{"id": primary, "score": 0.9}],
            db_connection=self.conn,
        )

        self.assertEqual(len(result["conflict_sets"]), 1)
        self.assertEqual(
            {member["id"] for member in result["conflict_sets"][0]["members"]},
            {primary, neighbor},
        )
        self.assertEqual(result["contradicts_cap"]["eligible_count"], 1)

    def test_three_member_component_via_shared_contradicts_endpoint_is_one_set(self):
        primary_one = self._memory("Primary one")
        primary_two = self._memory("Primary two")
        neighbor = self._memory("Shared neighbor")
        edge_one = self._edge(primary_one, neighbor)
        edge_two = self._edge(primary_two, neighbor)

        result = assemble_conflict_sets(
            self._expansion([edge_one, edge_two]),
            [
                {"id": primary_one, "score": 0.9},
                {"id": primary_two, "score": 0.8},
            ],
            db_connection=self.conn,
        )

        self.assertEqual(len(result["conflict_sets"]), 1)
        self.assertEqual(
            {member["id"] for member in result["conflict_sets"][0]["members"]},
            {primary_one, primary_two, neighbor},
        )
        self.assertEqual(result["conflict_sets"][0]["edges"], [edge_one, edge_two])

    def test_conflict_only_vs_expansion_inclusion_precedence(self):
        primary = self._memory("Primary")
        neighbor = self._memory("Expansion neighbor")
        edge = self._edge(primary, neighbor)

        result = assemble_conflict_sets(
            self._expansion([edge], [neighbor]),
            [{"id": primary, "score": 0.8}],
            db_connection=self.conn,
        )

        members = {member["id"]: member for member in result["conflict_sets"][0]["members"]}
        self.assertEqual(
            members[neighbor],
            {
                "id": neighbor,
                "inclusion": "expansion",
                "title": None,
                "status": None,
            },
        )

    def test_primary_beats_expansion_inclusion_precedence(self):
        primary = self._memory("Primary also expansion")
        neighbor = self._memory("Neighbor")
        edge = self._edge(primary, neighbor)

        result = assemble_conflict_sets(
            self._expansion([edge], [primary]),
            [{"id": primary, "score": 0.8}],
            db_connection=self.conn,
        )

        members = {member["id"]: member for member in result["conflict_sets"][0]["members"]}
        self.assertEqual(members[primary]["inclusion"], "primary")
        self.assertIsNone(members[primary]["title"])
        self.assertIsNone(members[primary]["status"])

    def test_cap_truncation_is_whole_set_or_nothing_without_skip_ahead(self):
        cap = CONTEXT_EXPANSION_CONTRADICTS_CAP
        components = []
        primary_hits = []
        for score, suffix, conflict_count in (
            (3.0, "first", cap - 2),
            (2.0, "second", cap - 1),
            (1.0, "third", 1),
        ):
            primary = self._memory(f"Primary {suffix}")
            neighbors = [
                self._memory(f"{suffix} neighbor {index}") for index in range(conflict_count)
            ]
            edges = [self._edge(primary, neighbor) for neighbor in neighbors]
            components.append((primary, neighbors, edges))
            primary_hits.append({"id": primary, "score": score})

        all_edges = [edge for _, _, edges in components for edge in edges]
        result = assemble_conflict_sets(
            self._expansion(all_edges),
            primary_hits,
            db_connection=self.conn,
        )

        self.assertEqual(
            result["contradicts_cap"],
            {
                "cap": cap,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 2,
            },
        )

        self.assertEqual(len(result["conflict_sets"]), 1)
        admitted_ids = {member["id"] for member in result["conflict_sets"][0]["members"]}
        self.assertEqual(admitted_ids, {components[0][0], *components[0][1]})
        dropped_ids = {
            entity_id
            for primary, neighbors, _ in components[1:]
            for entity_id in [primary, *neighbors]
        }
        self.assertTrue(admitted_ids.isdisjoint(dropped_ids))

    def test_tiebreak_uses_max_primary_hit_score_across_set_endpoints(self):
        low_primary = self._memory("Low primary")
        high_primary = self._memory("High primary")
        shared_neighbor = self._memory("Shared neighbor")
        other_primary = self._memory("Other primary")
        other_neighbor = self._memory("Other neighbor")
        first_edge = self._edge(low_primary, shared_neighbor)
        second_edge = self._edge(high_primary, shared_neighbor)
        third_edge = self._edge(other_primary, other_neighbor)

        result = assemble_conflict_sets(
            self._expansion([first_edge, second_edge, third_edge]),
            [
                {"id": low_primary, "score": 0.2},
                {"id": high_primary, "score": 0.9},
                {"id": other_primary, "score": 0.8},
            ],
            db_connection=self.conn,
        )

        self.assertEqual(len(result["conflict_sets"]), 2)
        first_ids = {member["id"] for member in result["conflict_sets"][0]["members"]}
        self.assertEqual(first_ids, {low_primary, high_primary, shared_neighbor})

    def test_deterministic_tertiary_ordering_is_stable(self):
        primary_one = self._memory("Primary one")
        neighbor_one = self._memory("Neighbor one")
        primary_two = self._memory("Primary two")
        neighbor_two = self._memory("Neighbor two")
        edges = [self._edge(primary_one, neighbor_one), self._edge(primary_two, neighbor_two)]
        primary_hits = [
            {"id": primary_one, "score": 0.5},
            {"id": primary_two, "score": 0.5},
        ]

        first = assemble_conflict_sets(
            self._expansion(edges), primary_hits, db_connection=self.conn
        )
        second = assemble_conflict_sets(
            self._expansion(edges), primary_hits, db_connection=self.conn
        )

        first_tuples = [
            tuple(member["id"] for member in conflict_set["members"])
            for conflict_set in first["conflict_sets"]
        ]
        second_tuples = [
            tuple(member["id"] for member in conflict_set["members"])
            for conflict_set in second["conflict_sets"]
        ]
        expected = sorted(
            [
                tuple(sorted((primary_one, neighbor_one))),
                tuple(sorted((primary_two, neighbor_two))),
            ]
        )
        self.assertEqual(first_tuples, expected)
        self.assertEqual(second_tuples, expected)

    def test_merge_diamond_connectivity_requires_multi_round_bfs(self):
        """The pre-fix anchor-only traversal misses the second merged parent."""
        a = self._memory("Merge diamond archived parent A")
        b = self._memory("Merge diamond consolidated entity B")
        d = self._memory("Merge diamond live parent D")
        edge = self._edge(a, d)
        self._raw_relation(b, a, "consolidated_from")
        self._raw_relation(b, d, "consolidated_from")
        self._archive(a)

        result = assemble_conflict_sets(
            self._expansion([edge]),
            [{"id": a, "score": 0.9}],
            db_connection=self.conn,
        )

        self.assertEqual(result["conflict_sets"], [])
        self.assertEqual(result["contradicts_cap"]["eligible_count"], 0)

    def test_missing_entity_row_falls_back_to_unknown(self):
        primary = self._memory("Primary")
        missing = str(uuid.uuid4())
        self.conn.execute("PRAGMA foreign_keys = OFF")
        relation_id = self._raw_relation(primary, missing, "contradicts")
        self.conn.execute("PRAGMA foreign_keys = ON")
        edge = {
            "relation_id": relation_id,
            "source_id": primary,
            "target_id": missing,
            "predicate": "contradicts",
        }

        result = assemble_conflict_sets(
            self._expansion([edge]),
            [{"id": primary, "score": 0.8}],
            db_connection=self.conn,
        )

        members = {member["id"]: member for member in result["conflict_sets"][0]["members"]}
        self.assertEqual(
            members[missing],
            {
                "id": missing,
                "inclusion": "conflict_only",
                "title": "Unknown",
                "status": "unknown",
            },
        )

    def test_anchor_get_lineage_error_path_is_non_fatal(self):
        primary = self._memory("Primary")
        neighbor = self._memory("Neighbor")
        edge = self._edge(primary, neighbor)

        with patch(
            "saltmdb.domain.services.conflict_set_service._get_lineage_raw",
            return_value={"error": "anchor unavailable"},
        ):
            result = assemble_conflict_sets(
                self._expansion([edge]),
                [{"id": primary, "score": 0.8}],
                db_connection=self.conn,
            )

        self.assertEqual(len(result["conflict_sets"]), 1)
        self.assertEqual(result["conflict_sets"][0]["edges"], [edge])
        self.assertEqual(result["contradicts_cap"]["eligible_count"], 1)


if __name__ == "__main__":
    unittest.main()
