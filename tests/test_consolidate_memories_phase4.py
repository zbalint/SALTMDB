import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import (
    bulk_consolidate_memories,
    consolidate_memories,
    store_relation,
)


class TestConsolidateMemoriesPhase4(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _parent(self, title: str) -> str:
        result = store_memory(
            title=title,
            content=f"Detailed, durable description of {title} used in consolidation tests.",
            owner_id="agent_db",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def _cohesion(self, ids, _conn, _db_path):
        state = {
            row[0]: (row[1], row[2])
            for row in self.conn.execute(
                f"SELECT id, content_hash, status FROM entities WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        }
        vectors = {entity_id: [1.0, 0.0] for entity_id in ids}
        return vectors, {}, state

    def test_success_archives_parents_and_returns_optional_worklist_without_repointing(self):
        parent_a = self._parent("Parent A")
        parent_b = self._parent("Parent B")
        neighbor = self._parent("Neighbor")
        parent_bytes = self.conn.execute(
            "SELECT title, full_content, content_hash, parent_ids FROM entities WHERE id = ?",
            (parent_a,),
        ).fetchone()
        relation = store_relation(
            source_id=parent_a,
            target_id=neighbor,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertNotIn("Error", relation)
        before = self.conn.execute(
            "SELECT source_id, target_id, predicate, valid_to FROM relations "
            "WHERE source_id = ? AND target_id = ?",
            (parent_a, neighbor),
        ).fetchone()

        with patch.object(
            __import__(
                "saltmdb.domain.services.relation_service", fromlist=["get_fresh_entity_centroids"]
            ),
            "get_fresh_entity_centroids",
            side_effect=self._cohesion,
        ):
            result = consolidate_memories(
                parent_ids=[parent_a, parent_b],
                title="Canonical summary",
                content="A coherent canonical summary of both parent memories.",
                owner_id="agent_db",
                db_connection=self.conn,
            )

        self.assertEqual(result["status"], "ok")
        data = result["data"]
        self.assertEqual(len(data["orphaned_relations"]), 1)
        item = data["orphaned_relations"][0]
        self.assertEqual(item["predicate"], "related_to")
        self.assertEqual(item["other_endpoint"], neighbor)
        self.assertEqual(item["originating_parent"], parent_a)
        self.assertEqual(item["source_id"], parent_a)
        self.assertEqual(item["target_id"], neighbor)

        after = self.conn.execute(
            "SELECT source_id, target_id, predicate, valid_to FROM relations "
            "WHERE source_id = ? AND target_id = ?",
            (parent_a, neighbor),
        ).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE predicate = 'related_to'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE predicate = 'consolidated_from' AND source_id = ?",
                (data["entity_id"],),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT title, full_content, content_hash, parent_ids FROM entities WHERE id = ?",
                (parent_a,),
            ).fetchone(),
            parent_bytes,
        )

    def test_one_parent_unknown_and_archived_parent_reject_with_zero_writes(self):
        parent = self._parent("Only Parent")
        before_entities = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        one = consolidate_memories(
            parent_ids=[parent],
            title="Should reject",
            content="A valid enough summary that should not be written.",
            db_connection=self.conn,
        )
        self.assertEqual(one["status"], "rejected")
        self.assertEqual(one["errors"][0]["code"], "REJECT_PARENT_COUNT")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )

        unknown = consolidate_memories(
            parent_ids=[parent, "not-an-entity"],
            title="Should reject",
            content="A valid enough summary that should not be written.",
            db_connection=self.conn,
        )
        self.assertEqual(unknown["errors"][0]["code"], "UNKNOWN_PARENT_ID")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )

        archived = self._parent("Archived Parent")
        self.conn.execute("UPDATE entities SET status = 'archived' WHERE id = ?", (archived,))
        self.conn.commit()
        inactive = consolidate_memories(
            parent_ids=[parent, archived],
            title="Should reject",
            content="A valid enough summary that should not be written.",
            db_connection=self.conn,
        )
        self.assertEqual(inactive["errors"][0]["code"], "INACTIVE_PARENT")
        self.assertIn("predicate='corrects'", inactive["errors"][0]["message"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'archived'").fetchone()[
                0
            ],
            1,
        )

    def test_bulk_success_preserves_each_item_worklist(self):
        parent_a = self._parent("Bulk Parent A")
        parent_b = self._parent("Bulk Parent B")
        neighbor = self._parent("Bulk Neighbor")
        self.assertNotIn(
            "Error",
            store_relation(
                source_id=parent_a,
                target_id=neighbor,
                predicate="related_to",
                db_connection=self.conn,
            ),
        )

        with patch.object(
            __import__(
                "saltmdb.domain.services.relation_service", fromlist=["get_fresh_entity_centroids"]
            ),
            "get_fresh_entity_centroids",
            side_effect=self._cohesion,
        ):
            results = bulk_consolidate_memories(
                consolidations=[
                    {
                        "parent_ids": [parent_a, parent_b],
                        "title": "Bulk Canonical",
                        "content": "A coherent canonical summary for the bulk operation.",
                    }
                ],
                db_connection=self.conn,
            )

        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(len(results[0]["orphaned_relations"]), 1)
        self.assertEqual(results[0]["orphaned_relations"][0]["originating_parent"], parent_a)


if __name__ == "__main__":
    unittest.main()
