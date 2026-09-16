import math
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

import sqlite_vec

from saltmdb import config
from saltmdb.config import (
    COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD,
    CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP,
)
from saltmdb.db.schema import init_db
from saltmdb.domain.services.community_detection_service import recompute_communities
from saltmdb.domain.services.context_budget_service import pack_context_budget
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.embedding_service import get_model
from saltmdb.domain.services.orphan_community_service import find_orphan_community_matches
from saltmdb.domain.services.relation_service import store_relation
from saltmdb.domain.services.retrieve_context_service import assemble_retrieve_context

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


def _cosine_vector(cosine: float, first: int = 0, second: int = 1) -> list[float]:
    vector = [0.0] * DIM
    vector[first] = cosine
    vector[second] = math.sqrt(max(0.0, 1.0 - cosine**2))
    return vector


def _memory_id(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        return cast(dict[str, str], result["data"])["id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class TestOrphanCommunityService(unittest.TestCase):
    temp_dir: str = ""
    db_path: str = ""
    conn: sqlite3.Connection = cast(sqlite3.Connection, cast(object, None))
    _test_mode: Any = None

    def setUp(self):
        self._test_mode = patch.dict(os.environ, {"SALTMDB_TEST_MODE": "1"}, clear=False)
        self._test_mode.start()
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._test_mode.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _memory(self, title: str, content: str | None = None) -> str:
        return _memory_id(
            store_memory(
                content=content or f"Orphan-community fixture content for {title} ({uuid.uuid4()})",
                title=title,
                owner_id="orphan-community-test",
                db_connection=self.conn,
            )
        )

    def _insert_vector(self, entity_id: str, vector: list[float]) -> None:
        self.conn.execute(
            "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (entity_id, sqlite_vec.serialize_float32(vector)),
        )
        self.conn.commit()

    def _insert_community(
        self,
        community_id: str,
        member_ids: list[str],
        centroid: list[float],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO communities (id, representative_entity_id, member_count, level, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (community_id, member_ids[0], len(member_ids), now),
        )
        self.conn.executemany(
            "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, 0)",
            [(entity_id, community_id) for entity_id in member_ids],
        )
        self.conn.execute(
            "INSERT INTO community_embeddings (community_id, embedding) VALUES (?, ?)",
            (community_id, sqlite_vec.serialize_float32(centroid)),
        )
        self.conn.commit()

    @staticmethod
    def _zero_result() -> dict[str, Any]:
        return {
            "orphan_community_matches": [],
            "orphan_community_cap": {
                "cap": CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP,
                "eligible_count": 0,
                "truncated": False,
                "dropped_count": 0,
            },
        }

    @staticmethod
    def _primary(entity_id: str) -> list[dict[str, Any]]:
        return [{"id": entity_id, "score": 1.0}]

    def test_scenario_01_empty_primary_hits_return_zero_shape_without_opening_connection(self):
        with patch(
            "saltmdb.domain.services.orphan_community_service.get_connection",
            side_effect=AssertionError("empty input must not open a database connection"),
        ):
            result = find_orphan_community_matches([], set())

        self.assertEqual(result, self._zero_result())

    def test_scenario_02_primary_hits_with_membership_rows_have_no_orphans(self):
        primary = self._memory("Already assigned primary")
        member = self._memory("Already assigned member")
        self._insert_community("community-assigned", [primary, member], _axis_vector(0))

        result = find_orphan_community_matches(
            self._primary(primary), set(), db_connection=self.conn
        )

        self.assertEqual(result, self._zero_result())

    def test_scenario_03_empty_community_centroids_silently_noop(self):
        orphan = self._memory("No community orphan")

        result = find_orphan_community_matches(
            self._primary(orphan), set(), db_connection=self.conn
        )

        self.assertEqual(result, self._zero_result())

    def test_scenario_04_orphan_above_threshold_admits_community_members(self):
        orphan = self._memory("Above threshold orphan")
        member = self._memory("Above threshold member")
        community_id = "community-above-threshold"
        self._insert_community(community_id, [member], _axis_vector(0))
        self._insert_vector(orphan, _axis_vector(0))
        self._insert_vector(member, _axis_vector(0))

        result = find_orphan_community_matches(
            self._primary(orphan), set(), db_connection=self.conn
        )

        matches = result["orphan_community_matches"]
        self.assertGreater(1.0, COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD)
        self.assertEqual([match["entity_id"] for match in matches], [member])
        self.assertEqual(matches[0]["title"], "Above threshold member")
        self.assertEqual(matches[0]["orphan_entity_id"], orphan)
        self.assertEqual(matches[0]["community_id"], community_id)
        self.assertGreater(matches[0]["similarity"], COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD)
        self.assertEqual(result["orphan_community_cap"]["eligible_count"], 1)

    def test_scenario_05_orphan_below_threshold_is_not_assigned(self):
        orphan = self._memory("Below threshold orphan")
        member = self._memory("Below threshold member")
        self._insert_community("community-below-threshold", [member], _axis_vector(0))
        self._insert_vector(orphan, _axis_vector(1))
        self._insert_vector(member, _axis_vector(0))

        result = find_orphan_community_matches(
            self._primary(orphan), set(), db_connection=self.conn
        )

        self.assertGreater(COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD, 0.0)
        self.assertEqual(result, self._zero_result())

    def test_scenario_06_members_rank_by_similarity_to_specific_orphan(self):
        orphan = self._memory("Specific orphan anchor")
        centroid_similarity = (COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD + 1.0) / 2.0
        centroid = _cosine_vector(centroid_similarity)
        centroid_closest = self._memory("Centroid-closest member")
        orphan_closest = self._memory("Orphan-closest member")
        community_id = "community-specific-orphan"
        self._insert_community(
            community_id,
            [centroid_closest, orphan_closest],
            centroid,
        )
        self._insert_vector(orphan, _axis_vector(0))
        self._insert_vector(centroid_closest, centroid)
        self._insert_vector(orphan_closest, _axis_vector(0))

        result = find_orphan_community_matches(
            self._primary(orphan), set(), db_connection=self.conn
        )

        self.assertEqual(
            [match["entity_id"] for match in result["orphan_community_matches"]],
            [orphan_closest, centroid_closest],
        )

    def test_scenario_07_excluded_ids_are_never_admitted(self):
        orphan = self._memory("Excluded-id orphan")
        excluded = self._memory("Excluded highest-ranked member")
        admitted = self._memory("Excluded fallback member")
        community_id = "community-exclusion"
        self._insert_community(community_id, [excluded, admitted], _axis_vector(0))
        self._insert_vector(orphan, _axis_vector(0))
        self._insert_vector(excluded, _axis_vector(0))
        self._insert_vector(admitted, _axis_vector(1))

        result = find_orphan_community_matches(
            self._primary(orphan), {excluded}, db_connection=self.conn
        )

        ids = [match["entity_id"] for match in result["orphan_community_matches"]]
        self.assertNotIn(excluded, ids)
        self.assertEqual(ids, [admitted])

    def test_scenario_08_global_cap_applies_across_multiple_orphan_hits(self):
        cap = CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP
        self.assertGreater(cap, 0)
        orphan_a = self._memory("Global-cap orphan A")
        orphan_b = self._memory("Global-cap orphan B")
        members_a = [self._memory(f"Global-cap A member {index}") for index in range(cap)]
        members_b = [self._memory(f"Global-cap B member {index}") for index in range(cap)]
        self._insert_community("community-global-a", members_a, _axis_vector(0))
        self._insert_community("community-global-b", members_b, _axis_vector(1))
        self._insert_vector(orphan_a, _axis_vector(0))
        self._insert_vector(orphan_b, _axis_vector(1))
        for member in members_a:
            self._insert_vector(member, _axis_vector(0))
        for member in members_b:
            self._insert_vector(member, _axis_vector(1))

        result = find_orphan_community_matches(
            [
                {"id": orphan_a, "score": 1.0},
                {"id": orphan_b, "score": 0.9},
            ],
            set(),
            db_connection=self.conn,
        )

        cap_result = result["orphan_community_cap"]
        self.assertEqual(len(result["orphan_community_matches"]), cap)
        self.assertEqual(cap_result["cap"], cap)
        self.assertEqual(cap_result["eligible_count"], cap * 2)
        self.assertTrue(cap_result["truncated"])
        self.assertEqual(cap_result["dropped_count"], cap)

    def test_scenario_09_shared_member_dedupes_to_higher_scoring_orphan_anchor(self):
        orphan_high = self._memory("Higher-scoring orphan anchor")
        orphan_low = self._memory("Lower-scoring orphan anchor")
        shared = self._memory("Shared reachable member")
        community_id = "community-shared-member"
        self._insert_community(community_id, [shared], _axis_vector(0))
        self._insert_vector(orphan_high, _axis_vector(0))
        self._insert_vector(
            orphan_low,
            _cosine_vector((COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD + 1.0) / 2.0),
        )
        self._insert_vector(shared, _axis_vector(0))

        result = find_orphan_community_matches(
            [
                {"id": orphan_low, "score": 1.0},
                {"id": orphan_high, "score": 0.9},
            ],
            set(),
            db_connection=self.conn,
        )

        matches = [
            match for match in result["orphan_community_matches"] if match["entity_id"] == shared
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["orphan_entity_id"], orphan_high)
        self.assertGreater(matches[0]["similarity"], 0.99)

    def test_scenario_10_missing_member_embedding_is_skipped_without_error(self):
        orphan = self._memory("Missing-member orphan")
        missing = self._memory("Missing member embedding")
        present = self._memory("Present member embedding")
        self._insert_community("community-missing-embedding", [missing, present], _axis_vector(0))
        self._insert_vector(orphan, _axis_vector(0))
        self._insert_vector(present, _axis_vector(0))

        result = find_orphan_community_matches(
            self._primary(orphan), set(), db_connection=self.conn
        )

        ids = [match["entity_id"] for match in result["orphan_community_matches"]]
        self.assertNotIn(missing, ids)
        self.assertIn(present, ids)

    def test_scenario_11_community_tie_break_uses_lowest_id_stably(self):
        orphan = self._memory("Community tie orphan")
        low_member = self._memory("Low community member")
        high_member = self._memory("High community member")
        self._insert_community("community-001", [low_member], _axis_vector(0))
        self._insert_community("community-002", [high_member], _axis_vector(0))
        self._insert_vector(orphan, _axis_vector(0))
        self._insert_vector(low_member, _axis_vector(0))
        self._insert_vector(high_member, _axis_vector(0))

        first = find_orphan_community_matches(self._primary(orphan), set(), db_connection=self.conn)
        second = find_orphan_community_matches(
            self._primary(orphan), set(), db_connection=self.conn
        )

        self.assertEqual(first, second)
        self.assertEqual(
            [match["entity_id"] for match in first["orphan_community_matches"]], [low_member]
        )
        self.assertEqual(first["orphan_community_matches"][0]["community_id"], "community-001")

    def test_scenario_12_pack_budget_is_backward_compatible_without_new_parameter(self):
        result = pack_context_budget({"expansion_candidates": []}, [], {"conflict_sets": []})

        self.assertEqual(
            result,
            {
                "packed_entity_ids": {"primary": [], "expansion": []},
                "dropped_entity_ids": {"primary": [], "expansion": []},
                "conflict_only_entity_ids": [],
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
                },
            },
        )

    def test_scenario_13_pack_budget_reserves_orphan_tokens_outside_ordinary_budget(self):
        primary = self._memory("Budget reserve primary", "primary budget reserve content body")
        orphan_member = self._memory(
            "Budget reserve orphan member",
            "A substantial orphan-community reserve body that must remain available outside the "
            "ordinary primary and expansion token budget.",
        )

        result = pack_context_budget(
            {"expansion_candidates": []},
            [{"id": primary, "score": 1.0}],
            {"conflict_sets": []},
            budget_tokens=0,
            orphan_community_entity_ids={orphan_member},
            db_connection=self.conn,
        )

        self.assertEqual(result["packed_entity_ids"], {"primary": [], "expansion": []})
        self.assertEqual(result["dropped_entity_ids"]["primary"], [primary])
        self.assertGreater(result["token_counts"][orphan_member], 0)
        self.assertEqual(
            result["budget"]["orphan_community_reserve_tokens_used"],
            result["token_counts"][orphan_member],
        )
        self.assertEqual(result["budget"]["used"], 0)

    def test_scenario_14_assemble_retrieve_context_composes_orphan_assignment_end_to_end(self):
        cluster_a = self._memory("Composition cluster A", "cluster composition support content A")
        cluster_b = self._memory("Composition cluster B", "cluster composition support content B")
        orphan = self._memory(
            "Composition orphan",
            "orphan composition query anchor",
        )
        relation_result = store_relation(
            source_id=cluster_a,
            target_id=cluster_b,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertIn("successfully stored", relation_result, relation_result)
        self._insert_vector(cluster_a, _axis_vector(0))
        self._insert_vector(cluster_b, _axis_vector(0))
        self._insert_vector(orphan, _axis_vector(0))

        recompute_result = recompute_communities(db_connection=self.conn)
        self.assertEqual(recompute_result["status"], "recomputed")
        result = assemble_retrieve_context(
            "orphan composition query",
            "orphan-community-test",
            limit=1,
            db_connection=self.conn,
        )

        orphan_rows = [
            memory for memory in result["memories"] if memory["inclusion"] == "orphan_community"
        ]
        self.assertTrue(orphan_rows)
        self.assertIn(orphan_rows[0]["entity_id"], {cluster_a, cluster_b})
        self.assertEqual(
            set(orphan_rows[0]["retrieval_provenance"][0]),
            {"reason", "orphan_entity_id", "community_id", "similarity"},
        )
        provenance = orphan_rows[0]["retrieval_provenance"][0]
        self.assertEqual(provenance["reason"], "orphan_community_assignment")
        self.assertEqual(provenance["orphan_entity_id"], orphan)
        orphan_reserve = result["metadata"]["fan_out"]["orphan_community_reserve"]
        self.assertEqual(orphan_reserve["cap"], CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP)
        self.assertEqual(orphan_reserve["eligible_count"], len(orphan_rows))
        self.assertFalse(orphan_reserve["truncated"])
        expected_reserve_tokens = 0
        for memory in orphan_rows:
            row = self.conn.execute(
                "SELECT full_content FROM entities WHERE id = ?", (memory["entity_id"],)
            ).fetchone()
            self.assertIsNotNone(row)
            expected_reserve_tokens += get_model().token_count(row[0])
        self.assertEqual(
            result["metadata"]["budget"]["orphan_community_reserve_tokens_used"],
            expected_reserve_tokens,
        )

    def test_scenario_15_orphan_assignment_matches_leaf_centroid_only(self):
        orphan = self._memory("Hierarchy orphan")
        parent_member = self._memory("Hierarchy parent member")
        leaf_member = self._memory("Hierarchy leaf member")
        parent_id = "hierarchy-parent"
        child_id = "hierarchy-child"
        now = datetime.now(UTC).isoformat()
        child_centroid = _cosine_vector(0.8)
        self.conn.execute(
            "INSERT INTO communities "
            "(id, representative_entity_id, member_count, level, created_at, parent_community_id) "
            "VALUES (?, ?, ?, 0, ?, NULL)",
            (parent_id, parent_member, 1, now),
        )
        self.conn.execute(
            "INSERT INTO communities "
            "(id, representative_entity_id, member_count, level, created_at, parent_community_id) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (child_id, leaf_member, 1, now, parent_id),
        )
        self.conn.execute(
            "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, 0)",
            (parent_member, parent_id),
        )
        self.conn.execute(
            "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, 1)",
            (leaf_member, child_id),
        )
        self.conn.executemany(
            "INSERT INTO community_embeddings (community_id, embedding) VALUES (?, ?)",
            [
                (parent_id, sqlite_vec.serialize_float32(_axis_vector(0))),
                (child_id, sqlite_vec.serialize_float32(child_centroid)),
            ],
        )
        self._insert_vector(orphan, _axis_vector(0))
        self._insert_vector(parent_member, _axis_vector(0))
        self._insert_vector(leaf_member, child_centroid)
        self.conn.commit()

        result = find_orphan_community_matches(
            self._primary(orphan), set(), db_connection=self.conn
        )

        matches = result["orphan_community_matches"]
        self.assertEqual([match["entity_id"] for match in matches], [leaf_member])
        self.assertEqual(matches[0]["community_id"], child_id)
        self.assertNotEqual(matches[0]["community_id"], parent_id)
        self.assertGreater(matches[0]["similarity"], COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
