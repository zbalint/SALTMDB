import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.db.schema import init_db


class TestLifecycleInvariantMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "legacy.db")
        self.conn = init_db(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _entity(self, entity_id, status="raw", valid_from="2026-01-02T00:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO entities "
            "(id,created_at,updated_at,last_accessed_at,status,title,full_content,valid_from) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (entity_id, valid_from, valid_from, valid_from, status, entity_id, "body", valid_from),
        )

    def _relation(self, relation_id, source, target, predicate="supersedes"):
        self.conn.execute(
            "INSERT INTO relations "
            "(id,source_id,target_id,predicate,created_at,valid_from) VALUES (?,?,?,?,?,?)",
            (
                relation_id,
                source,
                target,
                predicate,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    def _migrate(self):
        self.conn.execute("PRAGMA user_version = 2")
        self.conn.close()
        self.conn = init_db(self.path)

    def test_repairs_linear_supersedes_self_edges_and_vector_pollution(self):
        self._entity("new")
        self._entity("old")
        self._relation("sup", "new", "old")
        self._relation("self", "new", "new", "related_to")
        vector = b"\x00" * (384 * 4)
        self.conn.execute(
            "INSERT INTO entity_embeddings(entity_id,embedding) VALUES (?,?)", ("old", vector)
        )
        self.conn.execute(
            "INSERT INTO entity_embeddings(entity_id,embedding) VALUES (?,?)", ("orphan", vector)
        )
        self.conn.commit()

        self._migrate()

        status, valid_from, valid_to = self.conn.execute(
            "SELECT status,valid_from,valid_to FROM entities WHERE id='old'"
        ).fetchone()
        self.assertEqual(status, "archived")
        self.assertGreaterEqual(valid_to, valid_from)
        self.assertIsNotNone(
            self.conn.execute("SELECT invalid_at FROM relations WHERE id='self'").fetchone()[0]
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0], 0
        )
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_skips_entire_branching_supersedes_component(self):
        for entity_id in ("new-a", "new-b", "old"):
            self._entity(entity_id)
        self._relation("sup-a", "new-a", "old")
        self._relation("sup-b", "new-b", "old")
        self.conn.commit()

        self._migrate()

        self.assertEqual(
            self.conn.execute("SELECT status FROM entities WHERE id='old'").fetchone()[0], "raw"
        )

    def test_skips_entire_cyclic_supersedes_component(self):
        for entity_id in ("cycle-a", "cycle-b"):
            self._entity(entity_id)
        self._relation("sup-a-b", "cycle-a", "cycle-b")
        self._relation("sup-b-a", "cycle-b", "cycle-a")
        self.conn.commit()

        self._migrate()

        self.assertEqual(
            self.conn.execute(
                "SELECT GROUP_CONCAT(status, ',') FROM entities "
                "WHERE id IN ('cycle-a','cycle-b') ORDER BY id"
            ).fetchone()[0],
            "raw,raw",
        )

    def test_rebuilds_stale_context_index_definition(self):
        self.conn.execute("DROP INDEX idx_entities_context")
        self.conn.execute("CREATE INDEX idx_entities_context ON entities(context_id, project_id)")
        self.conn.commit()

        self._migrate()

        columns = [
            row[2]
            for row in self.conn.execute("PRAGMA index_info(idx_entities_context)").fetchall()
        ]
        self.assertEqual(columns, ["context_id"])

    def test_failed_v3_rolls_back_all_changes_and_retries_later(self):
        self._entity("new")
        self._entity("old")
        self._relation("sup", "new", "old")
        self._relation("self", "new", "new", "related_to")
        self.conn.execute("PRAGMA user_version = 2")
        self.conn.commit()
        self.conn.close()

        with patch(
            "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
            side_effect=sqlite3.OperationalError("forced cleanup failure"),
        ):
            self.conn = init_db(self.path)

        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(
            self.conn.execute("SELECT status FROM entities WHERE id='old'").fetchone()[0], "raw"
        )
        self.assertIsNone(
            self.conn.execute("SELECT invalid_at FROM relations WHERE id='self'").fetchone()[0]
        )

        self.conn.close()
        self.conn = init_db(self.path)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(
            self.conn.execute("SELECT status FROM entities WHERE id='old'").fetchone()[0],
            "archived",
        )

    def test_v3_does_not_leapfrog_failed_v2(self):
        self.conn.execute("PRAGMA user_version = 1")
        self.conn.commit()
        self.conn.close()

        with patch(
            "saltmdb.db.schema._migrate_predicate_drift",
            side_effect=sqlite3.OperationalError("forced v2 failure"),
        ):
            self.conn = init_db(self.path)

        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 1)

        self.conn.close()
        self.conn = init_db(self.path)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
