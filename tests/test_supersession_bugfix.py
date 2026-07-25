import unittest
import tempfile
import os
import shutil
import json
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service, event_service

class TestSupersessionBugfix(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "true"

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        if "SALTMDB_ENABLE_SEMANTIC" in os.environ:
            del os.environ["SALTMDB_ENABLE_SEMANTIC"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unreviewed_weight_preservation(self):
        """Test #1: Storing a matching memory does NOT lower existing memory weight or store_relation edge."""
        orig_res = memory_service.store_memory(
            content="Production system nofile ulimit setting was configured to 1048576 (1 million) in /etc/security/limits.conf",
            title="Linux ulimit Configuration",
            weight=10,
            owner_id="user1",
            skip_duplicate_check=True,
            db_connection=self.conn
        )
        orig_id = orig_res.split("ID: ")[1].strip()

        # Store reworded update
        new_res = memory_service.store_memory(
            content="Updated Linux open files limit constraint (nofile) value adjusted down to 163840 in limits configuration",
            title="Linux ulimit Configuration Update",
            owner_id="user1",
            db_connection=self.conn
        )

        # Assert existing memory weight remains 10
        row = self.conn.execute("SELECT weight FROM entities WHERE id = ?", (orig_id,)).fetchone()
        self.assertEqual(row[0], 10, "Existing memory weight should NOT be demoted automatically to 1")

        # Assert no auto-created 'supersedes' relation edge exists
        rel = self.conn.execute("SELECT id FROM relations WHERE predicate = 'supersedes' AND target_id = ?", (orig_id,)).fetchone()
        self.assertIsNone(rel, "No 'supersedes' relation edge should be automatically created without confirmation")

    def test_event_payload_structure(self):
        """Test #2: supersession_candidate event is logged with exact JSON payload structure."""
        orig_res = memory_service.store_memory(
            content="Production system nofile ulimit setting was configured to 1048576 (1 million) in /etc/security/limits.conf",
            title="Linux ulimit Configuration",
            owner_id="user1",
            skip_duplicate_check=True,
            db_connection=self.conn
        )
        orig_id = orig_res.split("ID: ")[1].strip()

        # Store reworded update matching candidate threshold
        new_res = memory_service.store_memory(
            content="Updated Linux open files limit constraint (nofile) value adjusted down to 163840 in limits configuration",
            title="Linux ulimit Configuration Update",
            owner_id="user1",
            db_connection=self.conn
        )

        # Query short-term events for supersession_candidate
        events = event_service.get_recent_events(type_filter="supersession_candidate", db_connection=self.conn)
        self.assertTrue(len(events) > 0, "A supersession_candidate event should be logged")
        
        payload = json.loads(events[0]["content"])
        self.assertIn("new_entity_id", payload)
        self.assertIn("target_entity_id", payload)
        self.assertEqual(payload["target_entity_id"], orig_id)
        self.assertIn("similarity_score", payload)
        self.assertGreaterEqual(payload["similarity_score"], 0.75)
        self.assertIn("target_title", payload)

    def test_distinct_fact_negative(self):
        """Test #3: Topically related distinct facts (<0.85 cosine) trigger NO duplicate warning."""
        fact1_res = memory_service.store_memory(
            content="Configured PostgreSQL max_connections setting to 500 and shared_buffers to 4GB in postgresql.conf",
            title="PostgreSQL Memory Configuration",
            owner_id="user1",
            skip_duplicate_check=True,
            db_connection=self.conn
        )
        
        fact2_res = memory_service.store_memory(
            content="Database backup script dumping PostgreSQL tables to S3 bucket daily",
            title="PostgreSQL Backup Configuration",
            owner_id="user1",
            db_connection=self.conn
        )

        self.assertNotIn("[WARNING: Potential duplicate", fact2_res, "Distinct fact should NOT trigger duplicate warning")
        
        events = event_service.get_recent_events(type_filter="supersession_candidate", db_connection=self.conn)
        self.assertEqual(len(events), 0, "Distinct fact should NOT trigger supersession_candidate event")

    def test_semantic_disabled_safety(self):
        """Test #4: Disabling semantic search completely suppresses supersession_candidate events."""
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "false"
        
        orig_res = memory_service.store_memory(
            content="Production system nofile ulimit setting was configured to 1048576 (1 million) in /etc/security/limits.conf",
            title="Linux ulimit Configuration",
            owner_id="user1",
            skip_duplicate_check=True,
            db_connection=self.conn
        )

        new_res = memory_service.store_memory(
            content="Updated Linux open files limit constraint (nofile) value adjusted down to 163840 in limits configuration",
            title="Linux ulimit Configuration Update",
            owner_id="user1",
            db_connection=self.conn
        )

        events = event_service.get_recent_events(type_filter="supersession_candidate", db_connection=self.conn)
        self.assertEqual(len(events), 0, "When SALTMDB_ENABLE_SEMANTIC is false, no supersession_candidate events should be logged")

    def test_store_and_warn_contract(self):
        """Test #5: Duplicate detection stores memory to SQLite and appends [WARNING: ...] string suffix."""
        orig_res = memory_service.store_memory(
            content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer headers",
            title="OAuth2 Core Specification",
            owner_id="user1",
            skip_duplicate_check=True,
            db_connection=self.conn
        )
        orig_id = orig_res.split("ID: ")[1].strip()

        dup_res = memory_service.store_memory(
            content="Token authentication via OAuth2 protocol with JWT refresh tokens and bearer authorization headers",
            title="OAuth2 Specification Update",
            owner_id="user1",
            db_connection=self.conn
        )

        self.assertIn("Knowledge stored successfully with ID:", dup_res)
        self.assertIn("[WARNING: Potential duplicate of existing memory", dup_res)

        new_id = dup_res.split("ID: ")[1].split(" [WARNING:")[0].strip()
        row = self.conn.execute("SELECT id FROM entities WHERE id = ?", (new_id,)).fetchone()
        self.assertIsNotNone(row, "New memory should be stored in SQLite despite duplicate warning")

if __name__ == "__main__":
    unittest.main()
