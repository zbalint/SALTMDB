import os
import shutil
import sqlite3
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.domain.services import trace_service

_OMP_CHECK = "CHECK(harness IN ('codex','claude_code','omp'))"
_CURRENT_CHECK = "CHECK(harness IN ('codex','claude_code','omp','copilot'))"


def _table_sql(conn) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_traces'"
    ).fetchone()[0]


def _make_omp_era(conn) -> None:
    """Put conversation_traces back to its pre-copilot (omp-era) CHECK."""
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql=replace(sql, ?, ?) "
        "WHERE type='table' AND name='conversation_traces'",
        (_CURRENT_CHECK, _OMP_CHECK),
    )
    conn.execute(f"PRAGMA schema_version={version + 1}")
    conn.execute("PRAGMA writable_schema=OFF")


class TestCopilotHarness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "copilot-harness.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _start(self, conn):
        return trace_service.capture_trace_start(
            agent_session_id="adapter-session",
            agent_id="copilot",
            harness="copilot",
            harness_session_id="copilot-session",
            harness_turn_id="interaction-1",
            user_prompt="hello",
            db_connection=conn,
        )

    def test_fresh_database_accepts_copilot_traces(self):
        conn = init_db(self.db_path)
        self.assertIn(_CURRENT_CHECK, _table_sql(conn))
        self.assertEqual(self._start(conn)["status"], "ok")
        self.assertEqual(
            conn.execute("SELECT harness FROM conversation_traces").fetchone()[0], "copilot"
        )
        conn.close()

    def test_omp_era_database_is_migrated(self):
        conn = init_db(self.db_path)
        _make_omp_era(conn)
        conn.close()
        conn = init_db(self.db_path)
        self.assertIn(_CURRENT_CHECK, _table_sql(conn))
        self.assertEqual(self._start(conn)["status"], "ok")
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()

    def test_migration_is_idempotent(self):
        conn = init_db(self.db_path)
        _make_omp_era(conn)
        conn.close()
        conn = init_db(self.db_path)
        migrated_sql = _table_sql(conn)
        conn.close()
        conn = init_db(self.db_path)
        self.assertEqual(_table_sql(conn), migrated_sql)
        self.assertEqual(migrated_sql.count("'copilot'"), 1)
        conn.close()

    def test_bogus_harness_still_rejected(self):
        conn = init_db(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO conversation_traces (id, agent_session_id, agent_id, harness,
                       harness_session_id, harness_turn_id, status, user_prompt,
                       user_prompt_hash, created_at, updated_at)
                   VALUES ('x', 's', 'a', 'bogus', 'hs', 'turn', 'pending', 'p', 'h', 'n', 'n')"""
            )
        conn.close()


if __name__ == "__main__":
    unittest.main()
