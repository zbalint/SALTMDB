import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import get_memory


class TestGetMemory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "get-memory.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert(self, entity_id, title, status="raw"):
        self.conn.execute(
            """INSERT INTO entities
               (id, created_at, updated_at, last_accessed_at, title, full_content, status)
               VALUES (?, datetime('now'), datetime('now'), datetime('now'), ?, ?, ?)""",
            (entity_id, title, f"# {title}\n\nFull body for {title}.", status),
        )
        self.conn.commit()

    def test_returns_full_active_memory_in_envelope(self):
        entity_id = "12345678-1234-1234-1234-123456789abc"
        self._insert(entity_id, "Active memory")

        result = get_memory(entity_id="12345678", db_connection=self.conn)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["id"], entity_id)
        self.assertEqual(result["data"]["status"], "raw")
        self.assertIn("Full body", result["data"]["content"])
        self.assertEqual(result["data"]["lineage"], {"ancestors": [], "descendants": []})

    def test_returns_archived_entity_without_redirecting(self):
        archived = "abcdefab-1234-1234-1234-123456789abc"
        successor = "fedcba98-1234-1234-1234-123456789abc"
        self._insert(archived, "Archived memory", status="archived")
        self._insert(successor, "Successor memory")

        with patch(
            "saltmdb.domain.services.relation_service.get_lineage",
            return_value={"nodes": [{"id": successor, "depth": 1, "status": "raw"}]},
            create=True,
        ) as lineage:
            result = get_memory(entity_id=archived, db_connection=self.conn)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["id"], archived)
        self.assertEqual(result["data"]["status"], "archived")
        self.assertEqual(lineage.call_count, 2)

    def test_ambiguous_prefix_is_structured_error(self):
        self._insert("a1b2c3d4-1234-1234-1234-123456789abc", "One")
        self._insert("a1b2c3d4-5678-1234-1234-123456789abc", "Two")

        result = get_memory(entity_id="a1b2c3d4", db_connection=self.conn)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "AMBIGUOUS_ID_PREFIX")
        self.assertEqual({c["title"] for c in result["errors"][0]["candidates"]}, {"One", "Two"})

    def test_unknown_id_is_structured_error(self):
        result = get_memory(entity_id="does-not-exist", db_connection=self.conn)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "UNKNOWN_ENTITY_ID")
