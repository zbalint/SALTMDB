import unittest
import tempfile
import os
import shutil
import uuid
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    _run_fts_search,
    store_memory,
    search_memory,
    reciprocal_rank_fusion,
)


class TestSearchScores(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_matching_pair(self) -> tuple[str, str]:
        """Create equal FTS matches whose fixed timestamps decide an unboosted tie."""
        older_id = "older-match"
        newer_id = "newer-match"
        for entity_id, updated_at in (
            (older_id, "2024-01-01T00:00:00+00:00"),
            (newer_id, "2024-01-02T00:00:00+00:00"),
        ):
            self.conn.execute(
                "INSERT INTO entities "
                "(id, created_at, updated_at, last_accessed_at, owner_id, status, title, "
                "full_content, content_hash, memory_type) "
                "VALUES (?, ?, ?, ?, 'test_user', 'raw', ?, ?, ?, 'fact')",
                (
                    entity_id,
                    updated_at,
                    updated_at,
                    updated_at,
                    "Relation ranking target",
                    "relation boost fixture content",
                    entity_id,
                ),
            )
        self.conn.commit()
        return older_id, newer_id

    def _insert_nonmatching_source(self, suffix: str) -> str:
        """Create a distinct source so every relation fixture satisfies both FKs."""
        source_id = f"relation-source-{suffix}"
        timestamp = "2024-01-03T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO entities "
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title, "
            "full_content, content_hash, memory_type) "
            "VALUES (?, ?, ?, ?, 'test_user', 'raw', ?, ?, ?, 'fact')",
            (
                source_id,
                timestamp,
                timestamp,
                timestamp,
                "Unrelated source entity",
                "does not match the ranking query",
                source_id,
            ),
        )
        self.conn.commit()
        return source_id

    def _insert_relation(self, source_id: str, target_id: str, valid_to: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) "
            "VALUES (?, ?, ?, 'relates_to', ?)",
            (str(uuid.uuid4()), source_id, target_id, valid_to),
        )
        self.conn.commit()

    def _fts_result_ids(self) -> list[str]:
        rows = _run_fts_search(
            self.conn, "relation boost target", ["e.status != 'archived'"], [], 10, 0
        )
        return [row[0] for row in rows]

    def test_fts_search_score_non_zero(self):
        store_memory(
            title="Authentication Module",
            content="Handles OAuth2 and JWT token authentication",
            owner_id="user1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        store_memory(
            title="Database Backup Service",
            content="Performs hourly PostgreSQL and SQLite snapshot backups",
            owner_id="user1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )

        results = search_memory(
            query_keywords="authentication OAuth2", owner_id="user1", db_path=self.db_path
        )
        self.assertTrue(len(results) > 0)
        self.assertGreater(results[0]["score"], 0.0)

    def test_reciprocal_rank_fusion_returns_scores(self):
        fts = [("id1", "title1")]
        semantic = [("id1", 0.1), ("id2", 0.2)]
        fused = reciprocal_rank_fusion(fts, semantic, limit=5)
        self.assertIn("id1", fused)
        self.assertGreater(fused["id1"], 0.0)

    def test_fts_tie_break_prefers_newer_entity_without_relations(self):
        older_id, newer_id = self._insert_matching_pair()

        result_ids = self._fts_result_ids()

        self.assertEqual(result_ids[:2], [newer_id, older_id])

    def test_active_incoming_relation_boosts_older_fts_match(self):
        older_id, newer_id = self._insert_matching_pair()
        self._insert_relation(self._insert_nonmatching_source("active"), older_id)

        result_ids = self._fts_result_ids()

        self.assertEqual(result_ids[:2], [older_id, newer_id])

    def test_past_valid_to_incoming_relation_does_not_boost_fts_match(self):
        older_id, newer_id = self._insert_matching_pair()
        self._insert_relation(
            self._insert_nonmatching_source("past"), older_id, "2020-01-01T00:00:00+00:00"
        )

        result_ids = self._fts_result_ids()

        self.assertEqual(result_ids[:2], [newer_id, older_id])

    def test_active_outgoing_relation_does_not_boost_fts_match(self):
        older_id, newer_id = self._insert_matching_pair()
        self._insert_relation(older_id, self._insert_nonmatching_source("outgoing"))

        result_ids = self._fts_result_ids()

        self.assertEqual(result_ids[:2], [newer_id, older_id])


if __name__ == "__main__":
    unittest.main()
