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
            "seed_cap": {"cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES, **cap},
            "representative_reserve": {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                **cap,
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

    def test_scenario_03_fewer_leaf_communities_all_seeded(self):
        first_rep, _ = self._insert_community(
            "community-a",
            [("A representative", _axis_vector(0)), ("A member", _axis_vector(1))],
            _axis_vector(0),
        )
        second_rep, _ = self._insert_community(
            "community-b",
            [("B representative", _axis_vector(0)), ("B member", _axis_vector(1))],
            _axis_vector(1),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": False,
                "dropped_count": 0,
            },
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep, second_rep],
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
            },
        )
        self.assertEqual(
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
        representatives = []
        lower_member_ids = []
        for community_id, similarity in (
            ("community-a", 1.0),
            ("community-b", 0.8),
            ("community-c", 0.6),
        ):
            representative, member_ids = self._insert_community(
                community_id,
                [
                    (f"{community_id} representative", _axis_vector(0)),
                    (f"{community_id} member", _cosine_vector(0.3)),
                ],
                _cosine_vector(similarity),
            )
            representatives.append(representative)
            lower_member_ids.append(member_ids[1])

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_TOP_K_COMMUNITIES", 3):
            with patch.object(
                community_retrieval_service, "CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP", 1
            ):
                with patch.object(
                    community_retrieval_service, "embed_text", return_value=_axis_vector(0)
                ):
                    result = seed_and_rank_communities("community query", db_connection=self.conn)

        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            representatives[:1],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": 1,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 2,
            },
        )
        member_ids = [match["entity_id"] for match in result["member_matches"]]
        self.assertTrue(set(lower_member_ids[1:]).issubset(member_ids))

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
        self.assertEqual(
            [match["community_id"] for match in result["representative_matches"]],
            ["community-a", "community-b"],
        )
        self.assertEqual(
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


if __name__ == "__main__":
    unittest.main()
