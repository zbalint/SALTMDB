import os
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
from saltmdb.db.schema import init_db
from saltmdb.domain.services import community_retrieval_service
from saltmdb.domain.services.community_retrieval_service import seed_and_rank_communities
from saltmdb.domain.services.memory_service import store_memory

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


def _cosine_vector(cosine: float, first: int = 0, second: int = 1) -> list[float]:
    vector = [0.0] * DIM
    vector[first] = cosine
    vector[second] = (max(0.0, 1.0 - cosine**2)) ** 0.5
    return vector


def _memory_id(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        return cast(dict[str, str], result["data"])["id"]
    marker = "ID: "
    start = result.find(marker)
    if start < 0:
        raise AssertionError(f"Could not parse entity ID from result: {result!r}")
    return result[start + len(marker) :].split()[0]


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _NullTitleConnection:
    def __init__(self, connection: sqlite3.Connection, null_title_ids: set[str]):
        self._connection = connection
        self._null_title_ids = null_title_ids

    def execute(self, sql: str, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        if "SELECT id, title FROM entities" not in sql:
            return cursor
        rows = cursor.fetchall()
        return _Rows(
            [
                (entity_id, None if entity_id in self._null_title_ids else title)
                for entity_id, title in rows
            ]
        )


class TestCommunityRetrievalService(unittest.TestCase):
    temp_dir: str = ""
    conn: sqlite3.Connection = cast(sqlite3.Connection, cast(object, None))
    _test_mode: Any = None

    def setUp(self):
        self._test_mode = patch.dict(os.environ, {"SALTMDB_TEST_MODE": "1"}, clear=False)
        self._test_mode.start()
        self.temp_dir = tempfile.mkdtemp()
        self.conn = init_db(os.path.join(self.temp_dir, "test.db"))

    def tearDown(self):
        self.conn.close()
        self._test_mode.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _memory(self, title: str) -> str:
        return _memory_id(
            store_memory(
                content=f"Global retrieval fixture content for {title} ({uuid.uuid4()})",
                title=title,
                owner_id="community-retrieval-test",
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
        member_vectors: list[tuple[str, list[float]]],
        centroid: list[float],
    ) -> tuple[str, list[str]]:
        member_ids = [self._memory(title) for title, _ in member_vectors]
        for entity_id, (_, vector) in zip(member_ids, member_vectors):
            self._insert_vector(entity_id, vector)
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
        return member_ids[0], member_ids

    @staticmethod
    def _zero_shape() -> dict[str, Any]:
        cap = {"eligible_count": 0, "truncated": False, "dropped_count": 0}
        return {
            "representative_matches": [],
            "member_matches": [],
            "seed_cap": {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                **cap,
                "gap_dropped_count": 0,
            },
            "representative_reserve": {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                **cap,
                "gap_dropped_count": 0,
            },
            "member_pool": {"cap": config.CONTEXT_GLOBAL_MEMBER_POOL_CAP, **cap},
        }

    def test_scenario_01_zero_communities_returns_empty_shape(self):
        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        self.assertEqual(result, self._zero_shape())

    def test_scenario_02_blank_query_returns_empty_shape_without_embedding(self):
        with patch.object(community_retrieval_service, "embed_text") as embed:
            result = seed_and_rank_communities("  \t\n", db_connection=self.conn)

        embed.assert_not_called()
        self.assertEqual(result, self._zero_shape())

    def test_scenario_03_gap_floor_excludes_orthogonal_second_community(self):
        first_rep, _ = self._insert_community(
            "community-a",
            [("A representative", _axis_vector(0)), ("A member", _axis_vector(1))],
            _axis_vector(0),
        )
        self._insert_community(
            "community-b",
            [("B representative", _axis_vector(0)), ("B member", _axis_vector(1))],
            _axis_vector(1),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # community-b's centroid is orthogonal to the query (similarity 0.0) while community-a's is
        # an exact match (1.0) -- a gap of 1.0, exceeding CONTEXT_GLOBAL_SEED_SIMILARITY_GAP (0.5).
        # This reproduces probe 3's confirmed Bug B defect shape in miniature: before this fix,
        # community-b was still seeded purely because fewer leaf communities existed than
        # CONTEXT_GLOBAL_TOP_K_COMMUNITIES, regardless of its zero relevance to the query.
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": True,
                "dropped_count": 0,
                "gap_dropped_count": 1,
            },
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep],
        )

    def test_scenario_04_top_k_seeding_truncates_by_centroid_similarity(self):
        representatives = []
        for community_id, similarity in (
            ("community-a", 1.0),
            ("community-b", 0.8),
            ("community-c", 0.2),
        ):
            representative, _ = self._insert_community(
                community_id,
                [
                    (f"{community_id} representative", _axis_vector(0)),
                    (f"{community_id} member", _axis_vector(1)),
                ],
                _cosine_vector(similarity),
            )
            representatives.append(representative)

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_TOP_K_COMMUNITIES", 2):
            with patch.object(
                community_retrieval_service, "CONTEXT_GLOBAL_SEED_SIMILARITY_GAP", 0.5
            ):
                with patch.object(
                    community_retrieval_service, "embed_text", return_value=_axis_vector(0)
                ):
                    result = seed_and_rank_communities("community query", db_connection=self.conn)

        self.assertEqual(
            result["seed_cap"],
            {
                "cap": 2,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 1,
                "gap_dropped_count": 0,
            },
        )
        self.assertCountEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            representatives[:2],
        )

    def test_scenario_05_best_per_query_match_becomes_representative_even_if_not_the_fixed_one(
        self,
    ):
        representative, member_ids = self._insert_community(
            "community-a",
            [("Representative", _axis_vector(2)), ("Member", _axis_vector(0))],
            _axis_vector(0),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # "Member" exactly matches the query (similarity 1.0); the fixed "Representative" is
        # orthogonal (similarity 0) and loses -- it falls through to the member pool instead of
        # being force-shown regardless of relevance.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]], [member_ids[1]]
        )
        self.assertEqual(
            [match["entity_id"] for match in result["member_matches"]], [representative]
        )

    def test_scenario_06_representative_and_member_caps_are_independent(self):
        _, first_ids = self._insert_community(
            "community-a",
            [("A representative", _cosine_vector(0.5)), ("A member", _cosine_vector(0.3))],
            _cosine_vector(1.0),
        )
        _, second_ids = self._insert_community(
            "community-b",
            [("B representative", _cosine_vector(0.9)), ("B member", _cosine_vector(0.3))],
            _cosine_vector(0.8),
        )
        _, third_ids = self._insert_community(
            "community-c",
            [("C representative", _cosine_vector(0.7)), ("C member", _cosine_vector(0.3))],
            _cosine_vector(0.6),
        )

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_TOP_K_COMMUNITIES", 3):
            with patch.object(
                community_retrieval_service, "CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP", 2
            ):
                with patch.object(
                    community_retrieval_service, "CONTEXT_GLOBAL_SEED_SIMILARITY_GAP", 0.5
                ):
                    with patch.object(
                        community_retrieval_service,
                        "CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP",
                        0.5,
                    ):
                        with patch.object(
                            community_retrieval_service,
                            "embed_text",
                            return_value=_axis_vector(0),
                        ):
                            result = seed_and_rank_communities(
                                "community query", db_connection=self.conn
                            )

        # Representative selection is driven entirely by each candidate's own real per-query
        # similarity (0.5/0.9/0.7), not its community's centroid rank (1.0/0.8/0.6) -- community-b's
        # representative (0.9) and community-c's representative (0.7) win the 2 reserve slots despite
        # community-a having the single highest centroid; community-a's representative (0.5, weakest)
        # is dropped by the cap, but its own other member still surfaces via the independent member
        # pool.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [second_ids[0], third_ids[0]],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": 2,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 1,
                "gap_dropped_count": 0,
            },
        )
        member_ids = [match["entity_id"] for match in result["member_matches"]]
        self.assertIn(first_ids[1], member_ids)

    def test_scenario_07_member_pool_is_shared_and_truncated_by_query_similarity(self):
        first_rep, first_ids = self._insert_community(
            "community-a",
            [
                ("A representative", _axis_vector(0)),
                ("A high member", _cosine_vector(0.9)),
                ("A low member", _cosine_vector(0.4)),
            ],
            _axis_vector(0),
        )
        second_rep, second_ids = self._insert_community(
            "community-b",
            [
                ("B representative", _axis_vector(0)),
                ("B high member", _cosine_vector(0.8)),
                ("B low member", _cosine_vector(0.7)),
            ],
            _axis_vector(0),
        )

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_TOP_K_COMMUNITIES", 2):
            with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_MEMBER_POOL_CAP", 2):
                with patch.object(
                    community_retrieval_service, "embed_text", return_value=_axis_vector(0)
                ):
                    result = seed_and_rank_communities("community query", db_connection=self.conn)

        self.assertEqual(
            result["member_pool"],
            {
                "cap": 2,
                "eligible_count": 4,
                "truncated": True,
                "dropped_count": 2,
            },
        )
        expected = [first_ids[1], second_ids[1]]
        self.assertEqual([match["entity_id"] for match in result["member_matches"]], expected)
        self.assertNotIn(first_rep, [match["entity_id"] for match in result["member_matches"]])
        self.assertNotIn(second_rep, [match["entity_id"] for match in result["member_matches"]])

    def test_scenario_08_missing_member_embedding_is_excluded(self):
        _, member_ids = self._insert_community(
            "community-a",
            [
                ("Representative", _axis_vector(2)),
                ("Missing embedding", _axis_vector(0)),
                ("Present member", _axis_vector(0)),
            ],
            _axis_vector(0),
        )
        self.conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (member_ids[1],))
        self.conn.commit()

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # "Present member" has the best query similarity among entities with an embedding row, so
        # it wins the representative slot; "Missing embedding" never competes (no embedding row)
        # and never appears in either list; "Representative" (the fixed medoid, orthogonal to the
        # query) loses the per-query contest and falls through to the member pool.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]], [member_ids[2]]
        )
        self.assertEqual(result["member_pool"]["eligible_count"], 1)
        self.assertEqual(
            [match["entity_id"] for match in result["member_matches"]], [member_ids[0]]
        )
        surfaced_ids = {
            match["entity_id"]
            for match in result["representative_matches"] + result["member_matches"]
        }
        self.assertNotIn(member_ids[1], surfaced_ids)

    def test_scenario_09_ties_break_by_community_and_entity_id(self):
        _, first_ids = self._insert_community(
            "community-a",
            [("A representative", _axis_vector(2)), ("A member", _axis_vector(0))],
            _axis_vector(0),
        )
        _, second_ids = self._insert_community(
            "community-b",
            [("B representative", _axis_vector(2)), ("B member", _axis_vector(0))],
            _axis_vector(0),
        )

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_TOP_K_COMMUNITIES", 2):
            with patch.object(
                community_retrieval_service, "embed_text", return_value=_axis_vector(0)
            ):
                result = seed_and_rank_communities("community query", db_connection=self.conn)

        # "A member"/"B member" exactly match the query and win each community's representative
        # slot; the two fixed representatives (both orthogonal to the query, tied at similarity 0)
        # fall through to the shared member pool, where the tie is broken by entity_id.
        self.assertCountEqual(
            [match["community_id"] for match in result["representative_matches"]],
            ["community-a", "community-b"],
        )
        self.assertCountEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_ids[1], second_ids[1]],
        )
        self.assertEqual(
            [match["entity_id"] for match in result["member_matches"]],
            sorted([first_ids[0], second_ids[0]]),
        )

    def test_scenario_10_null_title_is_reported_as_unknown(self):
        representative, _ = self._insert_community(
            "community-a",
            [
                ("Nullable title representative", _axis_vector(0)),
                ("Member", _cosine_vector(0.3)),
            ],
            _axis_vector(0),
        )
        connection = _NullTitleConnection(self.conn, {representative})

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities(
                "community query",
                db_connection=cast(sqlite3.Connection, cast(object, connection)),
            )

        self.assertEqual(result["representative_matches"][0]["entity_id"], representative)
        self.assertEqual(result["representative_matches"][0]["title"], "Unknown")

    def test_scenario_11_communities_within_gap_all_seeded(self):
        first_rep, _ = self._insert_community(
            "community-a",
            [("A representative", _axis_vector(0)), ("A member", _axis_vector(1))],
            _cosine_vector(1.0),
        )
        second_rep, _ = self._insert_community(
            "community-b",
            [("B representative", _axis_vector(0)), ("B member", _axis_vector(1))],
            _cosine_vector(0.7),
        )

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_SEED_SIMILARITY_GAP", 0.5):
            with patch.object(
                community_retrieval_service, "embed_text", return_value=_axis_vector(0)
            ):
                result = seed_and_rank_communities("community query", db_connection=self.conn)

        # Both centroids are within CONTEXT_GLOBAL_SEED_SIMILARITY_GAP (0.5) of the top match's own
        # similarity (gap 0.3) -- the floor admits both, confirming the fix trims genuine outliers
        # only, not every community below rank 1.
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": False,
                "dropped_count": 0,
                "gap_dropped_count": 0,
            },
        )
        self.assertCountEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep, second_rep],
        )

    def test_scenario_12_top_ranked_community_seeded_despite_weak_absolute_similarity(self):
        weak_rep, _ = self._insert_community(
            "community-a",
            [("Weak representative", _axis_vector(0)), ("Weak member", _axis_vector(1))],
            _cosine_vector(0.05),
        )
        self._insert_community(
            "community-b",
            [("Excluded representative", _axis_vector(0)), ("Excluded member", _axis_vector(1))],
            _cosine_vector(-0.5),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # community-a is rank 1 (similarity 0.05) and is force-included despite being a weak
        # absolute match -- the gap floor can never produce an empty seed set. community-b
        # (similarity -0.5, gap 0.55 from rank 1) exceeds CONTEXT_GLOBAL_SEED_SIMILARITY_GAP (0.5)
        # and is correctly excluded.
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": True,
                "dropped_count": 0,
                "gap_dropped_count": 1,
            },
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [weak_rep],
        )

    def test_scenario_13_representative_gap_floor_excludes_weak_second_representative(self):
        first_rep, _ = self._insert_community(
            "community-a",
            [("A representative", _cosine_vector(1.0)), ("A member", _cosine_vector(0.5))],
            _cosine_vector(1.0),
        )
        _, second_ids = self._insert_community(
            "community-b",
            [("B representative", _cosine_vector(0.3)), ("B member", _cosine_vector(0.1))],
            _cosine_vector(0.6),
        )

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_SEED_SIMILARITY_GAP", 0.5):
            with patch.object(
                community_retrieval_service, "CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP", 0.5
            ):
                with patch.object(
                    community_retrieval_service, "embed_text", return_value=_axis_vector(0)
                ):
                    result = seed_and_rank_communities("community query", db_connection=self.conn)

        # Both communities are seeded (centroid gap 1.0-0.6=0.4 <= CONTEXT_GLOBAL_SEED_SIMILARITY_GAP,
        # 0.5) -- D3's community-level floor admits both. But community-b's own best member (0.3) is
        # 0.7 away from community-a's own best member (1.0), exceeding
        # CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP (0.5) -- its representative candidate is
        # excluded from representative_matches. community-b's own dropped representative candidate is
        # never redirected into the member pool (this cap's pre-existing, unchanged cap-independent
        # behavior) -- but "B member" (0.1), always separately scored, still competes for member_pool
        # on its own merits.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                "eligible_count": 2,
                "truncated": True,
                "dropped_count": 0,
                "gap_dropped_count": 1,
            },
        )
        member_ids = [match["entity_id"] for match in result["member_matches"]]
        self.assertIn(second_ids[1], member_ids)
        self.assertNotIn(second_ids[0], member_ids)

    def test_scenario_14_weakest_possible_representative_still_admitted_when_sole_candidate(self):
        weak_rep, _ = self._insert_community(
            "community-a",
            [("Weak representative", _cosine_vector(0.02)), ("Weak member", _cosine_vector(0.01))],
            _cosine_vector(0.02),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # A single, extremely weak community/representative pair (similarity 0.02) -- the
        # representative gap floor compares each candidate to the best ADMITTED representative this
        # call, and the single best one's own gap-to-itself is always 0, so it is force-included
        # regardless of how weak its absolute similarity is. Mirrors scenario 12's proof of the same
        # guarantee at the community-seeding stage, one level down.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [weak_rep],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                "eligible_count": 1,
                "truncated": False,
                "dropped_count": 0,
                "gap_dropped_count": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
