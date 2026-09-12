import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch

from saltmdb.config import LINEAGE_HISTORICAL_CAP
from saltmdb.db.schema import init_db
from saltmdb.domain.services.lineage_assembly_service import assemble_lineage
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import get_lineage, store_relation


def _memory_id(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        return cast(dict[str, str], result["data"])["id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class TestLineageAssemblyService(unittest.TestCase):
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
                content=f"Lineage fixture content for {title} ({uuid.uuid4()})",
                title=title,
                owner_id="lineage-test",
                db_connection=self.conn,
            )
        )

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

    def _relation(self, source_id: str, target_id: str, predicate: str) -> str:
        result = store_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            db_connection=self.conn,
        )
        self.assertIn("successfully stored", result, result)
        return _memory_id(result)

    def _edge(
        self, source_id: str, target_id: str, predicate: str = "contradicts"
    ) -> dict[str, str]:
        return {
            "relation_id": self._raw_relation(source_id, target_id, predicate),
            "source_id": source_id,
            "target_id": target_id,
            "predicate": predicate,
        }

    def _archive(self, entity_id: str) -> None:
        self.conn.execute("UPDATE entities SET status='archived' WHERE id=?", (entity_id,))
        self.conn.commit()

    @staticmethod
    def _expansion(
        edges: list[dict[str, str]], candidates: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "contradicts_edges": edges,
            "expansion_candidates": [{"entity_id": entity_id} for entity_id in (candidates or [])],
        }

    def _chain(self, ancestor_count: int, prefix: str = "Chain") -> tuple[str, list[str]]:
        head = self._memory(f"{prefix} head")
        ancestors: list[str] = []
        predecessor = head
        for index in range(ancestor_count):
            ancestor = self._memory(f"{prefix} ancestor {index + 1}")
            self._raw_relation(predecessor, ancestor, "supersedes")
            ancestors.append(ancestor)
            predecessor = ancestor
        return head, ancestors

    def test_no_head_candidates_returns_empty_without_opening_connection(self):
        with patch(
            "saltmdb.domain.services.lineage_assembly_service.get_connection",
            side_effect=AssertionError("empty input must not open a database connection"),
        ):
            result = assemble_lineage(
                {"expansion_candidates": [], "contradicts_edges": []},
                [],
            )

        self.assertEqual(result, {})

    def test_head_candidate_with_no_supersession_history_is_omitted(self):
        head = self._memory("No history head")

        result = assemble_lineage(
            self._expansion([]),
            [{"id": head, "score": 1.0}],
            db_connection=self.conn,
        )

        self.assertNotIn(head, result)
        self.assertEqual(result, {})

    def test_plain_three_ancestor_chain_is_ordered_and_kept_under_cap(self):
        head, ancestors = self._chain(3, "Plain")

        result = assemble_lineage(
            self._expansion([]),
            [{"id": head, "score": 1.0}],
            db_connection=self.conn,
        )

        lineage = result[head]
        self.assertEqual([entry["id"] for entry in lineage["historical"]], ancestors)
        self.assertEqual(
            [entry["included_via"] for entry in lineage["historical"]],
            ["recency"] * 3,
        )
        self.assertFalse(lineage["historical_truncated"])
        self.assertEqual(lineage["historical_dropped_count"], 0)

    def test_eight_ancestor_chain_is_truncated_to_normal_cap(self):
        head, ancestors = self._chain(8, "Capped")

        result = assemble_lineage(
            self._expansion([]), [{"id": head, "score": 1.0}], db_connection=self.conn
        )

        lineage = result[head]
        self.assertEqual(
            [entry["id"] for entry in lineage["historical"]],
            ancestors[:LINEAGE_HISTORICAL_CAP],
        )
        self.assertEqual(len(lineage["historical"]), LINEAGE_HISTORICAL_CAP)
        self.assertTrue(lineage["historical_truncated"])
        self.assertEqual(lineage["historical_dropped_count"], 3)
        self.assertTrue(all(entry["included_via"] == "recency" for entry in lineage["historical"]))

    def test_resolved_contradiction_flags_current_and_ancestor(self):
        head, ancestors = self._chain(1, "Current contradiction")
        ancestor = ancestors[0]
        edge = self._edge(head, ancestor)
        self._archive(ancestor)

        result = assemble_lineage(
            self._expansion([edge]),
            [{"id": head, "score": 1.0}],
            db_connection=self.conn,
        )

        lineage = result[head]
        self.assertEqual(
            {
                "id": head,
                "title": "Current contradiction head",
                "was_flagged_contradiction": True,
                "contradicted_with": [ancestor],
            },
            lineage["current"],
        )
        historical = lineage["historical"]
        self.assertEqual(len(historical), 1)
        self.assertTrue(historical[0]["was_flagged_contradiction"])
        self.assertEqual(historical[0]["contradicted_with"], [head])

    def test_resolved_contradiction_flags_two_historical_entries_only(self):
        head, ancestors = self._chain(3, "Historical contradiction")
        first_endpoint, second_endpoint = ancestors[1:]
        edge = self._edge(first_endpoint, second_endpoint)
        self._archive(second_endpoint)

        result = assemble_lineage(
            self._expansion([edge]),
            [{"id": head, "score": 1.0}],
            db_connection=self.conn,
        )

        lineage = result[head]
        self.assertNotIn("was_flagged_contradiction", lineage["current"])
        historical_by_id = {entry["id"]: entry for entry in lineage["historical"]}
        self.assertNotIn("was_flagged_contradiction", historical_by_id[ancestors[0]])
        self.assertEqual(historical_by_id[first_endpoint]["contradicted_with"], [second_endpoint])
        self.assertEqual(historical_by_id[second_endpoint]["contradicted_with"], [first_endpoint])
        self.assertTrue(historical_by_id[first_endpoint]["was_flagged_contradiction"])
        self.assertTrue(historical_by_id[second_endpoint]["was_flagged_contradiction"])

    def test_beyond_cap_resolved_contradiction_endpoints_are_force_included(self):
        head, ancestors = self._chain(8, "Force include")
        first_endpoint, second_endpoint = ancestors[5:7]
        edge = self._edge(first_endpoint, second_endpoint)
        self._archive(second_endpoint)

        result = assemble_lineage(
            self._expansion([edge]),
            [{"id": head, "score": 1.0}],
            db_connection=self.conn,
        )

        lineage = result[head]
        historical = lineage["historical"]
        self.assertEqual([entry["id"] for entry in historical], ancestors[:7])
        self.assertEqual(len(historical), 7)
        self.assertEqual(lineage["historical_dropped_count"], 1)
        self.assertTrue(lineage["historical_truncated"])
        by_id = {entry["id"]: entry for entry in historical}
        self.assertEqual(
            [by_id[first_endpoint]["included_via"], by_id[second_endpoint]["included_via"]],
            ["contradicts_force_include", "contradicts_force_include"],
        )
        self.assertEqual(by_id[first_endpoint]["contradicted_with"], [second_endpoint])
        self.assertEqual(by_id[second_endpoint]["contradicted_with"], [first_endpoint])

    def test_unresolved_contradiction_is_never_flagged_in_lineage(self):
        head, ancestors = self._chain(3, "Unresolved contradiction")
        first_endpoint, second_endpoint = ancestors[1:]
        edge = self._edge(first_endpoint, second_endpoint)

        result = assemble_lineage(
            self._expansion([edge]),
            [{"id": head, "score": 1.0}],
            db_connection=self.conn,
        )

        lineage = result[head]
        self.assertNotIn("was_flagged_contradiction", lineage["current"])
        self.assertTrue(
            all("was_flagged_contradiction" not in entry for entry in lineage["historical"])
        )
        self.assertTrue(all("contradicted_with" not in entry for entry in lineage["historical"]))

    def test_same_depth_tiebreak_prefers_later_updated_at(self):
        head = self._memory("Merge tiebreak head")
        first = self._memory("Merge tiebreak first")
        second = self._memory("Merge tiebreak second")
        self._raw_relation(head, first, "consolidated_from")
        self._raw_relation(head, second, "consolidated_from")

        lower_id, higher_id = sorted((first, second))
        self.conn.execute(
            "UPDATE entities SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", lower_id),
        )
        self.conn.execute(
            "UPDATE entities SET updated_at=? WHERE id=?",
            ("2025-01-01T00:00:00+00:00", higher_id),
        )
        self.conn.commit()

        with patch("saltmdb.domain.services.lineage_assembly_service.LINEAGE_HISTORICAL_CAP", 1):
            result = assemble_lineage(
                self._expansion([]),
                [{"id": head, "score": 1.0}],
                db_connection=self.conn,
            )

        self.assertEqual([entry["id"] for entry in result[head]["historical"]], [higher_id])
        self.assertEqual(result[head]["historical"][0]["included_via"], "recency")
        self.assertTrue(result[head]["historical_truncated"])
        self.assertEqual(result[head]["historical_dropped_count"], 1)

    def test_get_lineage_error_skips_only_failing_head(self):
        failing_head = self._memory("Failing head")
        working_head, ancestors = self._chain(1, "Working")
        real_get_lineage = get_lineage

        def fake_get_lineage(entity_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if entity_id == failing_head:
                return {"error": "lineage unavailable"}
            return real_get_lineage(entity_id, *args, **kwargs)

        with patch(
            "saltmdb.domain.services.lineage_assembly_service.get_lineage",
            side_effect=fake_get_lineage,
        ):
            result = assemble_lineage(
                self._expansion([]),
                [
                    {"id": failing_head, "score": 1.0},
                    {"id": working_head, "score": 0.9},
                ],
                db_connection=self.conn,
            )

        self.assertNotIn(failing_head, result)
        self.assertIn(working_head, result)
        self.assertEqual([entry["id"] for entry in result[working_head]["historical"]], ancestors)


if __name__ == "__main__":
    unittest.main()
