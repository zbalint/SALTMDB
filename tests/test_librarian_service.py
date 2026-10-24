from saltmdb.domain.services.memory_service import Provenance
import unittest
import tempfile
import os
import shutil
from unittest.mock import MagicMock

from saltmdb.db.schema import init_db
from saltmdb.domain.services import librarian_service
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation, bulk_commit_consolidation


class TestLibrarianService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def test_maintenance_logs_process_memory_after_optimize(self):
        conn = MagicMock()
        with self.assertLogs("saltmdb.domain.services.librarian_service", "INFO") as logs:
            librarian_service._run_librarian_maintenance(conn)

        memory_records = [
            record
            for record in logs.records
            if record.getMessage().startswith("Process memory (librarian pass): rss_mb=")
        ]
        self.assertEqual(len(memory_records), 1)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_commit_consolidation_soft_archives_parents_and_links_lineage(self):
        res1 = store_memory(
            title="Parent Fact A",
            content="Detailed description of Fact A for testing consolidation",
            db_path=self.db_path,
            provenance=Provenance(agent_id="agent1"),
        )
        id1 = res1["data"]["id"]

        res2 = store_memory(
            title="Parent Fact B",
            content="Detailed description of Fact B for testing consolidation",
            db_path=self.db_path,
            provenance=Provenance(agent_id="agent1"),
        )
        id2 = res2["data"]["id"]

        c_res = commit_consolidation(
            parent_ids=[id1, id2],
            title="Consolidated Overview",
            content="Merged summary of A and B",
            tags=["#summary"],
            provenance=Provenance(agent_id="agent1"),
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", c_res)

        # Verify parent status is archived
        p1 = self.conn.execute(
            "SELECT status, embedding_status FROM entities WHERE id = ?", (id1,)
        ).fetchone()
        p2 = self.conn.execute(
            "SELECT status, embedding_status FROM entities WHERE id = ?", (id2,)
        ).fetchone()
        self.assertEqual(p1[0], "archived")
        self.assertEqual(p1[1], "archived")
        self.assertEqual(p2[0], "archived")
        self.assertEqual(p2[1], "archived")

    def test_bulk_commit_consolidation_is_all_or_nothing(self):
        parents = []
        for title in (
            "Bulk Atomicity Parent A",
            "Bulk Atomicity Parent B",
            "Bulk Atomicity Parent C",
            "Bulk Atomicity Parent D",
        ):
            result = store_memory(
                title=title,
                content=f"Detailed description of {title} for the bulk atomicity regression test",
                db_path=self.db_path,
                provenance=Provenance(agent_id="agent1"),
            )
            parents.append(result["data"]["id"])

        results = bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": parents[:2],
                    "title": "Would-Be Valid Consolidation",
                    "content": "This item is valid on its own and would succeed in isolation",
                },
                {
                    "parent_ids": parents[2:],
                    "title": "Rejected In Transaction",
                    "content": "test " * 40,
                },
            ],
            db_connection=self.conn,
        )

        # Whole batch reports as a single top-level error, not a mixed success/error list.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

        # The first item's consolidated entity must NOT exist -- proving the whole batch
        # rolled back rather than item 1 silently committing while item 2 failed.
        row = self.conn.execute(
            "SELECT id FROM entities WHERE title = ?", ("Would-Be Valid Consolidation",)
        ).fetchone()
        self.assertIsNone(row)

        # Both first-item parents must remain raw after the rejected second item.
        statuses = self.conn.execute(
            "SELECT status FROM entities WHERE id IN (?, ?) ORDER BY id", tuple(parents[:2])
        ).fetchall()
        self.assertEqual(statuses, [("raw",), ("raw",)])

    def test_merge_tags_heuristics_merges_normalized_duplicate_tags(self):
        self.conn.execute(
            "INSERT INTO tags (id, name, canonical_id) VALUES (?, ?, NULL)",
            ("tag-a", "#Topic"),
        )
        self.conn.execute(
            "INSERT INTO tags (id, name, canonical_id) VALUES (?, ?, NULL)",
            ("tag-b", "#topic"),
        )
        memory = store_memory(
            title="Tag merge association",
            content="Association target content with enough detail for the memory quality checks.",
            db_path=self.db_path,
            provenance=Provenance(agent_id="agent1"),
        )
        self.assertIsInstance(memory, dict)
        self.assertEqual(memory["status"], "ok", memory)
        entity_id = memory["data"]["id"]
        self.conn.execute(
            "INSERT INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
            (entity_id, "tag-b"),
        )
        self.conn.commit()
        librarian_service.merge_tags_heuristics(conn=self.conn)
        rows = self.conn.execute(
            "SELECT id, name, canonical_id FROM tags WHERE id IN ('tag-a', 'tag-b') ORDER BY id"
        ).fetchall()
        self.assertEqual(rows, [("tag-a", "#Topic", None), ("tag-b", "#topic", "tag-a")])
        self.assertEqual(
            self.conn.execute(
                "SELECT tag_id FROM entity_tags WHERE entity_id = ?", (entity_id,)
            ).fetchall(),
            [("tag-a",)],
        )
