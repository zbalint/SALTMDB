import os
import re
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from saltmdb.config import CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS
from saltmdb.db.schema import init_db
from saltmdb.domain.services.context_expansion_service import expand_context_candidates
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import store_relation


def _memory_id(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        return cast(dict[str, str], result["data"])["id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class TestContextExpansionService(unittest.TestCase):
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
                content=f"Context expansion fixture content for {title} ({uuid.uuid4()})",
                title=title,
                owner_id="context-expansion-test",
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

    def test_empty_input_returns_all_empty_shape_and_zero_cap(self):
        self.assertEqual(
            expand_context_candidates([], db_connection=self.conn),
            {
                "in_network_edges": [],
                "expansion_candidates": [],
                "contradicts_edges": [],
                "fan_out": {
                    "cap": 0,
                    "eligible_count": 0,
                    "truncated": False,
                    "dropped_count": 0,
                },
            },
        )

    def test_allowlist_positive_depends_on_surfaces_candidate_with_provenance(self):
        primary = self._memory("Allowlist Primary")
        neighbor = self._memory("Allowlist Neighbor")
        self._relation(primary, neighbor, "depends_on")

        result = expand_context_candidates(
            [{"id": primary, "score": 0.75}], db_connection=self.conn
        )

        self.assertEqual(result["fan_out"]["eligible_count"], 1)
        self.assertEqual(
            result["expansion_candidates"],
            [
                {
                    "entity_id": neighbor,
                    "title": "Allowlist Neighbor",
                    "mutual_neighbor_count": 1,
                    "tiebreak_score": 0.75,
                    "retrieval_provenance": [
                        {
                            "reason": "graph_expansion",
                            "origin_hit_id": primary,
                            "predicate": "depends_on",
                            "direction": "outbound",
                            "hop_depth": 1,
                        }
                    ],
                }
            ],
        )

    def test_allowlist_negative_related_to_does_not_surface_or_count_neighbor(self):
        primary = self._memory("Related Primary")
        neighbor = self._memory("Related Neighbor")
        self._relation(primary, neighbor, "related_to")

        result = expand_context_candidates([{"id": primary, "score": 0.5}], db_connection=self.conn)

        self.assertEqual(result["expansion_candidates"], [])
        self.assertEqual(result["fan_out"]["eligible_count"], 0)
        self.assertEqual(result["in_network_edges"], [])
        self.assertEqual(result["contradicts_edges"], [])

    def test_allowlist_excludes_reserved_and_legacy_predicates(self):
        primary = self._memory("Reserved Legacy Primary")
        superseded = self._memory("Reserved Neighbor")
        similar = self._memory("Legacy Neighbor")
        self._raw_relation(primary, superseded, "supersedes")
        self._raw_relation(primary, similar, "similar_to")

        result = expand_context_candidates([{"id": primary, "score": 0.5}], db_connection=self.conn)

        self.assertEqual(result["expansion_candidates"], [])
        self.assertEqual(result["fan_out"]["eligible_count"], 0)

    def test_in_network_edges_are_separate_from_out_of_network_candidates(self):
        primary_a = self._memory("Split Primary A")
        primary_b = self._memory("Split Primary B")
        primary_c = self._memory("Split Primary C")
        neighbor = self._memory("Split Neighbor")
        in_network_id = self._relation(primary_a, primary_b, "depends_on")
        self._relation(primary_a, neighbor, "depends_on")

        result = expand_context_candidates(
            [
                {"id": primary_a, "score": 0.7},
                {"id": primary_b, "score": 0.6},
                {"id": primary_c, "score": 0.5},
            ],
            db_connection=self.conn,
        )

        self.assertEqual(
            result["in_network_edges"],
            [
                {
                    "relation_id": in_network_id,
                    "source_id": primary_a,
                    "target_id": primary_b,
                    "predicate": "depends_on",
                }
            ],
        )
        self.assertEqual(
            [candidate["entity_id"] for candidate in result["expansion_candidates"]], [neighbor]
        )
        self.assertEqual(result["fan_out"]["eligible_count"], 1)

    def test_mutual_neighbor_count_ranks_above_primary_relevance(self):
        primary_a = self._memory("Mutual Primary A")
        primary_b = self._memory("Mutual Primary B")
        shared = self._memory("Mutual Shared")
        lone = self._memory("Mutual Lone")
        self._relation(primary_a, shared, "depends_on")
        self._relation(primary_b, shared, "depends_on")
        self._relation(primary_a, lone, "depends_on")

        result = expand_context_candidates(
            [{"id": primary_a, "score": 0.99}, {"id": primary_b, "score": 0.01}],
            db_connection=self.conn,
        )

        self.assertEqual(
            [candidate["entity_id"] for candidate in result["expansion_candidates"]], [shared, lone]
        )

    def test_relevance_tiebreak_uses_highest_connected_primary_score(self):
        high_primary = self._memory("Score Primary High")
        low_primary = self._memory("Score Primary Low")
        high_neighbor = self._memory("Score Neighbor High")
        low_neighbor = self._memory("Score Neighbor Low")
        self._relation(high_primary, high_neighbor, "depends_on")
        self._relation(low_primary, low_neighbor, "depends_on")

        result = expand_context_candidates(
            [{"id": high_primary, "score": 0.9}, {"id": low_primary, "score": 0.1}],
            db_connection=self.conn,
        )

        self.assertEqual(
            [candidate["entity_id"] for candidate in result["expansion_candidates"]],
            [high_neighbor, low_neighbor],
        )

    def test_entity_id_tertiary_tiebreak_is_stable_across_calls(self):
        primary = self._memory("Stable Primary")
        first = self._memory("Stable First")
        second = self._memory("Stable Second")
        self._relation(primary, first, "depends_on")
        self._relation(primary, second, "depends_on")

        first_result = expand_context_candidates(
            [{"id": primary, "score": 0.5}], db_connection=self.conn
        )
        second_result = expand_context_candidates(
            [{"id": primary, "score": 0.5}], db_connection=self.conn
        )

        expected = sorted([first, second])
        self.assertEqual(
            [candidate["entity_id"] for candidate in first_result["expansion_candidates"]], expected
        )
        self.assertEqual(
            [candidate["entity_id"] for candidate in second_result["expansion_candidates"]],
            expected,
        )

    def test_fan_out_cap_truncates_to_top_ranked_candidates(self):
        primary = self._memory("Cap Primary")
        neighbors = [self._memory(f"Cap Neighbor {index}") for index in range(12)]
        for neighbor in neighbors:
            self._relation(primary, neighbor, "depends_on")

        result = expand_context_candidates([{"id": primary, "score": 0.5}], db_connection=self.conn)

        expected_survivors = sorted(neighbors)[:CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS]
        self.assertEqual(result["fan_out"]["cap"], CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS)
        self.assertEqual(result["fan_out"]["eligible_count"], len(neighbors))
        self.assertTrue(result["fan_out"]["truncated"])
        self.assertEqual(
            result["fan_out"]["dropped_count"],
            len(neighbors) - CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS,
        )
        self.assertEqual(
            [candidate["entity_id"] for candidate in result["expansion_candidates"]],
            expected_survivors,
        )

    def test_in_network_edges_are_never_capped(self):
        primary = [self._memory(f"In-network Primary {index}") for index in range(12)]
        relation_ids = [
            self._relation(source_id, target_id, "depends_on")
            for source_id in primary
            for target_id in primary
            if source_id != target_id
        ]

        result = expand_context_candidates(
            [{"id": entity_id, "score": 0.5} for entity_id in primary], db_connection=self.conn
        )

        self.assertGreater(len(relation_ids), len(primary) * CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS)
        self.assertEqual(
            {edge["relation_id"] for edge in result["in_network_edges"]}, set(relation_ids)
        )
        self.assertEqual(result["expansion_candidates"], [])
        self.assertEqual(
            result["fan_out"],
            {
                "cap": len(primary) * CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS,
                "eligible_count": 0,
                "truncated": False,
                "dropped_count": 0,
            },
        )

    def test_multi_provenance_node_keeps_each_distinct_reaching_triple(self):
        primary = self._memory("Provenance Primary")
        neighbor = self._memory("Provenance Neighbor")
        self._relation(primary, neighbor, "depends_on")
        self._relation(primary, neighbor, "corrects")

        result = expand_context_candidates([{"id": primary, "score": 0.4}], db_connection=self.conn)

        self.assertEqual(
            result["expansion_candidates"][0]["retrieval_provenance"],
            [
                {
                    "reason": "graph_expansion",
                    "origin_hit_id": primary,
                    "predicate": "corrects",
                    "direction": "outbound",
                    "hop_depth": 1,
                },
                {
                    "reason": "graph_expansion",
                    "origin_hit_id": primary,
                    "predicate": "depends_on",
                    "direction": "outbound",
                    "hop_depth": 1,
                },
            ],
        )

    def test_contradicts_edges_bypass_allowlist_and_fan_out_cap(self):
        primary = self._memory("Contradicts Primary")
        contradicted = self._memory("Contradicted Neighbor")
        contradicts_id = self._relation(primary, contradicted, "contradicts")
        expansion_neighbors = [
            self._memory(f"Contradicts Cap Neighbor {index}") for index in range(11)
        ]
        for neighbor in expansion_neighbors:
            self._relation(primary, neighbor, "depends_on")

        result = expand_context_candidates([{"id": primary, "score": 0.5}], db_connection=self.conn)

        self.assertEqual(
            result["contradicts_edges"],
            [
                {
                    "relation_id": contradicts_id,
                    "source_id": primary,
                    "target_id": contradicted,
                    "predicate": "contradicts",
                }
            ],
        )
        self.assertNotIn(
            contradicted, [candidate["entity_id"] for candidate in result["expansion_candidates"]]
        )
        self.assertEqual(result["fan_out"]["eligible_count"], len(expansion_neighbors))
        self.assertEqual(result["fan_out"]["dropped_count"], 1)

    def test_unresolvable_primary_hit_is_skipped_without_hiding_valid_results(self):
        primary = self._memory("Resolvable Primary")
        neighbor = self._memory("Resolvable Neighbor")
        self._relation(primary, neighbor, "depends_on")

        result = expand_context_candidates(
            [
                {"id": "00000000-0000-0000-0000-000000000000", "score": 0.99},
                {"id": primary, "score": 0.4},
            ],
            db_connection=self.conn,
        )

        self.assertEqual(
            [candidate["entity_id"] for candidate in result["expansion_candidates"]], [neighbor]
        )
        self.assertEqual(result["fan_out"]["cap"], 2 * CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS)

    def test_provenance_direction_is_relative_to_the_primary_hit(self):
        primary = self._memory("Direction Primary")
        outbound = self._memory("Direction Outbound")
        inbound = self._memory("Direction Inbound")
        self._relation(primary, outbound, "depends_on")
        self._relation(inbound, primary, "depends_on")

        result = expand_context_candidates([{"id": primary, "score": 0.5}], db_connection=self.conn)
        provenance_by_id = {
            candidate["entity_id"]: candidate["retrieval_provenance"]
            for candidate in result["expansion_candidates"]
        }

        self.assertEqual(provenance_by_id[outbound][0]["direction"], "outbound")
        self.assertEqual(provenance_by_id[inbound][0]["direction"], "inbound")
