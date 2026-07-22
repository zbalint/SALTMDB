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
    store_memory,
    search_memory,
    create_snapshot,
    store_relation,
    analyze_dependencies,
    archive_memory,
    get_recent_events,
    log_event,
    get_session_summary,
    detect_orphaned_memories,
    check_duplicate_memories,
    scan_memories,
    fetch_memory_chunk,
    bulk_commit_consolidation,
    bulk_archive_memory,
    bulk_store_relations
)

TEST_DB_PATH = "test_saltmdb_run.db"

class TestSALTMDB(unittest.TestCase):
    def setUp(self):
        import saltmdb_server
        self.old_trigger = saltmdb_server.trigger_librarian
        saltmdb_server.trigger_librarian = lambda: None
        self.old_db_path = os.environ.get("SALTMDB_DB_PATH")
        
        # Unique database path per test method to completely avoid locks and pollution
        self.db_name = f"test_saltmdb_{self._testMethodName}.db"
        os.environ["SALTMDB_DB_PATH"] = os.path.abspath(self.db_name)
        if os.path.exists(self.db_name):
            try:
                os.remove(self.db_name)
            except PermissionError:
                pass
        # Ensure WAL journal files are also deleted
        for ext in ["-wal", "-shm"]:
            if os.path.exists(self.db_name + ext):
                try:
                    os.remove(self.db_name + ext)
                except PermissionError:
                    pass
        self.conn = init_db(self.db_name)

    def tearDown(self):
        self.conn.close()
        import saltmdb_server
        saltmdb_server.trigger_librarian = self.old_trigger
        if os.path.exists(self.db_name):
            try:
                os.remove(self.db_name)
            except PermissionError:
                pass
        for ext in ["-wal", "-shm"]:
            if os.path.exists(self.db_name + ext):
                try:
                    os.remove(self.db_name + ext)
                except PermissionError:
                    pass
        # Restore environment variable
        if self.old_db_path is not None:
            os.environ["SALTMDB_DB_PATH"] = self.old_db_path
        else:
            os.environ.pop("SALTMDB_DB_PATH", None)
        # Clean up custom redactions
        if os.path.exists(".saltmdb_redact"):
            os.remove(".saltmdb_redact")
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
        
        # Verify status remains 'raw' per REVIEW_1.md (decay removed; archive only on supersession/consolidation)
        cursor = self.conn.execute("SELECT status, weight FROM entities WHERE id = 'decay-1'")
        row = cursor.fetchone()
        self.assertEqual(row[0], 'raw')
        self.assertEqual(row[1], 1)

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
        
        # Verify the 5 raw entities are archived (algorithmic forgetting)
        cursor = self.conn.execute("SELECT status FROM entities WHERE id LIKE 'e-clutter-%'")
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertEqual(r[0], 'archived')
            
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

        # Verify parent entities are archived (algorithmic forgetting)
        cursor = self.conn.execute("SELECT id, status FROM entities WHERE id IN ('e1', 'e2', 'e3', 'e4', 'e5')")
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertEqual(r[1], 'archived')

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

    def test_store_memory_upsert(self):
        # Store new memory
        res = store_memory(
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
        res2 = store_memory(
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
        # Store shared memory for agent1
        store_memory(
            content="# Agent1 Fact\nAgent1 content.",
            tags=["#isolated"],
            scope="shared",
            owner_id="agent1"
        )
        # Store private memory for agent2
        store_memory(
            content="# Agent2 Private Fact\nAgent2 secret content.",
            tags=["#isolated"],
            scope="private",
            owner_id="agent2"
        )
        
        # Search as agent1: sees shared Agent1 Fact, but cannot see agent2's private memory
        results1 = search_memory(query_keywords="Fact", owner_id="agent1")
        titles1 = [r["title"] for r in results1]
        self.assertIn("Agent1 Fact", titles1)
        self.assertNotIn("Agent2 Private Fact", titles1)
        
        # Search as agent2: sees shared Agent1 Fact AND its own private Agent2 Private Fact
        results2 = search_memory(query_keywords="Fact", owner_id="agent2")
        titles2 = [r["title"] for r in results2]
        self.assertIn("Agent1 Fact", titles2)
        self.assertIn("Agent2 Private Fact", titles2)

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
        store_memory(
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
        store_memory(
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
        store_memory(content="# Core Component\nDescription.", tags=["#sys"], scope="shared", owner_id="ops", entity_id="node-core")
        store_memory(content="# Dependency A\nDescription.", tags=["#sys"], scope="shared", owner_id="ops", entity_id="node-dep-a")
        store_memory(content="# Dependency B\nDescription.", tags=["#sys"], scope="shared", owner_id="ops", entity_id="node-dep-b")
        
        # 2. Store relationships (relations)
        res1 = store_relation(source_id="node-core", target_id="node-dep-a", predicate="depends_on")
        res2 = store_relation(source_id="node-dep-a", target_id="node-dep-b", predicate="depends_on")
        self.assertIn("Relation successfully stored", res1)
        self.assertIn("Relation successfully stored", res2)
        
        # 3. Analyze dependencies recursively
        res_dict = analyze_dependencies(root_entity_id="node-core")
        deps = res_dict["dependencies"]
        self.assertEqual(len(deps), 3)
        
        paths = [d["path"] for d in deps]
        self.assertIn("Core Component", paths)
        self.assertIn("Core Component -> Dependency A", paths)
        self.assertIn("Core Component -> Dependency A -> Dependency B", paths)

    def test_store_memory_title_deduplication(self):
        # 1. Insert first memory
        store_memory(
            content="# Same Title\nContent version 1.",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="Deduplication Fact"
        )
        
        # 2. Insert second memory with same title and owner, but without passing entity_id
        store_memory(
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
        store_memory(
            content="# Normalization Fact\nContent.",
            tags=["#Auth-Error"],
            scope="shared",
            owner_id="agent1",
            title="Norm Fact"
        )
        
        # 2. Store another memory with case/hyphen drifted tag name
        store_memory(
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
        mock_socket.return_value.connect.side_effect = Exception("connection refused")
        mock_popen.return_value.poll.return_value = None
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
        store_memory(
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

    def test_metadata_filtering(self):
        # 1. Store with metadata
        store_memory(
            owner_id="agent1",
            content="# Config file\nThis is a configuration file.",
            tags=["#ops"],
            scope="shared",
            entity_id="uuid-meta-1",
            metadata={"project": "SALTMDB", "source_path": "etc/saltmdb.conf"}
        )
        store_memory(
            owner_id="agent1",
            content="# Build file\nThis is a build script.",
            tags=["#build"],
            scope="shared",
            entity_id="uuid-meta-2",
            metadata={"project": "BuildPipeline", "source_path": "bin/build.sh"}
        )
        
        # 2. Query with metadata filters
        res = search_memory(
            owner_id="agent1",
            metadata_filter={"project": "SALTMDB"}
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "uuid-meta-1")
        
        res2 = search_memory(
            owner_id="agent1",
            metadata_filter={"project": "BuildPipeline"}
        )
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]["id"], "uuid-meta-2")

    def test_fts_sanitization_and_fallback(self):
        # 1. Store knowledge
        store_memory(
            owner_id="agent1",
            content="# Auth Error\nDatabase validation failed with code 403.",
            tags=["#auth"],
            scope="shared",
            entity_id="uuid-fts-test"
        )
        
        # 2. Search with mismatched quotes and wildcards (FTS5 syntax crashers)
        query = 'auth error "AND code 403 -'
        res = search_memory(
            query_keywords=query,
            owner_id="agent1"
        )
        # Should not crash and successfully return matching node via sanitization/fallback!
        self.assertTrue(len(res) >= 1)
        self.assertEqual(res[0]["id"], "uuid-fts-test")

    def test_search_explain_mode(self):
        # 1. Query non-existent content in explain mode
        res = search_memory(
            query_keywords="nonexistenttoken",
            tags_filter=["#invalid-tag"],
            owner_id="agent1",
            explain_mode=True
        )
        self.assertIn("explain", res)
        explain = res["explain"]
        self.assertIn("searched_terms_found", explain)
        self.assertEqual(explain["searched_terms_found"]["nonexistenttoken"], False)
        self.assertIn("invalid_tags_suggestions", explain)
        self.assertIn("#invalid-tag", explain["invalid_tags_suggestions"])

    def test_detect_orphaned_memories(self):
        # 1. Add an active memory with NO relations
        store_memory(
            owner_id="agent1",
            content="# Orphan Memory\nThis memory stands alone.",
            tags=["#standalone"],
            scope="shared",
            entity_id="uuid-orphan"
        )
        
        # 2. Run orphan detection
        res = detect_orphaned_memories(owner_id="agent1")
        self.assertTrue(res["orphans_detected"] >= 1)
        orphan_ids = [o["orphan"]["id"] for o in res["details"]]
        self.assertIn("uuid-orphan", orphan_ids)

    def test_check_duplicate_memories(self):
        # 1. Insert base memory
        store_memory(
            owner_id="agent1",
            content="# Database setup rule\nAlways configure SQLite WAL mode.",
            tags=["#database"],
            scope="shared",
            entity_id="uuid-dup-base"
        )
        
        res = check_duplicate_memories(
            title="Database setup rule",
            content="Always configure SQLite Write-Ahead Logging WAL mode.",
            owner_id="agent1",
            tags=["#database"]
        )
        self.assertEqual(res["duplicate_found"], True)
        self.assertTrue(len(res["potential_duplicates"]) >= 1)
        self.assertEqual(res["potential_duplicates"][0]["id"], "uuid-dup-base")

    def test_get_recent_events_truncation(self):
        # 1. Log a massive event (e.g. 1500 characters)
        massive_text = "A" * 1500
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content)
                VALUES ('event-huge-1', ?, 'agent1', 'attempt', ?)
            """, (now, massive_text))
            
        # 2. Log a massive consolidation request
        event_content = json.dumps({"target": "general", "entity_ids": ["e-dyn-1"] * 50, "extra": "B" * 1200})
        with self.conn:
            self.conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content)
                VALUES ('event-huge-2', ?, 'agent1', 'consolidation_request', ?)
            """, (now, event_content))
            
        # 3. Retrieve events
        events = get_recent_events(agent_id="agent1", limit=10)
        
        # Find attempt event
        att_ev = next(e for e in events if e["id"] == "event-huge-1")
        self.assertTrue(len(att_ev["content"]) < 1100)
        self.assertIn("[TRUNCATED", att_ev["content"])
        
        # Find consolidation event (must NOT be truncated!)
        con_ev = next(e for e in events if e["id"] == "event-huge-2")
        self.assertEqual(len(con_ev["content"]), len(event_content))
        self.assertNotIn("[TRUNCATED", con_ev["content"])

    def test_input_validation(self):
        # 1. Test clean title validation
        res_bad_title = store_memory(
            content="# Bad Title\nContent",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="CORE.md — Bad Title"
        )
        self.assertIn("Error: Title violates clean title guidelines", res_bad_title)
        
        # 2. Test metadata absolute path validation
        res_bad_path = store_memory(
            content="# Good Title\nContent",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="Good Title",
            metadata={"source_path": "C:\\Users\\workspace\\CORE.md"}
        )
        self.assertIn("Error: 'source_path' must be a relative repository path", res_bad_path)
        
        # 3. Test valid store passes
        res_good = store_memory(
            content="# Good Title\nContent",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="Good Title",
            metadata={"source_path": "CORE.md"}
        )
        self.assertNotIn("Error", res_good)

    def test_scan_memories(self):
        # Insert 3 memories for agent1
        store_memory(content="Content 1", tags=["#t1"], scope="shared", owner_id="agent1", title="Memory 1", skip_duplicate_check=True)
        store_memory(content="Content 2", tags=["#t2"], scope="shared", owner_id="agent1", title="Memory 2", skip_duplicate_check=True)
        store_memory(content="Content 3", tags=["#t3"], scope="shared", owner_id="agent1", title="Memory 3", skip_duplicate_check=True)
        
        # Scan active memories
        mems = scan_memories(owner_id="agent1", status_filter="active", limit=2, offset=0)
        self.assertEqual(len(mems), 2)
        self.assertEqual(mems[0]["title"], "Memory 3") # Order by updated_at desc
        self.assertEqual(mems[1]["title"], "Memory 2")
        
        # Scan with offset
        mems_offset = scan_memories(owner_id="agent1", status_filter="active", limit=2, offset=2)
        self.assertEqual(len(mems_offset), 1)
        self.assertEqual(mems_offset[0]["title"], "Memory 1")

    def test_dependency_cycle_detection_by_id(self):
        # 1. Create entities with DUPLICATE titles but different IDs
        id1 = store_memory(content="Content 1", tags=["#tag"], scope="shared", owner_id="agent1", title="Duplicate Title").split(": ")[-1]
        id2 = store_memory(content="Content 2", tags=["#tag"], scope="shared", owner_id="agent2", title="Duplicate Title").split(": ")[-1]
        id3 = store_memory(content="Content 3", tags=["#tag"], scope="shared", owner_id="agent1", title="Third Node").split(": ")[-1]
        
        # Build path: id1 -> id2 -> id3
        store_relation(source_id=id1, target_id=id2, predicate="depends_on")
        store_relation(source_id=id2, target_id=id3, predicate="depends_on")
        
        # Traversal from id1 should return all 3 nodes (depth 0, 1, 2)
        tree = analyze_dependencies(id1)["dependencies"]
        self.assertEqual(len(tree), 3)
        self.assertEqual(tree[0]["id"], id1)
        self.assertEqual(tree[1]["id"], id2)
        self.assertEqual(tree[2]["id"], id3)
        
        # 2. Introduce a REAL cycle: id3 -> id1
        store_relation(source_id=id3, target_id=id1, predicate="depends_on")
        
        # Traversal should not crash or run indefinitely, FTS/CTE should halt at cycle
        tree_cycle = analyze_dependencies(id1)["dependencies"]
        # It should still only return 3 unique nodes in the distinct set
        self.assertEqual(len(tree_cycle), 3)

    def test_commit_consolidation_repoints_relations(self):
        # Create raw entities
        p1 = store_memory(content="Parent 1 content", tags=["#raw"], scope="shared", owner_id="agent1", title="Parent 1", skip_duplicate_check=True).split(": ")[-1]
        p2 = store_memory(content="Parent 2 content", tags=["#raw"], scope="shared", owner_id="agent1", title="Parent 2", skip_duplicate_check=True).split(": ")[-1]
        other = store_memory(content="Other content", tags=["#tag"], scope="shared", owner_id="agent1", title="Other Node", skip_duplicate_check=True).split(": ")[-1]
        
        # Relation: p1 depends on other
        store_relation(source_id=p1, target_id=other, predicate="depends_on")
        
        # Perform consolidation on p1 and p2
        res = commit_consolidation(
            parent_ids=[p1, p2],
            title="Consolidated Node",
            content="# Consolidated Node\nSynthesized content.",
            tags=["#consolidated"],
            scope="shared",
            db_connection=self.conn
        )
        self.assertNotIn("Error", res)
        # Extract new entity ID
        new_id = res.split("ID: ")[-1].split(" ")[0].strip()
        
        # Verify p1 and p2 are archived
        cursor = self.conn.execute("SELECT id, status FROM entities WHERE id IN (?, ?)", (p1, p2))
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r[1], 'archived')
        
        # Verify relation has been re-pointed to new_id -> other
        cursor = self.conn.execute("SELECT source_id, target_id FROM relations WHERE target_id = ?", (other,))
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], new_id)
        self.assertEqual(row[1], other)

    def test_search_memory_custom_limit(self):
        # Store 10 distinct entities
        for i in range(10):
            store_memory(
                content=f"Distinct search content {i}",
                tags=["#limit-test"],
                scope="shared",
                owner_id="agent1",
                title=f"Search Limit Title {i}",
                skip_duplicate_check=True
            )
            
        # 1. Test default limit (5)
        res_default = search_memory(owner_id="agent1", tags_filter=["#limit-test"])
        self.assertEqual(len(res_default), 5)
        
        # 2. Test custom limit (3)
        res_custom = search_memory(owner_id="agent1", tags_filter=["#limit-test"], limit=3)
        self.assertEqual(len(res_custom), 3)
        
        # 3. Test capped limit (30 -> 25)
        res_capped = search_memory(owner_id="agent1", tags_filter=["#limit-test"], limit=30)
        self.assertEqual(len(res_capped), 10)

    def test_store_memory_fuzzy_duplicate_guard(self):
        # 1. Store initial baseline memory
        store_memory(
            content="Always configure SQLite Write-Ahead Logging WAL mode for SALTMDB database.",
            tags=["#database"],
            scope="shared",
            owner_id="agent1",
            title="Database setup rule"
        )
        
        # 2. Attempt to store a fuzzy duplicate memory (slight title & content variation)
        res_dup = store_memory(
            content="Ensure you always enable Write-Ahead Logging WAL mode for SALTMDB.",
            tags=["#database"],
            scope="shared",
            owner_id="agent1",
            title="Database setup guidelines"
        )
        
        # Verify it was blocked and returned a duplicate warning message
        self.assertIn("Warning: Potential duplicate of existing memory", res_dup)
        
        # 3. Repeat insertion with skip_duplicate_check=True
        res_forced = store_memory(
            content="Ensure you always enable Write-Ahead Logging WAL mode for SALTMDB.",
            tags=["#database"],
            scope="shared",
            owner_id="agent1",
            title="Database setup guidelines",
            skip_duplicate_check=True
        )
        self.assertIn("Knowledge stored successfully", res_forced)

    def test_schema_migration_columns(self):
        # Verify schema tables have the new columns
        cursor = self.conn.execute("PRAGMA table_info(entities)")
        cols = [r[1] for r in cursor.fetchall()]
        self.assertIn("project_id", cols)
        
        cursor = self.conn.execute("PRAGMA table_info(events)")
        cols = [r[1] for r in cursor.fetchall()]
        self.assertIn("session_id", cols)

    def test_project_id_first_class_recall(self):
        # 1. Store with first-class project_id
        store_memory(
            content="Project specific memory.",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="First-class Project memory",
            project_id="PROJ-A"
        )
        
        # 2. Store with metadata.project fallback
        store_memory(
            content="Metadata fallback memory.",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="Metadata Fallback memory",
            metadata={"project": "PROJ-A"}
        )
        
        # 3. Store another memory for different project
        store_memory(
            content="Other project memory.",
            tags=["#test"],
            scope="shared",
            owner_id="agent1",
            title="Other Project memory",
            project_id="PROJ-B"
        )
        
        # 4. Search and filter by project_id
        results_a = search_memory(owner_id="agent1", project_id="PROJ-A", tags_filter=["#test"])
        # Should return both PROJ-A memories (first-class and metadata fallback)
        self.assertEqual(len(results_a), 2)
        titles = [r["title"] for r in results_a]
        self.assertIn("First-class Project memory", titles)
        self.assertIn("Metadata Fallback memory", titles)
        
        # 5. Search and filter by PROJ-B
        results_b = search_memory(owner_id="agent1", project_id="PROJ-B", tags_filter=["#test"])
        self.assertEqual(len(results_b), 1)
        self.assertEqual(results_b[0]["title"], "Other Project memory")

    def test_session_id_logging_and_summary(self):
        # Log events with a specific session ID
        log_event(agent_id="agent1", type="attempt", content="First attempt", session_id="SESSION-XYZ")
        log_event(agent_id="agent1", type="fix", content="Resolution fix", session_id="SESSION-XYZ")
        log_event(agent_id="agent1", type="decision", content="Other session decision", session_id="SESSION-123")
        
        # Chronological retrieval via get_session_summary
        events = get_session_summary("SESSION-XYZ")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["content"], "First attempt")
        self.assertEqual(events[1]["content"], "Resolution fix")
        
        # Verify in get_recent_events as well
        recent = get_recent_events(limit=5)
        xyz_events = [e for e in recent if e.get("session_id") == "SESSION-XYZ"]
        self.assertEqual(len(xyz_events), 2)

    def test_concurrent_lock_race(self):
        """Spawn N real processes racing to acquire the librarian lock; exactly one must win."""
        import subprocess
        import sys
        
        worker_script = os.path.join(os.path.dirname(__file__), "_lock_race_worker.py")
        db_path = os.path.abspath(self.db_name)
        
        # Spawn 10 concurrent processes
        procs = [
            subprocess.Popen([sys.executable, worker_script, db_path],
                             stdout=subprocess.PIPE, text=True)
            for _ in range(10)
        ]
        
        # Gather outputs
        results = [p.communicate()[0].strip() for p in procs]
        
        # Exactly one must successfully acquire the lock, and the other 9 must fail
        self.assertEqual(results.count("ACQUIRED"), 1)
        self.assertEqual(results.count("FAILED"), 9)

    def test_concurrent_write_race(self):
        """Spawn N real processes each storing a distinct memory; all must persist without lock deadlocks."""
        import subprocess
        import sys
        
        worker_script = os.path.join(os.path.dirname(__file__), "_write_race_worker.py")
        db_path = os.path.abspath(self.db_name)
        
        # Spawn 10 concurrent writes
        procs = [
            subprocess.Popen([sys.executable, worker_script, db_path, str(i), "agent1"],
                             stdout=subprocess.PIPE, text=True)
            for i in range(10)
        ]
        
        # Wait for all to finish and check outcomes
        results = [p.communicate()[0].strip() for p in procs]
        for idx, res in enumerate(results):
            self.assertEqual(res, "SUCCESS", f"Process {idx} failed with output: {res}")
            
        # Assert all 10 records are actually stored in the database
        cursor = self.conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'raw'")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 10)

    def test_fts_porter_stemmer(self):
        # Store a memory with singular/root keywords
        store_memory(
            content="This rule governs sqlite connections and connected database sockets.",
            tags=["#database"],
            scope="shared",
            owner_id="agent1",
            title="Database connections rule"
        )
        
        # Search for pluralization/tense inflected forms
        res_plural = search_memory(owner_id="agent1", query_keywords="connecting")
        self.assertEqual(len(res_plural), 1)
        self.assertEqual(res_plural[0]["title"], "Database connections rule")
        
        res_singular = search_memory(owner_id="agent1", query_keywords="connection")
        self.assertEqual(len(res_singular), 1)

    def test_search_aliases_indexing(self):
        # Store a memory with hidden search aliases in metadata
        store_memory(
            content="We use poetry as our primary packaging python framework.",
            tags=["#packaging"],
            scope="shared",
            owner_id="agent1",
            title="Python Package Manager Config",
            metadata={"search_aliases": ["pip", "dependency_resolution", "pyproject.toml"]}
        )
        
        # Search for an alias word not present in title or content
        res = search_memory(owner_id="agent1", query_keywords="dependency_resolution")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Python Package Manager Config")

    def test_relational_pagerank_boosting(self):
        # Store three identical content memories (with skip_duplicate_check to bypass fuzzy matches)
        id1 = store_memory(content="Common baseline knowledge.", tags=["#rank"], scope="shared", owner_id="agent1", title="Memory One", skip_duplicate_check=True).split(": ")[-1]
        id2 = store_memory(content="Common baseline knowledge.", tags=["#rank"], scope="shared", owner_id="agent1", title="Memory Two", skip_duplicate_check=True).split(": ")[-1]
        id3 = store_memory(content="Common baseline knowledge.", tags=["#rank"], scope="shared", owner_id="agent1", title="Memory Three", skip_duplicate_check=True).split(": ")[-1]
        
        # Create incoming relations for Memory Two (has 2 incoming edges)
        store_relation(source_id=id1, target_id=id2, predicate="links_to")
        store_relation(source_id=id3, target_id=id2, predicate="links_to")
        
        # Create incoming relations for Memory Three (has 1 incoming edge)
        store_relation(source_id=id1, target_id=id3, predicate="links_to")
        
        # Search for the term "Common baseline knowledge"
        res = search_memory(owner_id="agent1", query_keywords="Common baseline knowledge", limit=3)
        self.assertEqual(len(res), 3)
        # Memory Two should be ranked first because it has the most incoming active edges (2)
        self.assertEqual(res[0]["id"], id2)
        # Memory Three should be ranked second (1 incoming edge)
        self.assertEqual(res[1]["id"], id3)
        # Memory One should be ranked third (0 incoming edges)
        self.assertEqual(res[2]["id"], id1)

    def test_consolidation_narrative_preservation(self):
        # 1. Create raw source log memories
        id1 = store_memory(content="Session 1: Tried model A, got timeout.", tags=["#session"], scope="shared", owner_id="agent1", title="Session 1 log", skip_duplicate_check=True).split(": ")[-1]
        id2 = store_memory(content="Session 2: Tried model B, succeeded with 1300W limit.", tags=["#session"], scope="shared", owner_id="agent1", title="Session 2 log", skip_duplicate_check=True).split(": ")[-1]
        
        # 2. Consolidate them
        res = commit_consolidation(
            parent_ids=[id1, id2],
            title="Consolidated Hardware Limits",
            content="# Consolidated Hardware Limits\nUse model B with 1300W limit.",
            tags=["#hardware"],
            scope="shared",
            db_connection=self.conn
        )
        new_id = res.split("ID: ")[-1].split(" ")[0].strip()
        
        # 3. Verify general search does NOT return the archived parents
        res_search = search_memory(owner_id="agent1", query_keywords="Timeout")
        self.assertEqual(len(res_search), 0)
        
        # 4. Verify we can fetch the consolidated memory by ID, read its parent_ids list
        cursor = self.conn.execute("SELECT parent_ids FROM entities WHERE id = ?", (new_id,))
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        parent_ids = json.loads(row[0])
        self.assertIn(id1, parent_ids)
        self.assertIn(id2, parent_ids)
        
        # 5. Fetch archived raw parent contents by ID using fetch_memory_chunk (reconstructing narrative)
        p1_content = fetch_memory_chunk(id1)
        self.assertIn("Tried model A", p1_content)
        
        p2_content = fetch_memory_chunk(id2)
        self.assertIn("Tried model B", p2_content)

    def test_search_memory_is_core(self):
        # 1. Store a core memory and a non-core memory
        core_id = store_memory(
            content="This is a core behavior memory.",
            tags=["#core-test"],
            scope="shared",
            owner_id="agent1",
            title="Core Rule",
            is_core=True,
            skip_duplicate_check=True
        ).split("ID: ")[-1].split(" ")[0].strip()

        non_core_id = store_memory(
            content="This is a non-core behavior memory.",
            tags=["#core-test"],
            scope="shared",
            owner_id="agent1",
            title="Non-Core Fact",
            is_core=False,
            skip_duplicate_check=True
        ).split("ID: ")[-1].split(" ")[0].strip()

        # 2. Search filtering by is_core=True
        results_core = search_memory(owner_id="agent1", tags_filter=["#core-test"], is_core=True)
        self.assertEqual(len(results_core), 1)
        self.assertEqual(results_core[0]["id"], core_id)
        self.assertTrue(results_core[0]["is_core"])

        # 3. Search filtering by is_core=False
        results_non_core = search_memory(owner_id="agent1", tags_filter=["#core-test"], is_core=False)
        self.assertEqual(len(results_non_core), 1)
        self.assertEqual(results_non_core[0]["id"], non_core_id)
        self.assertFalse(results_non_core[0]["is_core"])

        # 4. Search with is_core=None (default) should return both
        results_all = search_memory(owner_id="agent1", tags_filter=["#core-test"])
        self.assertEqual(len(results_all), 2)
        
        # Verify both objects have their correct "is_core" flags
        core_res = next(r for r in results_all if r["id"] == core_id)
        non_core_res = next(r for r in results_all if r["id"] == non_core_id)
        self.assertTrue(core_res["is_core"])
        self.assertFalse(non_core_res["is_core"])

    def test_check_duplicate_memories_rephrasing(self):
        # 1. Store a memory
        store_memory(
            content="We should always configure SQLite write-ahead logging (WAL) mode for local databases.",
            tags=["#database", "#performance"],
            scope="shared",
            owner_id="agent1",
            title="WAL mode configuration for SQLite",
            skip_duplicate_check=True
        )

        # 2. Test duplicate check with shuffled phrasing, suffix variations, and overlapping tags
        res = check_duplicate_memories(
            title="SQLite write-ahead logging configure guidelines",
            content="Always ensure you enable WAL mode for local database setups to improve performance.",
            owner_id="agent1",
            tags=["#database", "#sqlite"]
        )

        self.assertTrue(res["duplicate_found"])
        self.assertGreaterEqual(len(res["potential_duplicates"]), 1)
        self.assertEqual(res["potential_duplicates"][0]["title"], "WAL mode configuration for SQLite")

    def test_search_memory_tag_operator(self):
        # 1. Store memories with unique tags
        store_memory(
            content="Setting up Docker configurations.",
            tags=["#docker"],
            scope="shared",
            owner_id="agent1",
            title="Docker setup",
            skip_duplicate_check=True
        )

        store_memory(
            content="Setting up Kubernetes configurations.",
            tags=["#k8s"],
            scope="shared",
            owner_id="agent1",
            title="Kubernetes setup",
            skip_duplicate_check=True
        )

        store_memory(
            content="Setting up cloud infrastructure.",
            tags=["#docker", "#k8s"],
            scope="shared",
            owner_id="agent1",
            title="Cloud infrastructure",
            skip_duplicate_check=True
        )

        # 2. Query with default (AND) operator: should only return the memory with BOTH tags
        res_and = search_memory(owner_id="agent1", tags_filter=["#docker", "#k8s"])
        self.assertEqual(len(res_and), 1)
        self.assertEqual(res_and[0]["title"], "Cloud infrastructure")

        # 3. Query with explicit OR operator: should return all 3 memories
        res_or = search_memory(owner_id="agent1", tags_filter=["#docker", "#k8s"], tag_operator="OR")
        self.assertEqual(len(res_or), 3)
        titles = [r["title"] for r in res_or]
        self.assertIn("Docker setup", titles)
        self.assertIn("Kubernetes setup", titles)
        self.assertIn("Cloud infrastructure", titles)

    def test_bulk_operations(self):
        # 1. Test bulk_store_relations and bulk_archive_memory
        id1 = store_memory(content="Bulk test node 1", tags=["#bulk"], scope="shared", owner_id="agent1", title="Bulk Title 1", skip_duplicate_check=True).split(": ")[-1]
        id2 = store_memory(content="Bulk test node 2", tags=["#bulk"], scope="shared", owner_id="agent1", title="Bulk Title 2", skip_duplicate_check=True).split(": ")[-1]
        
        # Store relations in bulk
        rel_res = bulk_store_relations([
            {"source_id": id1, "target_id": id2, "predicate": "depends_on"},
            {"source_id": id2, "target_id": id1, "predicate": "links_to"},
            {"source_id": "invalid-id", "target_id": id2, "predicate": "links_to"}  # Should gracefully fail
        ])
        
        self.assertEqual(len(rel_res), 3)
        self.assertEqual(rel_res[0]["status"], "success")
        self.assertEqual(rel_res[1]["status"], "success")
        self.assertEqual(rel_res[2]["status"], "error")
        
        # Verify relation stored in DB
        cursor = self.conn.execute("SELECT predicate FROM relations WHERE source_id = ? AND target_id = ?", (id1, id2))
        self.assertEqual(cursor.fetchone()[0], "depends_on")
        
        # Test bulk_commit_consolidation
        con_res = bulk_commit_consolidation([
            {
                "parent_ids": [id1, id2],
                "title": "Consolidated Bulk Title",
                "content": "# Consolidated Bulk Title\nSummary details.",
                "tags": ["#bulk-summary"]
            },
            {
                "parent_ids": [],  # Should fail due to missing fields / validation
                "title": "",
                "content": "",
                "tags": []
            }
        ])
        
        self.assertEqual(len(con_res), 2)
        self.assertEqual(con_res[0]["status"], "success")
        self.assertEqual(con_res[1]["status"], "error")
        new_con_id = con_res[0]["entity_id"]
        
        # Verify parents are archived and child exists
        cursor = self.conn.execute("SELECT status FROM entities WHERE id = ?", (id1,))
        self.assertEqual(cursor.fetchone()[0], "archived")
        cursor = self.conn.execute("SELECT status FROM entities WHERE id = ?", (new_con_id,))
        self.assertEqual(cursor.fetchone()[0], "consolidated")
        
        # Test bulk_archive_memory
        arch_res = bulk_archive_memory([
            {"entity_id": new_con_id, "owner_id": "system"},  # Consolidated is owned by 'system'
            {"entity_id": "invalid-id", "owner_id": "agent1"}  # Should fail
        ])
        self.assertEqual(len(arch_res), 2)
        self.assertEqual(arch_res[0]["status"], "success")
        self.assertEqual(arch_res[1]["status"], "error")
        
        cursor = self.conn.execute("SELECT status FROM entities WHERE id = ?", (new_con_id,))
        self.assertEqual(cursor.fetchone()[0], "archived")

    def test_cursor_pagination(self):
        # 1. Store 3 memories
        for i in range(3):
            store_memory(
                content=f"Pagination fact content {i}",
                tags=["#page-test"],
                scope="shared",
                owner_id="agent1",
                title=f"Page Title {i}",
                skip_duplicate_check=True
            )

        # 2. Test search_memory pagination
        # Page 1 (limit 2)
        res1 = search_memory(owner_id="agent1", tags_filter=["#page-test"], limit=2)
        self.assertEqual(len(res1), 2)
        self.assertIn("cursor", res1[-1])
        c1 = res1[-1]["cursor"]

        # Page 2 (limit 2, using cursor)
        res2 = search_memory(owner_id="agent1", tags_filter=["#page-test"], limit=2, cursor=c1)
        self.assertEqual(len(res2), 1)
        self.assertNotEqual(res2[0]["id"], res1[0]["id"])
        self.assertNotEqual(res2[0]["id"], res1[1]["id"])

        # 3. Test scan_memories pagination
        # Page 1 (limit 2)
        scan1 = scan_memories(owner_id="agent1", status_filter="active", limit=2)
        self.assertEqual(len(scan1), 2)
        self.assertIn("cursor", scan1[-1])
        sc1 = scan1[-1]["cursor"]

        # Page 2 (limit 2, using cursor)
        scan2 = scan_memories(owner_id="agent1", status_filter="active", limit=2, cursor=sc1)
        self.assertGreaterEqual(len(scan2), 1)
        self.assertNotEqual(scan2[0]["id"], scan1[0]["id"])
        self.assertNotEqual(scan2[0]["id"], scan1[1]["id"])

if __name__ == "__main__":
    unittest.main()
