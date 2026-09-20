import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from typing import Any, cast
from unittest.mock import patch

from saltmdb import config
from saltmdb.db.schema import init_db
from saltmdb.domain.services.conflict_set_service import assemble_conflict_sets
from saltmdb.domain.services.context_budget_service import pack_context_budget
from saltmdb.domain.services.context_expansion_service import (
    PrimaryHit,
    expand_context_candidates,
)
from saltmdb.domain.services.embedding_service import get_model
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import store_relation


def _memory_id(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        data = cast(dict[str, str], result["data"])
        return data.get("id") or data["relation_id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class TestContextBudgetService(unittest.TestCase):
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

    def _memory(self, title: str, content: str | None = None) -> str:
        return _memory_id(
            store_memory(
                content=content or f"Context budget fixture content for {title} ({uuid.uuid4()})",
                title=title,
                owner_id="context-budget-test",
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
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("successfully stored", result["data"]["message"])
        return _memory_id(result)

    @staticmethod
    def _expansion(candidates: list[str]) -> dict[str, Any]:
        return {"expansion_candidates": [{"entity_id": entity_id} for entity_id in candidates]}

    @staticmethod
    def _conflicts(*members: tuple[str, str]) -> dict[str, Any]:
        return {
            "conflict_sets": [
                {
                    "members": [
                        {"id": entity_id, "inclusion": inclusion}
                        for entity_id, inclusion in members
                    ]
                }
            ]
        }

    def _content(self, entity_id: str) -> str:
        row = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        assert row is not None
        return row[0]

    def test_scenario_1_all_inputs_empty_returns_zero_shape_without_opening_connection(self):
        with patch(
            "saltmdb.domain.services.context_budget_service.get_connection",
            side_effect=AssertionError("empty input must not open a database connection"),
        ):
            result = pack_context_budget({"expansion_candidates": []}, [], {"conflict_sets": []})

        self.assertEqual(
            result,
            {
                "packed_entity_ids": {"primary": [], "expansion": [], "community_member": []},
                "dropped_entity_ids": {"primary": [], "expansion": [], "community_member": []},
                "conflict_only_entity_ids": [],
                "community_representative_entity_ids": [],
                "token_counts": {},
                "budget": {
                    "unit": "tokens",
                    "limit": config.CONTEXT_BUDGET_DEFAULT_TOKENS,
                    "used": 0,
                    "primary_truncated": False,
                    "primary_dropped_count": 0,
                    "expansion_truncated": False,
                    "expansion_dropped_count": 0,
                    "conflict_reserve_tokens_used": 0,
                    "orphan_community_reserve_tokens_used": 0,
                    "community_member_truncated": False,
                    "community_member_dropped_count": 0,
                    "community_representative_reserve_tokens_used": 0,
                },
            },
        )

    def test_scenario_2_everything_fits_under_budget(self):
        primary = [self._memory("Fits primary one"), self._memory("Fits primary two")]
        expansion = [self._memory("Fits expansion one"), self._memory("Fits expansion two")]
        result = pack_context_budget(
            self._expansion(expansion),
            [{"id": entity_id, "score": 1.0} for entity_id in primary],
            {"conflict_sets": []},
            db_connection=self.conn,
        )

        self.assertEqual(
            result["packed_entity_ids"],
            {"primary": primary, "expansion": expansion, "community_member": []},
        )
        self.assertEqual(
            result["dropped_entity_ids"], {"primary": [], "expansion": [], "community_member": []}
        )
        self.assertFalse(result["budget"]["primary_truncated"])
        self.assertFalse(result["budget"]["expansion_truncated"])
        expected_counts = {
            entity_id: get_model().token_count(self._content(entity_id))
            for entity_id in primary + expansion
        }
        self.assertEqual(result["token_counts"], expected_counts)
        self.assertEqual(result["budget"]["used"], sum(expected_counts.values()))

    def test_scenario_3_primary_hits_alone_exceed_budget_and_expansion_is_evaluated(self):
        first_primary = self._memory("Budget primary first")
        second_primary = self._memory("Budget primary second")
        expansion = self._memory(
            "Budget expansion", "A short expansion candidate with nonzero cost."
        )
        first_cost = get_model().token_count(self._content(first_primary))

        result = pack_context_budget(
            self._expansion([expansion]),
            [
                {"id": first_primary, "score": 1.0},
                {"id": second_primary, "score": 0.9},
            ],
            {"conflict_sets": []},
            budget_tokens=first_cost,
            db_connection=self.conn,
        )

        self.assertEqual(result["packed_entity_ids"]["primary"], [first_primary])
        self.assertEqual(result["dropped_entity_ids"]["primary"], [second_primary])
        self.assertTrue(result["budget"]["primary_truncated"])
        self.assertEqual(result["packed_entity_ids"]["expansion"], [])
        self.assertEqual(result["dropped_entity_ids"]["expansion"], [expansion])
        self.assertEqual(result["budget"]["used"], first_cost)

    def test_scenario_4_first_fit_continues_past_a_miss(self):
        large = self._memory(
            "First fit large",
            (
                "# Large candidate\n\n"
                "The larger candidate carries a detailed explanation of graph expansion ordering, "
                "budget accounting, and downstream context assembly.\n\n"
                "Its second paragraph records why a candidate can miss the current budget while "
                "later candidates remain eligible for an independent first-fit decision."
            ),
        )
        small = self._memory("First fit small", "A later small candidate.")
        small_cost = get_model().token_count(self._content(small))

        result = pack_context_budget(
            {"expansion_candidates": []},
            [{"id": large, "score": 1.0}, {"id": small, "score": 0.9}],
            {"conflict_sets": []},
            budget_tokens=small_cost,
            db_connection=self.conn,
        )

        self.assertEqual(result["dropped_entity_ids"]["primary"], [large])
        self.assertEqual(result["packed_entity_ids"]["primary"], [small])
        self.assertEqual(result["budget"]["used"], small_cost)

    def test_scenario_5_conflict_only_members_are_always_included_outside_budget(self):
        primary = self._memory("Conflict reserve primary")
        conflict_only = self._memory(
            "Conflict reserve member",
            (
                "# Conflict reserve member\n\n"
                "This member documents a substantial contradiction that must remain visible even "
                "when the primary context budget has no remaining room for ordinary candidates.\n\n"
                "Its supporting paragraph supplies independent evidence, provenance, and resolution "
                "details for the conflict reserve."
            ),
        )
        result = pack_context_budget(
            {"expansion_candidates": []},
            [{"id": primary, "score": 1.0}],
            self._conflicts((conflict_only, "conflict_only")),
            budget_tokens=0,
            db_connection=self.conn,
        )

        self.assertEqual(
            result["packed_entity_ids"], {"primary": [], "expansion": [], "community_member": []}
        )
        self.assertEqual(result["dropped_entity_ids"]["primary"], [primary])
        self.assertEqual(result["conflict_only_entity_ids"], [conflict_only])
        self.assertGreater(result["token_counts"][conflict_only], 0)
        self.assertEqual(
            result["budget"]["conflict_reserve_tokens_used"], result["token_counts"][conflict_only]
        )
        self.assertEqual(result["budget"]["used"], 0)

    def test_scenario_6_budget_tokens_is_clamped_down_to_maximum(self):
        result = pack_context_budget(
            {"expansion_candidates": []},
            [],
            {"conflict_sets": []},
            budget_tokens=config.CONTEXT_BUDGET_MAX_TOKENS + 100_000,
        )

        self.assertEqual(result["budget"]["limit"], config.CONTEXT_BUDGET_MAX_TOKENS)

    def test_scenario_7_budget_tokens_below_ceiling_is_honored_exactly(self):
        requested_budget = 7
        result = pack_context_budget(
            {"expansion_candidates": []},
            [],
            {"conflict_sets": []},
            budget_tokens=requested_budget,
        )

        self.assertEqual(result["budget"]["limit"], requested_budget)

    def test_scenario_8_budget_tokens_none_resolves_to_default(self):
        result = pack_context_budget(
            {"expansion_candidates": []}, [], {"conflict_sets": []}, budget_tokens=None
        )

        self.assertEqual(result["budget"]["limit"], config.CONTEXT_BUDGET_DEFAULT_TOKENS)

    def test_scenario_9_missing_entity_row_falls_back_to_empty_content_and_zero_tokens(self):
        missing = str(uuid.uuid4())
        result = pack_context_budget(
            {"expansion_candidates": []},
            [{"id": missing, "score": 1.0}],
            {"conflict_sets": []},
            db_connection=self.conn,
        )

        self.assertEqual(result["token_counts"][missing], 0)
        self.assertEqual(result["packed_entity_ids"]["primary"], [missing])
        self.assertEqual(result["budget"]["used"], 0)

    def test_scenario_9_real_empty_entity_row_normalizes_to_zero_tokens(self):
        empty_entity = self._memory("Real empty content")
        self.conn.execute("UPDATE entities SET full_content = '' WHERE id = ?", (empty_entity,))
        self.conn.commit()

        result = pack_context_budget(
            {"expansion_candidates": []},
            [{"id": empty_entity, "score": 1.0}],
            {"conflict_sets": []},
            db_connection=self.conn,
        )

        self.assertEqual(result["token_counts"][empty_entity], 0)
        self.assertEqual(result["packed_entity_ids"]["primary"], [empty_entity])
        self.assertEqual(result["budget"]["used"], 0)

    def test_scenario_10_actual_a1_and_a2_outputs_pack_distinct_pools_end_to_end(self):
        primary = self._memory("Integration primary")
        expansion = self._memory("Integration expansion")
        conflict_only = self._memory("Integration conflict-only")
        self._relation(primary, expansion, "depends_on")
        self._relation(primary, conflict_only, "contradicts")
        primary_hits = [{"id": primary, "score": 1.0}]

        expansion_result = expand_context_candidates(
            cast(list[PrimaryHit], primary_hits), db_connection=self.conn
        )
        expansion_ids = [
            candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
        ]
        conflict_sets_result = assemble_conflict_sets(
            expansion_result, primary_hits, db_connection=self.conn
        )
        conflict_only_ids = [
            member["id"]
            for conflict_set in conflict_sets_result["conflict_sets"]
            for member in conflict_set["members"]
            if member["inclusion"] == "conflict_only"
        ]

        self.assertEqual(expansion_ids, [expansion])
        self.assertEqual(conflict_only_ids, [conflict_only])
        result = pack_context_budget(
            expansion_result,
            primary_hits,
            conflict_sets_result,
            db_connection=self.conn,
        )

        self.assertEqual(result["packed_entity_ids"]["primary"], [primary])
        self.assertEqual(result["packed_entity_ids"]["expansion"], [expansion])
        self.assertEqual(
            result["dropped_entity_ids"], {"primary": [], "expansion": [], "community_member": []}
        )
        self.assertEqual(result["conflict_only_entity_ids"], [conflict_only])
        self.assertEqual(set(result["token_counts"]), {primary, expansion, conflict_only})
        self.assertGreater(result["budget"]["conflict_reserve_tokens_used"], 0)

    def test_scenario_11_community_members_pack_in_order_and_truncate_with_first_fit(self):
        members = [
            self._memory("Community member one", "first community member content."),
            self._memory("Community member two", "second community member content."),
            self._memory("Community member three", "third community member content."),
        ]
        first_two_cost = sum(
            get_model().token_count(self._content(entity_id)) for entity_id in members[:2]
        )

        result = pack_context_budget(
            {"expansion_candidates": []},
            [],
            {"conflict_sets": []},
            budget_tokens=first_two_cost,
            community_member_ids=members,
            db_connection=self.conn,
        )

        self.assertEqual(result["packed_entity_ids"]["community_member"], members[:2])
        self.assertEqual(result["dropped_entity_ids"]["community_member"], [members[2]])
        self.assertTrue(result["budget"]["community_member_truncated"])
        self.assertEqual(result["budget"]["community_member_dropped_count"], 1)
        self.assertEqual(result["budget"]["used"], first_two_cost)

    def test_scenario_12_community_representatives_reserve_tokens_outside_ordinary_budget(self):
        primary = self._memory("Community representative primary", "ordinary primary content.")
        representative = self._memory(
            "Community representative reserve",
            "A representative must remain available outside the ordinary token budget.",
        )

        result = pack_context_budget(
            {"expansion_candidates": []},
            [{"id": primary, "score": 1.0}],
            {"conflict_sets": []},
            budget_tokens=0,
            community_representative_ids={representative},
            db_connection=self.conn,
        )

        self.assertEqual(result["packed_entity_ids"]["primary"], [])
        self.assertEqual(result["dropped_entity_ids"]["primary"], [primary])
        self.assertEqual(result["community_representative_entity_ids"], [representative])
        self.assertGreater(result["token_counts"][representative], 0)
        self.assertEqual(
            result["budget"]["community_representative_reserve_tokens_used"],
            result["token_counts"][representative],
        )
        self.assertEqual(result["budget"]["used"], 0)


if __name__ == "__main__":
    unittest.main()
