import unittest
import os
import sys
import sqlite3
import json
import time
import socket
import socketserver
import urllib.request
import urllib.parse
import subprocess
from datetime import datetime, UTC

# Add workspace root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import saltmdb_server
import saltmdb_viewer

TEST_E2E_DB = "test_saltmdb_e2e.db"
TEST_VIEWER_PORT = 8089

class TestSALTMDBE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_db_path = os.environ.get("SALTMDB_DB_PATH")
        cls.db_path = os.path.abspath(TEST_E2E_DB)
        os.environ["SALTMDB_DB_PATH"] = cls.db_path
        cls.cleanup_db_files()

    @classmethod
    def tearDownClass(cls):
        cls.cleanup_db_files()
        if cls.old_db_path:
            os.environ["SALTMDB_DB_PATH"] = cls.old_db_path
        else:
            os.environ.pop("SALTMDB_DB_PATH", None)

    @classmethod
    def cleanup_db_files(cls):
        for path in [cls.db_path, cls.db_path + "-wal", cls.db_path + "-shm"]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    def setUp(self):
        self.cleanup_db_files()
        self.conn = saltmdb_server.init_db(self.db_path)

    def tearDown(self):
        if hasattr(self, "conn") and self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.cleanup_db_files()

    # =========================================================================
    # 1. MCP Server Stdio E2E Test
    # =========================================================================
    def test_mcp_server_stdio_interface(self):
        """Test launching saltmdb_server.py as an MCP server process communicating over stdio."""
        env = os.environ.copy()
        env["SALTMDB_DB_PATH"] = self.db_path
        
        proc = subprocess.Popen(
            [sys.executable, "saltmdb_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        )
        
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"}
            }
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()
        
        resp_line = proc.stdout.readline()
        self.assertTrue(resp_line, "MCP server did not return response to initialize")
        resp = json.loads(resp_line)
        self.assertEqual(resp.get("id"), 1)
        self.assertIn("result", resp)
        
        init_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write(json.dumps(init_notif) + "\n")
        proc.stdin.flush()

        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        proc.stdin.write(json.dumps(tools_req) + "\n")
        proc.stdin.flush()
        
        resp_line = proc.stdout.readline()
        self.assertTrue(resp_line, "MCP server did not return response to tools/list")
        resp = json.loads(resp_line)
        self.assertEqual(resp.get("id"), 2)
        tools = resp.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        self.assertIn("store_memory", tool_names)
        self.assertIn("search_memory", tool_names)
        self.assertIn("log_event", tool_names)

        proc.terminate()
        proc.wait(timeout=3)

    # =========================================================================
    # 2. Librarian Process E2E Test
    # =========================================================================
    def test_librarian_subprocess_e2e(self):
        """Test spawning detached librarian worker process and verifying execution."""
        for i in range(5):
            saltmdb_server.store_memory(
                content=f"# Raw Memory {i}\nDetails about item {i}",
                tags=["#cluttered_test"],
                owner_id="agent_lib_test",
                skip_duplicate_check=True
            )
            
        env = os.environ.copy()
        env["SALTMDB_DB_PATH"] = self.db_path
        
        proc = subprocess.Popen(
            [sys.executable, "saltmdb_server.py", "--librarian"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        )
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, f"Librarian failed with stderr: {stderr}")
        self.assertIn("Librarian consolidation complete.", stdout)
        
        events = saltmdb_server.get_recent_events(agent_id="agent_lib_test", type_filter="consolidation_request")
        self.assertGreaterEqual(len(events), 1, "Librarian should log consolidation request event")

    # =========================================================================
    # 3. HTTP Database Viewer E2E Test
    # =========================================================================
    def test_http_db_viewer_e2e(self):
        """Test HTTP Viewer server API endpoints, static assets, and HTTP error handling."""
        saltmdb_server.log_event(agent_id="viewer_test_agent", type="decision", content="Testing viewer HTTP server")
        mem_res = saltmdb_server.store_memory(
            content="# Viewer Test Memory\nContent for HTTP API test",
            tags=["#viewer"],
            owner_id="viewer_test_agent",
            skip_duplicate_check=True
        )
        entity_id = mem_res.split("ID: ")[1]

        old_viewer_db = saltmdb_viewer.DB_PATH
        saltmdb_viewer.DB_PATH = self.db_path
        
        server = socketserver.TCPServer(("127.0.0.1", TEST_VIEWER_PORT), saltmdb_viewer.SALTMDBHandler)
        import threading
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        base_url = f"http://127.0.0.1:{TEST_VIEWER_PORT}"
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                html = res.read().decode("utf-8")
                self.assertIn("SALTMDB Database Viewer", html)

            with urllib.request.urlopen(f"{base_url}/api/entities", timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                data = json.loads(res.read().decode("utf-8"))
                self.assertIn("entities", data)

            with urllib.request.urlopen(f"{base_url}/api/events", timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                data = json.loads(res.read().decode("utf-8"))
                self.assertIn("events", data)

            with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2.0) as res:
                self.assertEqual(res.status, 200)

            with urllib.request.urlopen(f"{base_url}/api/locks", timeout=2.0) as res:
                self.assertEqual(res.status, 200)

            with urllib.request.urlopen(f"{base_url}/api/entity/{entity_id}", timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                data = json.loads(res.read().decode("utf-8"))
                self.assertEqual(data["id"], entity_id)

            req = urllib.request.Request(f"{base_url}/api/non_existent_endpoint")
            try:
                urllib.request.urlopen(req, timeout=2.0)
                self.fail("Should return 404 for unknown endpoint")
            except urllib.error.HTTPError as err:
                self.assertEqual(err.code, 404)

        finally:
            server.shutdown()
            server.server_close()
            saltmdb_viewer.DB_PATH = old_viewer_db

    # =========================================================================
    # 4. Detailed Bug & Edge Case Tests
    # =========================================================================

    def test_bug_title_secret_redaction(self):
        """Bug Test: store_memory must redact credentials from title."""
        secret_title = "Secret key ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 config"
        res = saltmdb_server.store_memory(
            content="# Content\nBody text",
            tags=["#security"],
            owner_id="edge_agent",
            title=secret_title,
            skip_duplicate_check=True
        )
        mem_id = res.split("ID: ")[1]
        
        conn = saltmdb_server.init_db(self.db_path)
        cursor = conn.execute("SELECT title FROM entities WHERE id = ?", (mem_id,))
        stored_title = cursor.fetchone()[0]
        conn.close()
        
        self.assertNotIn("ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", stored_title)
        self.assertIn("[REDACTED_SECRET]", stored_title)

    def test_bug_tag_search_canonicalization_and_normalization(self):
        """Bug Test: search_memory tag filter should resolve alias tags and normalize tag inputs."""
        res = saltmdb_server.store_memory(
            content="# Auth Failure\nInvalid token specified",
            tags=["#Auth_Error"],
            owner_id="edge_agent",
            skip_duplicate_check=True
        )
        mem_id = res.split("ID: ")[1]

        # Simulate tag canonicalization (e.g. #Auth_Error -> #auth-error)
        conn = saltmdb_server.init_db(self.db_path)
        with conn:
            conn.execute("INSERT OR IGNORE INTO tags (id, name, canonical_id) VALUES ('c1', '#auth-error', NULL)")
            conn.execute("UPDATE tags SET canonical_id = 'c1' WHERE name = '#Auth_Error'")
            conn.execute("UPDATE entity_tags SET tag_id = 'c1' WHERE entity_id = ?", (mem_id,))
        conn.close()

        # 1. Search with alias tag name
        results = saltmdb_server.search_memory(owner_id="edge_agent", tags_filter=["#Auth_Error"])
        self.assertIsInstance(results, list)
        self.assertGreaterEqual(len(results), 1, "Searching by alias tag should return the memory linked to canonical tag")

        # 2. Search without '#' prefix and uppercase
        results_norm = saltmdb_server.search_memory(owner_id="edge_agent", tags_filter=["AUTH-ERROR"])
        self.assertIsInstance(results_norm, list)
        self.assertGreaterEqual(len(results_norm), 1, "Searching by normalized tag without # should return the memory")

    def test_bug_check_duplicate_memories_none_title_or_content(self):
        """Bug Test: check_duplicate_memories should handle None title or content gracefully without crashing."""
        res1 = saltmdb_server.check_duplicate_memories(title=None, content="Test content", owner_id="edge_agent")
        self.assertIsInstance(res1, dict)
        self.assertIn("duplicate_found", res1)

        res2 = saltmdb_server.check_duplicate_memories(title="Test Title", content=None, owner_id="edge_agent")
        self.assertIsInstance(res2, dict)
        self.assertIn("duplicate_found", res2)

    def test_bug_viewer_cors_options_preflight(self):
        """Bug Test: SALTMDBHandler must respond to HTTP OPTIONS CORS preflight requests."""
        old_viewer_db = saltmdb_viewer.DB_PATH
        saltmdb_viewer.DB_PATH = self.db_path
        
        server = socketserver.TCPServer(("127.0.0.1", TEST_VIEWER_PORT), saltmdb_viewer.SALTMDBHandler)
        import threading
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        base_url = f"http://127.0.0.1:{TEST_VIEWER_PORT}"
        try:
            req = urllib.request.Request(f"{base_url}/api/entities", method="OPTIONS")
            with urllib.request.urlopen(req, timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "*")
                self.assertIn("OPTIONS", res.headers.get("Access-Control-Allow-Methods", ""))
        finally:
            server.shutdown()
            server.server_close()
            saltmdb_viewer.DB_PATH = old_viewer_db

    def test_bug_viewer_negative_and_invalid_pagination(self):
        """Bug Test: Viewer API page parameters should be validated to avoid negative offsets."""
        old_viewer_db = saltmdb_viewer.DB_PATH
        saltmdb_viewer.DB_PATH = self.db_path
        
        server = socketserver.TCPServer(("127.0.0.1", TEST_VIEWER_PORT), saltmdb_viewer.SALTMDBHandler)
        import threading
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        base_url = f"http://127.0.0.1:{TEST_VIEWER_PORT}"
        try:
            with urllib.request.urlopen(f"{base_url}/api/entities?page=-5", timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                data = json.loads(res.read().decode("utf-8"))
                self.assertEqual(data["pagination"]["page"], 1)

            with urllib.request.urlopen(f"{base_url}/api/entities?page=invalid", timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                data = json.loads(res.read().decode("utf-8"))
                self.assertEqual(data["pagination"]["page"], 1)
        finally:
            server.shutdown()
            server.server_close()
            saltmdb_viewer.DB_PATH = old_viewer_db

    def test_bug_store_relation_self_loop(self):
        """Bug Test: store_relation and bulk_store_relations should reject self-referential relations (source_id == target_id)."""
        res_mem = saltmdb_server.store_memory(content="# Self Loop Test", tags=["#loop"], owner_id="loop_agent", skip_duplicate_check=True)
        mem_id = res_mem.split("ID: ")[1]

        res = saltmdb_server.store_relation(source_id=mem_id, target_id=mem_id, predicate="depends_on")
        self.assertTrue(res.startswith("Error:"), "Self-referential relations must be rejected")

        bulk_res = saltmdb_server.bulk_store_relations([{"source_id": mem_id, "target_id": mem_id, "predicate": "depends_on"}])
        self.assertEqual(bulk_res[0]["status"], "error")

    def test_bug_viewer_entity_detail_missing_target_relation(self):
        """Bug Test: get_entity_detail should use LEFT JOIN so relations pointing to missing/deleted entities are preserved."""
        mem1 = saltmdb_server.store_memory(content="# Source Entity", tags=["#rel"], owner_id="rel_agent", skip_duplicate_check=True).split("ID: ")[1]
        mem2 = saltmdb_server.store_memory(content="# Target Entity", tags=["#rel"], owner_id="rel_agent", skip_duplicate_check=True).split("ID: ")[1]
        saltmdb_server.store_relation(source_id=mem1, target_id=mem2, predicate="depends_on")

        # Delete target entity directly with FK disabled to simulate missing target in relations
        conn = saltmdb_server.init_db(self.db_path)
        with conn:
            conn.execute("PRAGMA foreign_keys=OFF;")
            conn.execute("DELETE FROM entities WHERE id = ?", (mem2,))
        conn.close()

        old_viewer_db = saltmdb_viewer.DB_PATH
        saltmdb_viewer.DB_PATH = self.db_path
        
        server = socketserver.TCPServer(("127.0.0.1", TEST_VIEWER_PORT), saltmdb_viewer.SALTMDBHandler)
        import threading
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        
        base_url = f"http://127.0.0.1:{TEST_VIEWER_PORT}"
        try:
            with urllib.request.urlopen(f"{base_url}/api/entity/{mem1}", timeout=2.0) as res:
                self.assertEqual(res.status, 200)
                data = json.loads(res.read().decode("utf-8"))
                outgoing = data.get("relations", {}).get("outgoing", [])
                self.assertEqual(len(outgoing), 1, "Outgoing relation to missing target entity must be preserved via LEFT JOIN")
        finally:
            server.shutdown()
            server.server_close()
            saltmdb_viewer.DB_PATH = old_viewer_db

    def test_bug_bulk_commit_consolidation_relation_repoint(self):
        """Bug Test: bulk_commit_consolidation must re-point existing relations from parent IDs to the new consolidated entity."""
        parent_id = saltmdb_server.store_memory(content="# Parent Memory", tags=["#p"], owner_id="bulk_agent", skip_duplicate_check=True).split("ID: ")[1]
        other_id = saltmdb_server.store_memory(content="# Other Memory", tags=["#o"], owner_id="bulk_agent", skip_duplicate_check=True).split("ID: ")[1]
        saltmdb_server.store_relation(source_id=other_id, target_id=parent_id, predicate="depends_on")

        bulk_res = saltmdb_server.bulk_commit_consolidation([{
            "parent_ids": [parent_id],
            "title": "Consolidated Summary Title",
            "content": "# Summary\nConsolidated text",
            "tags": ["#p"]
        }])
        self.assertEqual(bulk_res[0]["status"], "success")
        consolidated_id = bulk_res[0]["entity_id"]

        # Verify existing relation target_id was re-pointed to consolidated_id
        conn = saltmdb_server.init_db(self.db_path)
        cursor = conn.execute("SELECT target_id FROM relations WHERE source_id = ? AND predicate = 'depends_on'", (other_id,))
        row = cursor.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], consolidated_id, "Existing relation target_id must be re-pointed to consolidated memory ID")

    # =========================================================================
    # 5. Usability & Parameter Aliasing Tests (Post-Blind-Test Refactoring)
    # =========================================================================

    def test_usability_init_db_default_arg(self):
        """Usability Test: init_db() should work without mandatory positional args."""
        conn = saltmdb_server.init_db()
        self.assertIsNotNone(conn)
        conn.close()

    def test_usability_smart_entity_id_resolution(self):
        """Usability Test: Tools expecting entity IDs must automatically resolve status strings and entity titles."""
        # Store memories
        m1_res = saltmdb_server.store_memory(content="# Component Alpha\nCore alpha service", tags=["#comp"], owner_id="smart_agent", skip_duplicate_check=True)
        m2_res = saltmdb_server.store_memory(content="# Component Beta\nCore beta service", tags=["#comp"], owner_id="smart_agent", skip_duplicate_check=True)
        
        # 1. Pass status string directly into store_relation
        rel_res = saltmdb_server.store_relation(source_id=m1_res, target_id="Component Beta", predicate="depends_on")
        self.assertTrue(rel_res.startswith("Relation successfully stored"), f"Smart resolution failed: {rel_res}")

        # 2. Fetch memory chunk passing title
        chunk = saltmdb_server.fetch_memory_chunk(entity_id="Component Alpha")
        self.assertIn("Core alpha service", chunk)

        # 3. Analyze dependencies passing component title
        deps = saltmdb_server.analyze_dependencies(root_entity_id="Component Alpha")
        self.assertIsInstance(deps, dict)
        self.assertTrue(deps.get("graph_exhausted"))
        self.assertGreaterEqual(len(deps.get("dependencies", [])), 2)

    def test_usability_parameter_aliasing(self):
        """Usability Test: Tools should accept parameter synonyms (query, event_type, text, tag, owner, etc.)."""
        # 1. log_event with event_type and message
        log_res = saltmdb_server.log_event(agent="alias_agent", event_type="fix", message="Applied patch")
        self.assertTrue(log_res.startswith("Event logged successfully"))

        # 2. store_memory with text, tag, owner
        mem_res = saltmdb_server.store_memory(text="# Alias Test\nText body", tag="alias_tag", owner="alias_agent", skip_duplicate_check=True)
        self.assertTrue(mem_res.startswith("Knowledge stored successfully"))

        # 3. search_memory with owner, query, tag
        search_res = saltmdb_server.search_memory(owner="alias_agent", query="patch", tag="alias_tag")
        self.assertIsInstance(search_res, list)

    def test_usability_bulk_archive_string_list(self):
        """Usability Test: bulk_archive_memory should accept simple string lists of UUIDs/titles."""
        m_id = saltmdb_server.store_memory(content="# To Archive\nTemporary memory", tags=["#temp"], owner_id="bulk_arch_agent", skip_duplicate_check=True).split("ID: ")[1]
        
        results = saltmdb_server.bulk_archive_memory(archive_requests=[m_id])
        self.assertEqual(results[0]["status"], "success")

    # =========================================================================
    # 6. REVIEW_1.md Architectural & Concurrency Tests
    # =========================================================================

    def test_review1_query_normalization(self):
        """REVIEW_1 Test: Natural language search queries should be normalized by stripping stop words."""
        saltmdb_server.store_memory(content="# SQLite WAL Logging\nHow to configure Write Ahead Logging mode in SQLite DB", tags=["#sqlite"], owner_id="norm_agent", skip_duplicate_check=True)
        
        # Query with natural language filler words
        results = saltmdb_server.search_memory(owner_id="norm_agent", query="how can I configure WAL logging in sqlite")
        self.assertIsInstance(results, list)
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("SQLite WAL Logging", results[0]["title"])

    def test_review1_lossless_consolidation_lineage(self):
        """REVIEW_1 Test: Consolidation must archive (never delete) parent entities and auto-create lineage edges."""
        p1 = saltmdb_server.store_memory(content="# Source Alpha\nFact alpha content", tags=["#src"], owner_id="lineage_agent", skip_duplicate_check=True).split("ID: ")[1]
        p2 = saltmdb_server.store_memory(content="# Source Beta\nFact beta content", tags=["#src"], owner_id="lineage_agent", skip_duplicate_check=True).split("ID: ")[1]
        
        res = saltmdb_server.commit_consolidation(parent_ids=[p1, p2], title="Synthesized Summary", content="# Summary\nCombined alpha & beta", tags=["#summary"])
        self.assertTrue(res.startswith("Successfully committed consolidated memory"))
        c_id = res.split("ID: ")[1].split(" ")[0]

        # Verify parent entities are archived (not deleted)
        conn = saltmdb_server.init_db(self.db_path)
        cursor = conn.execute("SELECT status FROM entities WHERE id IN (?, ?)", (p1, p2))
        statuses = [r[0] for r in cursor.fetchall()]
        conn.close()
        self.assertEqual(statuses, ["archived", "archived"])

        # Verify lineage ancestry traversal tool
        lineage = saltmdb_server.analyze_lineage(entity_id=c_id)
        self.assertIsInstance(lineage, dict)
        self.assertEqual(lineage["entity_id"], c_id)
        self.assertGreaterEqual(len(lineage["ancestors"]), 2)

    def test_concurrency_multiprocess_lock_racing(self):
        """Concurrency Test: Multiple OS processes racing to acquire the librarian lock must guarantee mutual exclusion."""
        import subprocess
        import sys
        
        # Script to attempt lock acquisition
        script_code = f"""
import sys, os
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import saltmdb_server
conn = saltmdb_server.init_db(r"{self.db_path}")
acquired = saltmdb_server.acquire_librarian_lock(conn)
if acquired:
    print("ACQUIRED")
    import time
    time.sleep(0.5)
    saltmdb_server.release_librarian_lock(conn)
else:
    print("FAILED")
conn.close()
"""
        # Run 4 parallel processes simultaneously
        processes = [
            subprocess.Popen([sys.executable, "-c", script_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(4)
        ]
        
        results = [p.communicate()[0].strip() for p in processes]
        acquired_count = sum(1 for r in results if "ACQUIRED" in r)
        self.assertEqual(acquired_count, 1, f"Expected exactly 1 process to acquire lock, got outputs: {results}")

    def test_concurrency_multithreaded_write_racing(self):
        """Concurrency Test: High-concurrency multithreaded writes under WAL mode must execute cleanly without locks."""
        import threading
        
        errors = []
        def worker(thread_idx):
            try:
                for i in range(5):
                    saltmdb_server.log_event(agent_id=f"thread_{thread_idx}", type="test", content=f"Event {i} from thread {thread_idx}")
                    saltmdb_server.store_memory(content=f"# Thread {thread_idx} Memory {i}\nContent", tags=["#thread"], owner_id=f"thread_{thread_idx}", skip_duplicate_check=True)
            except Exception as ex:
                errors.append(ex)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent multithreaded write errors: {errors}")

if __name__ == "__main__":
    unittest.main()
