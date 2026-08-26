"""Tests for saltmdb.db.agent_sessions: session tracking for last-session bootstrap digest."""

import os
import sqlite3
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.db.agent_sessions import (
    record_session,
    get_last_session_for_cwd,
    get_recent_sessions_for_cwd,
    touch_session,
    close_session,
    reconcile_orphaned_sessions,
)


class TestAgentSessions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_session_idempotent(self):
        """Calling record_session twice with the same session_id should not raise or duplicate."""
        session_id = "test-session-123"
        cwd = "/home/user/project"
        started_at = "2024-01-01T12:00:00+00:00"

        record_session(self.conn, session_id, cwd, started_at)
        record_session(self.conn, session_id, cwd, started_at)

        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM _agent_sessions WHERE session_id = ?", (session_id,)
        )
        count = cursor.fetchone()[0]
        self.assertEqual(count, 1, "record_session should be idempotent")

    def test_lifecycle_metadata_is_monotonic_and_close_is_idempotent(self):
        record_session(self.conn, "session-life", "/project", "2024-01-01T10:00:00+00:00", "codex")
        touch_session(self.conn, "session-life", "2024-01-01T11:00:00+00:00")
        touch_session(self.conn, "session-life", "2024-01-01T10:30:00+00:00")
        close_session(self.conn, "session-life", "2024-01-01T12:00:00+00:00")
        close_session(self.conn, "session-life", "2024-01-01T13:00:00+00:00")
        row = self.conn.execute(
            "SELECT owner_id, last_activity_at, ended_at FROM _agent_sessions WHERE session_id = ?",
            ("session-life",),
        ).fetchone()
        self.assertEqual(row, ("codex", "2024-01-01T11:00:00+00:00", "2024-01-01T12:00:00+00:00"))

    def test_rehello_same_id_preserves_identity_and_history(self):
        """Daemon reconnects may re-register a live adapter without duplicating its row."""
        record_session(
            self.conn,
            "session-reconnect",
            "/project",
            "2024-01-01T10:00:00+00:00",
            "codex",
        )
        close_session(self.conn, "session-reconnect", "2024-01-01T11:00:00+00:00")
        record_session(
            self.conn,
            "session-reconnect",
            "/project",
            "2024-01-01T12:00:00+00:00",
            "antigravity",
        )
        row = self.conn.execute(
            "SELECT COUNT(*), owner_id, started_at, last_activity_at, ended_at "
            "FROM _agent_sessions WHERE session_id = ?",
            ("session-reconnect",),
        ).fetchone()
        self.assertEqual(row, (1, "codex", "2024-01-01T10:00:00+00:00", "2024-01-01T12:00:00+00:00", None))

    def test_rehello_does_not_move_activity_backwards(self):
        record_session(self.conn, "session-monotonic", "/project", "2024-01-01T12:00:00+00:00", "codex")
        record_session(self.conn, "session-monotonic", "/other", "2024-01-01T11:00:00+00:00", "other")
        row = self.conn.execute(
            "SELECT cwd, owner_id, started_at, last_activity_at, ended_at "
            "FROM _agent_sessions WHERE session_id = ?",
            ("session-monotonic",),
        ).fetchone()
        self.assertEqual(
            row,
            ("/project", "codex", "2024-01-01T12:00:00+00:00", "2024-01-01T12:00:00+00:00", None),
        )

    def test_schema_migration_preserves_legacy_rows_and_leaves_new_fields_null(self):
        """The additive lifecycle migration must not invent unavailable historical metadata."""
        legacy_path = os.path.join(self.temp_dir, "legacy.db")
        legacy = sqlite3.connect(legacy_path)
        legacy.execute(
            "CREATE TABLE _agent_sessions "
            "(session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, started_at DATETIME NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO _agent_sessions VALUES (?, ?, ?)",
            ("legacy-session", "/legacy", "2024-01-01T10:00:00+00:00"),
        )
        legacy.commit()
        legacy.close()

        migrated = init_db(legacy_path)
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(_agent_sessions)").fetchall()
        }
        self.assertTrue({"owner_id", "last_activity_at", "ended_at"}.issubset(columns))
        cwd_column = next(
            row for row in migrated.execute("PRAGMA table_info(_agent_sessions)").fetchall()
            if row[1] == "cwd"
        )
        self.assertEqual(cwd_column[3], 0, "cwd must be nullable for incomplete registrations")
        row = migrated.execute(
            "SELECT cwd, started_at, owner_id, last_activity_at, ended_at "
            "FROM _agent_sessions WHERE session_id = ?",
            ("legacy-session",),
        ).fetchone()
        self.assertEqual(
            row,
            ("/legacy", "2024-01-01T10:00:00+00:00", None, None, None),
        )
        migrated.close()

    def test_schema_migration_adds_missing_cwd_and_is_idempotent(self):
        legacy_path = os.path.join(self.temp_dir, "legacy-no-cwd.db")
        legacy = sqlite3.connect(legacy_path)
        legacy.execute(
            "CREATE TABLE _agent_sessions "
            "(session_id TEXT PRIMARY KEY, started_at DATETIME NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO _agent_sessions VALUES (?, ?)",
            ("legacy-no-cwd", "2024-01-01T10:00:00+00:00"),
        )
        legacy.commit()
        legacy.close()

        migrated = init_db(legacy_path)
        record_session(migrated, "legacy-no-cwd", "/enriched", "2024-01-01T11:00:00+00:00", "codex")
        migrated.commit()
        migrated.close()

        reopened = init_db(legacy_path)
        columns = reopened.execute("PRAGMA table_info(_agent_sessions)").fetchall()
        cwd_column = next(row for row in columns if row[1] == "cwd")
        self.assertEqual(cwd_column[3], 0)
        row = reopened.execute(
            "SELECT cwd, started_at, owner_id, last_activity_at, ended_at "
            "FROM _agent_sessions WHERE session_id = ?",
            ("legacy-no-cwd",),
        ).fetchone()
        self.assertEqual(
            row,
            ("/enriched", "2024-01-01T10:00:00+00:00", "codex", "2024-01-01T11:00:00+00:00", None),
        )
        reopened.close()

    def test_reconcile_orphaned_sessions_closes_open_rows_using_last_activity(self):
        """A row left with ended_at NULL by an unclean death is backdated to last_activity_at."""
        record_session(self.conn, "orphan-1", "/project", "2024-01-01T10:00:00+00:00", "codex")
        touch_session(self.conn, "orphan-1", "2024-01-01T10:30:00+00:00")

        closed = reconcile_orphaned_sessions(self.conn)

        self.assertEqual(closed, 1)
        row = self.conn.execute(
            "SELECT last_activity_at, ended_at FROM _agent_sessions WHERE session_id = ?",
            ("orphan-1",),
        ).fetchone()
        self.assertEqual(row, ("2024-01-01T10:30:00+00:00", "2024-01-01T10:30:00+00:00"))

    def test_reconcile_orphaned_sessions_falls_back_to_started_at(self):
        """A session that never received a touch has no last_activity_at to backdate to."""
        record_session(self.conn, "orphan-2", "/project", "2024-01-01T09:00:00+00:00", "codex")

        closed = reconcile_orphaned_sessions(self.conn)

        self.assertEqual(closed, 1)
        row = self.conn.execute(
            "SELECT ended_at FROM _agent_sessions WHERE session_id = ?", ("orphan-2",)
        ).fetchone()
        self.assertEqual(row[0], "2024-01-01T09:00:00+00:00")

    def test_reconcile_orphaned_sessions_leaves_already_closed_rows_untouched(self):
        record_session(self.conn, "closed-1", "/project", "2024-01-01T09:00:00+00:00", "codex")
        close_session(self.conn, "closed-1", "2024-01-01T09:15:00+00:00")

        closed = reconcile_orphaned_sessions(self.conn)

        self.assertEqual(closed, 0)
        row = self.conn.execute(
            "SELECT ended_at FROM _agent_sessions WHERE session_id = ?", ("closed-1",)
        ).fetchone()
        self.assertEqual(row[0], "2024-01-01T09:15:00+00:00")

    def test_reconcile_orphaned_sessions_returns_zero_on_empty_table(self):
        self.assertEqual(reconcile_orphaned_sessions(self.conn), 0)

    def test_get_last_session_returns_most_recent(self):
        """When multiple sessions share a cwd, get_last_session_for_cwd returns the most recent."""
        cwd = "/home/user/project"
        session_1 = "session-001"
        session_2 = "session-002"
        session_3 = "session-003"

        record_session(self.conn, session_1, cwd, "2024-01-01T10:00:00+00:00")
        record_session(self.conn, session_2, cwd, "2024-01-01T12:00:00+00:00")
        record_session(self.conn, session_3, cwd, "2024-01-01T11:00:00+00:00")

        result = get_last_session_for_cwd(self.conn, cwd)
        self.assertIsNotNone(result)
        self.assertEqual(result["session_id"], session_2)
        self.assertEqual(result["started_at"], "2024-01-01T12:00:00+00:00")

    def test_get_last_session_returns_none_for_unknown_cwd(self):
        """get_last_session_for_cwd returns None when the cwd has no prior sessions."""
        record_session(self.conn, "session-1", "/some/path", "2024-01-01T10:00:00+00:00")
        result = get_last_session_for_cwd(self.conn, "/different/path")
        self.assertIsNone(result)

    def test_get_last_session_exact_cwd_match(self):
        """Sessions for different cwds are never returned."""
        cwd_a = "/home/user/project_a"
        cwd_b = "/home/user/project_b"

        record_session(self.conn, "session-a", cwd_a, "2024-01-01T10:00:00+00:00")
        record_session(self.conn, "session-b", cwd_b, "2024-01-01T11:00:00+00:00")

        result_a = get_last_session_for_cwd(self.conn, cwd_a)
        result_b = get_last_session_for_cwd(self.conn, cwd_b)

        self.assertEqual(result_a["session_id"], "session-a")
        self.assertEqual(result_b["session_id"], "session-b")

    def test_get_last_session_returns_dict_with_required_keys(self):
        """The returned dict has exactly the keys expected by session_digest_service."""
        cwd = "/test/path"
        record_session(self.conn, "test-session", cwd, "2024-01-01T12:00:00+00:00")

        result = get_last_session_for_cwd(self.conn, cwd)
        self.assertIsNotNone(result)
        self.assertIn("session_id", result)
        self.assertIn("started_at", result)
        self.assertEqual(result["session_id"], "test-session")
        self.assertEqual(result["started_at"], "2024-01-01T12:00:00+00:00")

    def test_get_recent_sessions_returns_newest_first(self):
        """get_recent_sessions_for_cwd orders all matching rows newest-to-oldest."""
        cwd = "/home/user/project"
        record_session(self.conn, "session-old", cwd, "2024-01-01T10:00:00+00:00")
        record_session(self.conn, "session-newest", cwd, "2024-01-01T12:00:00+00:00")
        record_session(self.conn, "session-mid", cwd, "2024-01-01T11:00:00+00:00")

        result = get_recent_sessions_for_cwd(self.conn, cwd)
        self.assertEqual(
            [r["session_id"] for r in result],
            ["session-newest", "session-mid", "session-old"],
        )

    def test_get_recent_sessions_respects_limit(self):
        """get_recent_sessions_for_cwd caps the number of rows returned at `limit`."""
        cwd = "/home/user/project"
        for i in range(5):
            record_session(self.conn, f"session-{i}", cwd, f"2024-01-01T{10 + i:02d}:00:00+00:00")

        result = get_recent_sessions_for_cwd(self.conn, cwd, limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["session_id"], "session-4")
        self.assertEqual(result[1]["session_id"], "session-3")

    def test_get_recent_sessions_returns_empty_list_for_unknown_cwd(self):
        """get_recent_sessions_for_cwd returns [] (not None) when the cwd has no sessions."""
        result = get_recent_sessions_for_cwd(self.conn, "/never/seen")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
