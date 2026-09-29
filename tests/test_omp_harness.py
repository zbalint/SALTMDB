import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime

from saltmdb.db.schema import init_db
from saltmdb.domain.services import trace_service
from saltmdb.mcp import tools

_OLD_CHECK = "CHECK(harness IN ('codex','claude_code'))"
_NEW_CHECK = "CHECK(harness IN ('codex','claude_code','omp'))"


def _table_sql(conn) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_traces'"
    ).fetchone()[0]


def _make_legacy(conn) -> None:
    """Put conversation_traces back to its pre-omp CHECK, as an existing database has it."""
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql=replace(sql, ?, ?) "
        "WHERE type='table' AND name='conversation_traces'",
        (_NEW_CHECK, _OLD_CHECK),
    )
    conn.execute(f"PRAGMA schema_version={version + 1}")
    conn.execute("PRAGMA writable_schema=OFF")


class TestOmpHarness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "omp-harness.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _start(self, conn, turn_id="turn-1"):
        return trace_service.capture_trace_start(
            agent_session_id="adapter-session",
            agent_id="omp",
            harness="omp",
            harness_session_id="omp-session",
            harness_turn_id=turn_id,
            user_prompt="hello",
            db_connection=conn,
        )

    def test_fresh_database_accepts_omp_traces(self):
        conn = init_db(self.db_path)
        self.assertIn(_NEW_CHECK, _table_sql(conn))
        result = self._start(conn)
        self.assertEqual(result["status"], "ok")
        stored = conn.execute("SELECT harness FROM conversation_traces").fetchone()[0]
        self.assertEqual(stored, "omp")
        conn.close()

    def test_legacy_database_is_migrated_and_child_rows_survive(self):
        conn = init_db(self.db_path)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, agent_id,
                   scope, title, full_content, content_hash)
               VALUES ('e1', ?, ?, ?, 'claude', 'shared', 't', 'c', 'h')""",
            (now, now, now),
        )
        conn.execute(
            """INSERT INTO conversation_traces (id, agent_session_id, agent_id, harness,
                   harness_session_id, harness_turn_id, status, user_prompt, user_prompt_hash,
                   created_at, updated_at)
               VALUES ('t1', 's', 'claude', 'claude_code', 'hs', 'turn-0', 'pending', 'p', 'h',
                   ?, ?)""",
            (now, now),
        )
        conn.execute(
            "INSERT INTO trace_memory_links (id, trace_id, entity_id, content_hash, "
            "write_operation, created_at) VALUES ('l1', 't1', 'e1', 'h', 'store_memory_new', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO trace_turn_messages (id, trace_id, seq, message, message_hash, "
            "created_at) VALUES ('m1', 't1', 1, 'msg', 'mh', ?)",
            (now,),
        )
        _make_legacy(conn)
        conn.close()

        legacy = sqlite3.connect(self.db_path)
        self.assertIn(_OLD_CHECK, _table_sql(legacy))
        legacy.close()

        conn = init_db(self.db_path)
        self.assertIn(_NEW_CHECK, _table_sql(conn))
        self.assertEqual(self._start(conn)["status"], "ok")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trace_memory_links").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trace_turn_messages").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_traces").fetchone()[0], 2)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()

    def test_migration_is_idempotent(self):
        conn = init_db(self.db_path)
        _make_legacy(conn)
        conn.close()
        conn = init_db(self.db_path)
        migrated_sql = _table_sql(conn)
        conn.close()
        conn = init_db(self.db_path)
        self.assertEqual(_table_sql(conn), migrated_sql)
        self.assertEqual(migrated_sql.count("'omp'"), 1)
        conn.close()

    def test_unrecognised_table_definition_is_refused_not_guessed(self):
        conn = init_db(self.db_path)
        _make_legacy(conn)
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=replace(sql, ?, 'CHECK(harness IN (''codex''))') "
            "WHERE type='table' AND name='conversation_traces'",
            (_OLD_CHECK,),
        )
        conn.execute("PRAGMA writable_schema=OFF")
        conn.close()
        with self.assertRaises(RuntimeError):
            init_db(self.db_path)

    def test_other_harness_values_are_still_rejected(self):
        conn = init_db(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO conversation_traces (id, agent_session_id, agent_id, harness,
                       harness_session_id, harness_turn_id, status, user_prompt,
                       user_prompt_hash, created_at, updated_at)
                   VALUES ('x', 's', 'a', 'bogus', 'hs', 'turn', 'pending', 'p', 'h', 'n', 'n')"""
            )
        conn.close()

    def test_capture_tool_schema_lists_omp(self):
        registered = tools.mcp._tool_manager._tools["capture_trace_start"]
        self.assertEqual(
            set(registered.parameters["properties"]["harness"]["enum"]),
            {"codex", "claude_code", "omp"},
        )


if __name__ == "__main__":
    unittest.main()
