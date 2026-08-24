import unittest
import tempfile
import os
import shutil
import uuid
from unittest.mock import patch
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
            db_path=self.db_path,
        )
        store_memory(
            title="Database Backup Service",
            content="Performs hourly PostgreSQL and SQLite snapshot backups",
            owner_id="user1",
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

    def test_active_incoming_relation_does_not_change_fts_relevance(self):
        older_id, newer_id = self._insert_matching_pair()
        self._insert_relation(self._insert_nonmatching_source("active"), older_id)

        result_ids = self._fts_result_ids()

        self.assertEqual(result_ids[:2], [newer_id, older_id])

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

    def _insert_search_entity(self, entity_id: str, title: str, status: str = "raw") -> None:
        timestamp = "2024-02-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO entities "
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title, "
            "full_content, content_hash, memory_type) "
            "VALUES (?, ?, ?, ?, 'test_user', ?, ?, ?, ?, 'fact')",
            (
                entity_id,
                timestamp,
                timestamp,
                timestamp,
                status,
                title,
                f"content for {entity_id}",
                entity_id,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _fts_row(entity_id: str, title: str) -> tuple:
        return (
            entity_id,
            title,
            f"content for {entity_id}",
            1,
            0,
            -1.0,
            "",
            "",
            "test_user",
            "private",
            "{}",
            None,
            "fact",
            0,
            None,
        )

    def test_unique_active_exact_title_is_ranked_first(self):
        self._insert_search_entity("semantic-winner", "Related architecture overview")
        self._insert_search_entity("exact-title", "Exact: Unicode — Title")
        fts_rows = [
            self._fts_row("semantic-winner", "Related architecture overview"),
            self._fts_row("exact-title", "Exact: Unicode — Title"),
        ]
        semantic_rows = [("semantic-winner", 0.1), ("exact-title", 0.2)]

        with (
            patch(
                "saltmdb.domain.services.memory_service.search_primitives._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.search_primitives.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            results = search_memory(
                query_keywords="Exact: Unicode — Title",
                include_related=False,
                db_connection=self.conn,
                db_path=self.db_path,
            )

        self.assertEqual([row["id"] for row in results], ["exact-title"])

    def test_duplicate_active_exact_title_preserves_hybrid_order(self):
        title = "Shared exact title"
        self._insert_search_entity("hybrid-winner", title)
        self._insert_search_entity("duplicate", title)
        fts_rows = [
            self._fts_row("hybrid-winner", title),
            self._fts_row("duplicate", title),
        ]
        semantic_rows = [("hybrid-winner", 0.1), ("duplicate", 0.2)]

        with (
            patch(
                "saltmdb.domain.services.memory_service.search_primitives._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.search_primitives.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            results = search_memory(
                query_keywords=title,
                include_related=False,
                db_connection=self.conn,
                db_path=self.db_path,
            )

        self.assertEqual([row["id"] for row in results[:2]], ["hybrid-winner", "duplicate"])

    def test_archived_duplicate_does_not_block_active_exact_title(self):
        title = "One active exact title"
        self._insert_search_entity("semantic-winner", "Related result")
        self._insert_search_entity("active-exact", title)
        self._insert_search_entity("archived-duplicate", title, status="archived")
        fts_rows = [
            self._fts_row("semantic-winner", "Related result"),
            self._fts_row("active-exact", title),
        ]
        semantic_rows = [("semantic-winner", 0.1), ("active-exact", 0.2)]

        with (
            patch(
                "saltmdb.domain.services.memory_service.search_primitives._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.search_primitives.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            results = search_memory(
                query_keywords=title,
                include_related=False,
                db_connection=self.conn,
                db_path=self.db_path,
            )

        self.assertEqual([row["id"] for row in results], ["active-exact"])

    def test_exact_title_fast_path_works_outside_hybrid_candidate_window(self):
        title = "Low-weight exact title"
        self._insert_search_entity("hybrid-winner", "Content-only winner")
        self._insert_search_entity("exact-outside-window", title)
        fts_rows = [self._fts_row("hybrid-winner", "Content-only winner")]
        semantic_rows = [("hybrid-winner", 0.1)]

        with (
            patch(
                "saltmdb.domain.services.memory_service.search_primitives._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.search_primitives.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            results = search_memory(
                query_keywords=title,
                include_related=False,
                db_connection=self.conn,
                db_path=self.db_path,
            )

        self.assertEqual([row["id"] for row in results], ["exact-outside-window"])

    def test_punctuation_only_exact_title_does_not_become_empty_query_browse(self):
        self._insert_search_entity("punctuation-title", "-----")
        self._insert_search_entity("newer-unrelated", "Newest unrelated memory")
        self.conn.execute(
            "UPDATE entities SET updated_at = '2024-03-01T00:00:00+00:00' "
            "WHERE id = 'newer-unrelated'"
        )
        self.conn.commit()

        results = search_memory(
            query_keywords="-----",
            include_related=False,
            db_connection=self.conn,
            db_path=self.db_path,
        )

        self.assertEqual(results[0]["id"], "punctuation-title")

    def test_exact_title_fast_path_works_when_semantic_search_is_disabled(self):
        title = "Exact title without semantic retrieval"
        self._insert_search_entity("exact-without-semantic", title)

        with patch(
            "saltmdb.config.is_semantic_search_enabled",
            return_value=False,
        ):
            results = search_memory(
                query_keywords=title,
                include_related=False,
                db_connection=self.conn,
                db_path=self.db_path,
            )

        self.assertEqual([row["id"] for row in results], ["exact-without-semantic"])

    def test_exact_title_fast_path_respects_zero_limit(self):
        title = "Exact title with zero limit"
        self._insert_search_entity("exact-zero-limit", title)

        results = search_memory(
            query_keywords=title,
            limit=0,
            include_related=False,
            db_connection=self.conn,
            db_path=self.db_path,
        )

        self.assertEqual(results, [])

    def test_history_exact_title_fast_path_marks_superseded_entity(self):
        title = "Superseded exact title"
        self._insert_search_entity("old-exact", title)
        self._insert_search_entity("new-head", "Current replacement title")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate) "
            "VALUES (?, 'new-head', 'old-exact', 'supersedes')",
            (str(uuid.uuid4()),),
        )
        self.conn.commit()

        results = search_memory(
            query_keywords=title,
            mode="history",
            include_related=False,
            db_connection=self.conn,
            db_path=self.db_path,
        )

        self.assertEqual([row["id"] for row in results], ["old-exact"])
        self.assertTrue(results[0]["is_superseded"])

    def test_family_collapse_takes_precedence_over_exact_title_fast_path(self):
        title = "Old family member title"
        self._insert_search_entity("old-member", title)
        self._insert_search_entity("family-head", "Current family head")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate) "
            "VALUES (?, 'family-head', 'old-member', 'supersedes')",
            (str(uuid.uuid4()),),
        )
        self.conn.commit()
        fts_rows = [
            self._fts_row("old-member", title),
            self._fts_row("family-head", "Current family head"),
        ]
        semantic_rows = [("old-member", 0.1), ("family-head", 0.2)]

        with (
            patch(
                "saltmdb.domain.services.memory_service.search_primitives._run_fts_search",
                return_value=(fts_rows, False),
            ),
            patch(
                "saltmdb.domain.services.memory_service.search_primitives.semantic_search",
                return_value=semantic_rows,
            ),
        ):
            results = search_memory(
                query_keywords=title,
                collapse_supersedes_families=True,
                include_related=False,
                db_connection=self.conn,
                db_path=self.db_path,
            )

        self.assertEqual([row["id"] for row in results], ["family-head"])

    def test_active_title_lookup_index_exists(self):
        indexes = {row[1] for row in self.conn.execute("PRAGMA index_list(entities)").fetchall()}

        self.assertIn("idx_entities_active_title", indexes)


if __name__ == "__main__":
    unittest.main()
