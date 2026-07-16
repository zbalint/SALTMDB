import unittest
import unittest.mock
import os
import sqlite3
import json
from datetime import datetime, UTC

# Import components from saltmdb_server
from saltmdb_server import (
    init_db,
    redact_secrets,
    merge_tags_heuristics,
    consolidate_memories,
    consolidate_cluttered_tags,
    decay_lru_memories,
    extract_title_and_snippet,
    CUSTOM_REDACT_PATTERNS,
    acquire_librarian_lock,
    release_librarian_lock,
    commit_consolidation,
    store_knowledge,
    search_memory,
    create_snapshot,
    store_relation,
    analyze_dependencies,
    archive_memory,
    get_recent_events
)

TEST_DB_PATH = "test_saltmdb.db"

class TestSALTMDB(unittest.TestCase):
    def setUp(self):
        self.old_db_path = os.environ.get("SALTMDB_DB_PATH")
        os.environ["SALTMDB_DB_PATH"] = os.path.abspath(TEST_DB_PATH)
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        # Ensure WAL journal files are also deleted
        for ext in ["-wal", "-shm"]:
            if os.path.exists(TEST_DB_PATH + ext):
                os.remove(TEST_DB_PATH + ext)
        self.conn = init_db(TEST_DB_PATH)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        for ext in ["-wal", "-shm"]:
            if os.path.exists(TEST_DB_PATH + ext):
                os.remove(TEST_DB_PATH + ext)
        # Restore environment variable
        if self.old_db_path is not None:
            os.environ["SALTMDB_DB_PATH"] = self.old_db_path
        else:
            os.environ.pop("SALTMDB_DB_PATH", None)
        # Clean up custom redactions
        if os.path.exists(".saltmdb_redact"):
            os.remove(".saltmdb_redact")
        import saltmdb_server
        saltmdb_server.CUSTOM_REDACT_PATTERNS = []

    def test_redact_secrets(self):
        text_with_github = "My key is ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        text_with_anthropic = "My key is sk-ant-sid01-1234567890abcdef1234567890abcdef"
        text_with_openai = "My key is sk-proj-1234567890abcdef1234567890abcdef"
        
        self.assertEqual(redact_secrets(text_with_github), "My key is [REDACTED_SECRET]")
        self.assertEqual(redact_secrets(text_with_anthropic), "My key is [REDACTED_SECRET]")
        self.assertEqual(redact_secrets(text_with_openai), "My key is [REDACTED_SECRET]")

    def test_custom_redaction(self):
        # Create a mock .saltmdb_redact file
        with open(".saltmdb_redact", "w", encoding="utf-8") as f:
            f.write("# This is a comment\n")
            f.write("custom_pattern_\\d+\n")
            
        import saltmdb_server
        saltmdb_server.load_custom_redact_patterns()
        
        test_text = "Some text with custom_pattern_12345 inside."
        self.assertEqual(saltmdb_server.redact_secrets(test_text), "Some text with [REDACTED_SECRET] inside.")

    def test_fts5_triggers(self):
        # Insert entity
        now = datetime.now(UTC).isoformat()
        entity_id = "test-entity-1"
        title = "Test Title"
        with self.conn:
            self.conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                VALUES (?, ?, ?, ?, 'owner1', 'shared', 0, 1, 'raw', '[]', ?, '# Test Title\nThis is a test memory.')
            """, (entity_id, now, now, now, title))
            
        # Verify trigger inserted into FTS5
        cursor = self.conn.execute("SELECT id, title, full_content FROM entities_fts WHERE id = ?", (entity_id,))
        fts_row = cursor.fetchone()
        self.assertIsNotNone(fts_row)
        self.assertEqual(fts_row[1], 'Test Title')
        self.assertEqual(fts_row[2], '# Test Title\nThis is a test memory.')

        # Update entity (not archived)
        new_title = 'Updated Title'
        new_content = '# Updated Title\nThis is updated.'
        with self.conn:
            self.conn.execute("UPDATE entities SET title = ?, full_content = ? WHERE id = ?", (new_title, new_content, entity_id))
            
        # Verify trigger updated FTS5
        cursor = self.conn.execute("SELECT title, full_content FROM entities_fts WHERE id = ?", (entity_id,))
        row = cursor.fetchone()
        self.assertEqual(row[0], new_title)
        self.assertEqual(row[1], new_content)

        # Archive entity
        with self.conn:
            self.conn.execute("UPDATE entities SET status = 'archived' WHERE id = ?", (entity_id,))
            
        # Verify trigger removed it from FTS5
        cursor = self.conn.execute("SELECT id FROM entities_fts WHERE id = ?", (entity_id,))
        self.assertIsNone(cursor.fetchone())

    def test_tag_canonical_merging(self):
        # Insert tags
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES ('t1', '#auth-error', NULL)")
            self.conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES ('t2', '#Auth_Error', NULL)")
            self.conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES ('t3', '#auth_error', NULL)")
            
            # Map entities to these tags
            self.conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, status, title, full_content) 
                VALUES ('e1', ?, ?, ?, 'raw', 'c1', 'c1')
            """, (now, now, now))
            self.conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, status, title, full_content) 
                VALUES ('e2', ?, ?, ?, 'raw', 'c2', 'c2')
            """, (now, now, now))
            
            self.conn.execute("INSERT INTO entity_tags (entity_id, tag_id) VALUES ('e1', 't2')")
            self.conn.execute("INSERT INTO entity_tags (entity_id, tag_id) VALUES ('e2', 't3')")

        merge_tags_heuristics(self.conn)

        # Verify that t2 and t3 canonical_id point to t1
        cursor = self.conn.execute("SELECT id, canonical_id FROM tags WHERE id IN ('t2', 't3')")
        for tag_id, canonical_id in cursor.fetchall():
            self.assertEqual(canonical_id, 't1')

        # Verify that entity mappings have updated
        cursor = self.conn.execute("SELECT entity_id, tag_id FROM entity_tags")
        mappings = cursor.fetchall()
        self.assertEqual(len(mappings), 2)
        for entity_id, tag_id in mappings:
            self.assertEqual(tag_id, 't1')

    def test_decay_lru_memories(self):
        # Insert entity accessed far in the past
        old_access_time = "2020-01-01T00:00:00"
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, title, full_content)
                VALUES ('decay-1', ?, ?, ?, 'owner1', 'shared', 0, 1, 'raw', 'Decay Test', 'Some content')
            """, (now, now, old_access_time))
            
        # Run decay
        decay_lru_memories(self.conn)
        
        # Verify status is now 'archived' (weight decayed from 1 to 0, which triggers archiving)
        cursor = self.conn.execute("SELECT status, weight FROM entities WHERE id = 'decay-1'")
        row = cursor.fetchone()
        self.assertEqual(row[0], 'archived')
        self.assertEqual(row[1], 0)

    def test_consolidate_cluttered_tags(self):
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO tags (id, name) VALUES ('tag-clutter', '#cluttered')")
            
            # Insert 5 raw entities sharing the cluttered tag
            for i in range(5):
                entity_id = f"e-clutter-{i}"
                self.conn.execute("""
                    INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, status, title, full_content)
                    VALUES (?, ?, ?, ?, 'agent1', 'shared', 'raw', ?, ?)
                """, (entity_id, now, now, now, f"Fact title {i}", f"# Fact {i}\nContent {i}"))
                self.conn.execute("INSERT INTO entity_tags (entity_id, tag_id) VALUES (?, 'tag-clutter')", (entity_id,))
                
        # Run tag cluster consolidation
        consolidate_cluttered_tags(self.conn)
        
        # Verify a consolidation_request event was logged
        cursor = self.conn.execute("SELECT content FROM events WHERE type = 'consolidation_request'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        data = json.loads(row[0])
        self.assertEqual(data["target"], "tag")
        self.assertEqual(data["tag_name"], "#cluttered")
        self.assertEqual(len(data["entity_ids"]), 5)
        
        # Simulate agent reading raw entities and calling commit_consolidation
        result = commit_consolidation(
            parent_ids=data["entity_ids"],
            title="Consolidated Memory for #cluttered",
            content="# Consolidated Memory for #cluttered\n\nContent 0 and Content 4 merged",
            tags=["#cluttered"],
            scope="shared",
            db_connection=self.conn
        )
        self.assertIn("Successfully committed", result)
        
        # Verify the 5 raw entities are physically deleted (algorithmic forgetting)
        cursor = self.conn.execute("SELECT status FROM entities WHERE id LIKE 'e-clutter-%'")
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 0)
            
        # Verify a new consolidated memory is created
        cursor = self.conn.execute("SELECT id, status, parent_ids, title, full_content FROM entities WHERE status = 'consolidated'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[3], "Consolidated Memory for #cluttered")
        self.assertIn("Content 0", row[4])
        self.assertIn("Content 4", row[4])

    def test_general_consolidation(self):
        # Insert 5 raw entities with tags (matching new threshold)
        now = datetime.now(UTC).isoformat()
        with self.conn:
            words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
            for i in range(1, 6):
                word = words[i]
                self.conn.execute(f"""
                    INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, status, title, full_content)
                    VALUES ('e{i}', ?, ?, ?, 'agent1', 'shared', 'raw', 'Fact {i}', '# Fact {i}\nFact number {word}')
                """, (now, now, now))
            
            self.conn.execute("INSERT INTO tags (id, name) VALUES ('t1', '#test')")
            for i in range(1, 6):
                self.conn.execute(f"INSERT INTO entity_tags (entity_id, tag_id) VALUES ('e{i}', 't1')")

        # Run consolidation (uses fallback since no LLM keys are set)
        consolidate_memories(self.conn)

        # Verify a consolidation_request event was logged
        cursor = self.conn.execute("SELECT content FROM events WHERE type = 'consolidation_request'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        data = json.loads(row[0])
        self.assertEqual(data["target"], "general")
        self.assertEqual(len(data["entity_ids"]), 5)

        # Simulate agent committing the consolidation
        result = commit_consolidation(
            parent_ids=data["entity_ids"],
            title="Consolidated Memory (general)",
            content="# Consolidated Memory\n\nFact number one and Fact number two merged",
            tags=["#test"],
            scope="shared",
            db_connection=self.conn
        )
        self.assertIn("Successfully committed", result)

        # Verify parent entities are physically deleted (algorithmic forgetting)
        cursor = self.conn.execute("SELECT id, status FROM entities WHERE id IN ('e1', 'e2', 'e3', 'e4', 'e5')")
        self.assertEqual(len(cursor.fetchall()), 0)

        # Verify a new consolidated entity exists
        cursor = self.conn.execute("SELECT id, status, parent_ids, full_content FROM entities WHERE status = 'consolidated'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        consolidated_id = row[0]
        parent_ids = json.loads(row[2])
        self.assertIn('e1', parent_ids)
        self.assertIn('e2', parent_ids)
        self.assertIn('Fact number one', row[3])
        self.assertIn('Fact number two', row[3])

        # Verify new entity has tag t1
        cursor = self.conn.execute("SELECT tag_id FROM entity_tags WHERE entity_id = ?", (consolidated_id,))
        self.assertEqual(cursor.fetchone()[0], 't1')

    def test_system_locks(self):
        # 1. Acquire lock initially
        success = acquire_librarian_lock(self.conn)
        self.assertTrue(success)

        # 2. Try to acquire again (should fail)
        second_attempt = acquire_librarian_lock(self.conn)
        self.assertFalse(second_attempt)

        # 3. Release lock
        release_librarian_lock(self.conn)

        # 4. Acquire again (should succeed now)
        third_attempt = acquire_librarian_lock(self.conn)
        self.assertTrue(third_attempt)

        # 5. Lock expiry fail-safe check:
        # Mock the lock time to be 15 minutes ago
        with self.conn:
            self.conn.execute("""
                UPDATE _system_locks 
                SET locked_at = datetime('now', '-15 minutes'), locked_by_pid = 99999
                WHERE task_name = 'librarian_consolidation'
            """)
        
        # Should be able to acquire/hijack expired lock
        fourth_attempt = acquire_librarian_lock(self.conn)
        self.assertTrue(fourth_attempt)

    def test_store_knowledge_upsert(self):
        # Store new memory
        res = store_knowledge(
            content="# Upsert Test\nInitial content here.",
            tags=["#test"],
            scope="shared",
            owner_id="test_agent",
            weight=1,
            entity_id="test-upsert-uuid"
        )
        self.assertIn("stored successfully", res)
        
        # Verify insertion
        cursor = self.conn.execute("SELECT full_content, weight FROM entities WHERE id = 'test-upsert-uuid'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "# Upsert Test\nInitial content here.")
        self.assertEqual(row[1], 1)
        
        # Upsert (update) same memory
        res2 = store_knowledge(
            content="# Upsert Test (Updated)\nNew content here.",
            tags=["#new-tag"],
            scope="shared",
            owner_id="test_agent",
            weight=5,
            entity_id="test-upsert-uuid"
        )
        self.assertIn("stored successfully", res2)
        
        # Verify update
        cursor = self.conn.execute("SELECT full_content, weight FROM entities WHERE id = 'test-upsert-uuid'")
        row = cursor.fetchone()
        self.assertEqual(row[0], "# Upsert Test (Updated)\nNew content here.")
        self.assertEqual(row[1], 5)
        
        # Verify tag update (old tag cleared, new tag linked)
        cursor = self.conn.execute("SELECT t.name FROM tags t JOIN entity_tags et ON t.id = et.tag_id WHERE et.entity_id = 'test-upsert-uuid'")
        tags = [r[0] for r in cursor.fetchall()]
        self.assertEqual(tags, ["#new-tag"])

    def test_multi_agent_isolation(self):
        # Store memory for agent1
        store_knowledge(
            content="# Agent1 Fact\nAgent1 content.",
            tags=["#isolated"],
            scope="shared",
            owner_id="agent1"
        )
        # Store memory for agent2
        store_knowledge(
            content="# Agent2 Fact\nAgent2 content.",
            tags=["#isolated"],
            scope="shared",
            owner_id="agent2"
        )
        
        # Search as agent1
        results1 = search_memory(query_keywords="Fact", owner_id="agent1")
        titles1 = [r["title"] for r in results1]
        self.assertIn("Agent1 Fact", titles1)
        self.assertNotIn("Agent2 Fact", titles1)
        
        # Search as agent2
        results2 = search_memory(query_keywords="Fact", owner_id="agent2")
        titles2 = [r["title"] for r in results2]
        self.assertIn("Agent2 Fact", titles2)
        self.assertNotIn("Agent1 Fact", titles2)
        
        # Search without owner_id (should return error payload)
        results_err = search_memory(query_keywords="Fact")
        self.assertEqual(len(results_err), 1)
        self.assertIn("error", results_err[0])
        self.assertIn("owner_id is mandatory", results_err[0]["error"])

    def test_create_snapshot(self):
        # Test snapshot backup utility
        res = create_snapshot()
        self.assertIn("snapshot successfully created", res)
        
        # Find created file path from output
        backup_path = res.split(": ")[-1].strip()
        self.assertTrue(os.path.exists(backup_path))
        
        # Cleanup backup file
        if os.path.exists(backup_path):
            os.remove(backup_path)
        # Cleanup WAL backup files if any
        for ext in ["-wal", "-shm"]:
            if os.path.exists(backup_path + ext):
                os.remove(backup_path + ext)

    def test_temporal_versioning_scd(self):
        # 1. Insert original memory
        store_knowledge(
            content="# Original Fact\nVersion 1 content.",
            tags=["#ver1"],
            scope="shared",
            owner_id="test_agent",
            entity_id="temp-ver-uuid"
        )
        
        # Verify it has valid_from set and valid_to is NULL
        cursor = self.conn.execute("SELECT valid_from, valid_to FROM entities WHERE id = 'temp-ver-uuid'")
        row = cursor.fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNone(row[1])
        original_valid_from = row[0]
        
        # 2. Update memory to trigger temporal history copy (SCD Type 2)
        store_knowledge(
            content="# Updated Fact\nVersion 2 content.",
            tags=["#ver2"],
            scope="shared",
            owner_id="test_agent",
            entity_id="temp-ver-uuid"
        )
        
        # Verify active row was updated
        cursor = self.conn.execute("SELECT full_content, valid_from, valid_to FROM entities WHERE id = 'temp-ver-uuid'")
        row = cursor.fetchone()
        self.assertEqual(row[0], "# Updated Fact\nVersion 2 content.")
        self.assertIsNotNone(row[1])
        self.assertIsNone(row[2])
        self.assertNotEqual(row[1], original_valid_from)
        
        # Verify a historical row was created with status = 'archived' and valid_to set to current time
        cursor = self.conn.execute("""
            SELECT id, full_content, valid_from, valid_to, status 
            FROM entities 
            WHERE id LIKE 'temp-ver-uuid_h_%'
        """)
        hist_rows = cursor.fetchall()
        self.assertEqual(len(hist_rows), 1)
        hist_id, hist_content, hist_from, hist_to, hist_status = hist_rows[0]
        self.assertEqual(hist_content, "# Original Fact\nVersion 1 content.")
        self.assertEqual(hist_status, 'archived')
        self.assertEqual(hist_from, original_valid_from)
        self.assertIsNotNone(hist_to)
        
        # Verify tags for both the active and historical entities
        cursor = self.conn.execute("SELECT t.name FROM tags t JOIN entity_tags et ON t.id = et.tag_id WHERE et.entity_id = ?", (hist_id,))
        hist_tags = [r[0] for r in cursor.fetchall()]
        self.assertEqual(hist_tags, ["#ver1"])
        
        cursor = self.conn.execute("SELECT t.name FROM tags t JOIN entity_tags et ON t.id = et.tag_id WHERE et.entity_id = 'temp-ver-uuid'")
        active_tags = [r[0] for r in cursor.fetchall()]
        self.assertEqual(active_tags, ["#ver2"])

    def test_relations_and_cte_traversal(self):
        # 1. Insert memory nodes
        store_knowledge(content="# Core Component\nDescription.", tags=["#sys"], scope="shared", owner_id="ops", entity_id="node-core")
        store_knowledge(content="# Dependency A\nDescription.", tags=["#sys"], scope="shared", owner_id="ops", entity_id="node-dep-a")
        store_knowledge(content="# Dependency B\nDescription.", tags=["#sys"], scope="shared", owner_id="ops", entity_id="node-dep-b")
        
        # 2. Store relationships (relations)
        res1 = store_relation(source_id="node-core", target_id="node-dep-a", predicate="depends_on")
        res2 = store_relation(source_id="node-dep-a", target_id="node-dep-b", predicate="depends_on")
        self.assertIn("Relation successfully stored", res1)
        self.assertIn("Relation successfully stored", res2)
        
        # 3. Analyze dependencies recursively
        deps = analyze_dependencies(root_entity_id="node-core")
        self.assertEqual(len(deps), 3)
        
        paths = [d["path"] for d in deps]
        self.assertIn("Core Component", paths)
        self.assertIn("Core Component -> Dependency A", paths)
        self.assertIn("Core Component -> Dependency A -> Dependency B", paths)

    def test_store_knowledge_title_deduplication(self):
        # 1. Insert first memory
        store_knowledge(
            content="# Same Title\nContent version 1.",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="Deduplication Fact"
        )
        
        # 2. Insert second memory with same title and owner, but without passing entity_id
        store_knowledge(
            content="# Same Title\nContent version 2.",
            tags=["#test-updated"],
            scope="shared",
            owner_id="agent1",
            title="Deduplication Fact"
        )
        
        # Verify that only ONE active entity exists (the second one, with temporal history cloned)
        cursor = self.conn.execute("SELECT id, full_content FROM entities WHERE title = 'Deduplication Fact' AND status = 'raw'")
        active_rows = cursor.fetchall()
        self.assertEqual(len(active_rows), 1)
        self.assertIn("Content version 2", active_rows[0][1])
        
        # Verify history copy exists (cloned due to title match)
        cursor = self.conn.execute("SELECT id, full_content FROM entities WHERE title = 'Deduplication Fact' AND status = 'archived'")
        hist_rows = cursor.fetchall()
        self.assertEqual(len(hist_rows), 1)
        self.assertIn("Content version 1", hist_rows[0][1])

    def test_pre_write_tag_normalization(self):
        # 1. Store memory with canonical tag
        store_knowledge(
            content="# Normalization Fact\nContent.",
            tags=["#Auth-Error"],
            scope="shared",
            owner_id="agent1",
            title="Norm Fact"
        )
        
        # 2. Store another memory with case/hyphen drifted tag name
        store_knowledge(
            content="# Normalization Fact 2\nContent.",
            tags=["#auth_error"],
            scope="shared",
            owner_id="agent1",
            title="Norm Fact 2"
        )
        
        # Verify that both memories point to the EXACT same tag ID (drift prevention)
        cursor = self.conn.execute("SELECT name FROM tags")
        all_tags = [r[0] for r in cursor.fetchall()]
        # Tag table should only have '#Auth-Error', not duplicate alias rows
        self.assertIn("#Auth-Error", all_tags)
        self.assertNotIn("#auth_error", all_tags)

    def test_consolidation_hygiene_runbooks(self):
        # Insert 3 raw entities with '#ops-runbook' tag (meets the high hygiene threshold of 3)
        now = datetime.now(UTC).isoformat()
        with self.conn:
            for i in range(3):
                self.conn.execute(f"""
                    INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, status, title, full_content)
                    VALUES ('runbook-{i}', ?, ?, ?, 'agent1', 'shared', 'raw', 'Runbook {i}', 'Runbook content {i}')
                """, (now, now, now))
                
            self.conn.execute("INSERT OR IGNORE INTO tags (id, name) VALUES ('t-runbook', '#ops-runbook')")
            for i in range(3):
                self.conn.execute(f"INSERT INTO entity_tags (entity_id, tag_id) VALUES ('runbook-{i}', 't-runbook')")
                
        # Run tag consolidation
        consolidate_cluttered_tags(self.conn)
        
        # Verify a consolidation_request event was logged (due to threshold of 3 for runbook tag)
        cursor = self.conn.execute("SELECT content FROM events WHERE type = 'consolidation_request' AND agent_id = 'agent1'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        data = json.loads(row[0])
        self.assertEqual(data["target"], "tag")
        self.assertEqual(data["tag_name"], "#ops-runbook")
        self.assertEqual(len(data["entity_ids"]), 3)

    @unittest.mock.patch("subprocess.Popen")
    @unittest.mock.patch("socket.socket")
    @unittest.mock.patch("urllib.request.urlopen")
    def test_db_viewer_start_stop(self, mock_urlopen, mock_socket, mock_popen):
        from saltmdb_server import start_db_viewer, stop_db_viewer
        import urllib.error
        
        # Mock already running case
        mock_urlopen.return_value.__enter__.return_value.status = 200
        res = start_db_viewer()
        self.assertIn("already running", res.lower())
        
        # Mock not running case (start starts it)
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        res2 = start_db_viewer()
        self.assertIn("started successfully", res2.lower())
        mock_popen.assert_called_once()
        
        # Mock stop - connection refused (not running)
        mock_socket.return_value.connect.side_effect = Exception("conn failed")
        res3 = stop_db_viewer()
        self.assertIn("not running", res3.lower())
        
        # Mock stop - connection success (running)
        mock_socket.return_value.connect.side_effect = None
        with unittest.mock.patch("subprocess.check_output") as mock_check_output:
            mock_check_output.return_value = "  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       9999"
            with unittest.mock.patch("subprocess.run") as mock_run:
                res4 = stop_db_viewer()
                self.assertIn("stopped successfully", res4.lower())
                mock_run.assert_called()

    def test_archive_memory(self):
        # 1. Store a memory
        store_knowledge(
            owner_id="agent1",
            content="# Archivable Fact\nThis is an archivable fact.",
            tags=["#test"],
            scope="shared",
            entity_id="uuid-archivable"
        )
        
        # 2. Assert it is raw/active
        cursor = self.conn.execute("SELECT status FROM entities WHERE id = 'uuid-archivable'")
        self.assertEqual(cursor.fetchone()[0], "raw")
        
        # 3. Archive it
        res = archive_memory(entity_id="uuid-archivable", owner_id="agent1")
        self.assertIn("successfully archived", res)
        
        # 4. Assert it is archived
        cursor = self.conn.execute("SELECT status, valid_to FROM entities WHERE id = 'uuid-archivable'")
        row = cursor.fetchone()
        self.assertEqual(row[0], "archived")
        self.assertIsNotNone(row[1])
        
        # 5. Assert error if archiving non-existent or wrong owner
        res_err = archive_memory(entity_id="uuid-archivable", owner_id="wrong_owner")
        self.assertIn("error", res_err.lower())

    def test_get_recent_events_dynamic_status(self):
        # 1. Insert raw entities
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, status, title, full_content) VALUES ('e-dyn-1', ?, ?, ?, 'agent1', 'shared', 'raw', 'Title 1', 'Content 1')", (now, now, now))
            self.conn.execute("INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, status, title, full_content) VALUES ('e-dyn-2', ?, ?, ?, 'agent1', 'shared', 'raw', 'Title 2', 'Content 2')", (now, now, now))
            
        # 2. Log consolidation event manually
        event_content = json.dumps({"target": "general", "entity_ids": ["e-dyn-1", "e-dyn-2"]})
        with self.conn:
            self.conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content)
                VALUES ('event-dyn-1', ?, 'agent1', 'consolidation_request', ?)
            """, (now, event_content))
            
        # 3. Fetch events, assert status is pending
        events = get_recent_events(agent_id="agent1", type_filter="consolidation_request")
        self.assertEqual(events[0]["status"], "pending")
        
        # 4. Consolidate them
        commit_consolidation(
            parent_ids=["e-dyn-1", "e-dyn-2"],
            title="Consolidated Dyn",
            content="# Consolidated Title\nMerged content.",
            tags=["#dyn"],
            db_connection=self.conn
        )
        
        # 5. Fetch events again, assert status has dynamically changed to resolved!
        events_after = get_recent_events(agent_id="agent1", type_filter="consolidation_request")
        self.assertEqual(events_after[0]["status"], "resolved")

if __name__ == "__main__":
    unittest.main()
