"""Tests for saltmdb.db.agent_sessions: session tracking for last-session bootstrap digest."""

import os
import sqlite3
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.db import agent_sessions
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

    def _mk_entity(
        self,
        entity_id,
        *,
        agent_session_id=None,
        last_touched_session_id=None,
        status="raw",
    ):
        now = "2024-01-01T00:00:00+00:00"
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, agent_id,
            scope, status, title, memory_type, full_content, valid_from, agent_session_id,
            last_touched_session_id) VALUES (?, ?, ?, ?, 'tester', 'shared', ?, ?, 'fact',
            'body content', ?, ?, ?)""",
            (
                entity_id,
                now,
                now,
                now,
                status,
                entity_id,
                now,
                agent_session_id,
                last_touched_session_id,
            ),
        )
        self.conn.commit()

    def _mk_trace(self, trace_id, session_id):
        now = "2024-01-01T00:00:00+00:00"
        self.conn.execute(
            """INSERT INTO conversation_traces
               (id, agent_session_id, agent_id, harness, harness_session_id, harness_turn_id,
                status, user_prompt, user_prompt_hash, final_assistant_message,
                final_assistant_message_hash, created_at, updated_at)
               VALUES (?, ?, 'tester', 'claude_code', 'harness', ?, 'completed',
                       'prompt', 'prompt-hash', 'response', 'response-hash', ?, ?)""",
            (trace_id, session_id, f"turn-{trace_id}", now, now),
        )
        self.conn.commit()

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
            "SELECT agent_id, last_activity_at, ended_at, ended_reason "
            "FROM _agent_sessions WHERE session_id = ?",
            ("session-life",),
        ).fetchone()
        self.assertEqual(
            row, ("codex", "2024-01-01T11:00:00+00:00", "2024-01-01T12:00:00+00:00", "goodbye")
        )

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
            "SELECT COUNT(*), agent_id, started_at, last_activity_at, ended_at, ended_reason "
            "FROM _agent_sessions WHERE session_id = ?",
            ("session-reconnect",),
        ).fetchone()
        self.assertEqual(
            row,
            (1, "codex", "2024-01-01T10:00:00+00:00", "2024-01-01T12:00:00+00:00", None, None),
        )

    def test_rehello_does_not_move_activity_backwards(self):
        record_session(
            self.conn, "session-monotonic", "/project", "2024-01-01T12:00:00+00:00", "codex"
        )
        record_session(
            self.conn, "session-monotonic", "/other", "2024-01-01T11:00:00+00:00", "other"
        )
        row = self.conn.execute(
            "SELECT cwd, agent_id, started_at, last_activity_at, ended_at "
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
        self.assertTrue(
            {"agent_id", "last_activity_at", "ended_at", "ended_reason"}.issubset(columns)
        )
        cwd_column = next(
            row
            for row in migrated.execute("PRAGMA table_info(_agent_sessions)").fetchall()
            if row[1] == "cwd"
        )
        self.assertEqual(cwd_column[3], 0, "cwd must be nullable for incomplete registrations")
        row = migrated.execute(
            "SELECT cwd, started_at, agent_id, last_activity_at, ended_at, ended_reason "
            "FROM _agent_sessions WHERE session_id = ?",
            ("legacy-session",),
        ).fetchone()
        self.assertEqual(
            row,
            ("/legacy", "2024-01-01T10:00:00+00:00", None, None, None, None),
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
            "SELECT cwd, started_at, agent_id, last_activity_at, ended_at, ended_reason "
            "FROM _agent_sessions WHERE session_id = ?",
            ("legacy-no-cwd",),
        ).fetchone()
        self.assertEqual(
            row,
            (
                "/enriched",
                "2024-01-01T10:00:00+00:00",
                "codex",
                "2024-01-01T11:00:00+00:00",
                None,
                None,
            ),
        )
        reopened.close()

    def test_reconcile_orphaned_sessions_closes_open_rows_using_last_activity(self):
        """A row left with ended_at NULL by an unclean death is backdated to last_activity_at."""
        record_session(self.conn, "orphan-1", "/project", "2024-01-01T10:00:00+00:00", "codex")
        touch_session(self.conn, "orphan-1", "2024-01-01T10:30:00+00:00")

        closed = reconcile_orphaned_sessions(self.conn)

        self.assertEqual(closed, 1)
        row = self.conn.execute(
            "SELECT last_activity_at, ended_at, ended_reason FROM _agent_sessions "
            "WHERE session_id = ?",
            ("orphan-1",),
        ).fetchone()
        self.assertEqual(
            row, ("2024-01-01T10:30:00+00:00", "2024-01-01T10:30:00+00:00", "orphaned")
        )

    def test_reconcile_orphaned_sessions_falls_back_to_started_at(self):
        """A session that never received a touch has no last_activity_at to backdate to."""
        record_session(self.conn, "orphan-2", "/project", "2024-01-01T09:00:00+00:00", "codex")

        closed = reconcile_orphaned_sessions(self.conn)

        self.assertEqual(closed, 1)
        row = self.conn.execute(
            "SELECT ended_at, ended_reason FROM _agent_sessions WHERE session_id = ?",
            ("orphan-2",),
        ).fetchone()
        self.assertEqual(row, ("2024-01-01T09:00:00+00:00", "orphaned"))

    def test_reconcile_orphaned_sessions_leaves_already_closed_rows_untouched(self):
        record_session(self.conn, "closed-1", "/project", "2024-01-01T09:00:00+00:00", "codex")
        close_session(self.conn, "closed-1", "2024-01-01T09:15:00+00:00")

        closed = reconcile_orphaned_sessions(self.conn)

        self.assertEqual(closed, 0)
        row = self.conn.execute(
            "SELECT ended_at, ended_reason FROM _agent_sessions WHERE session_id = ?",
            ("closed-1",),
        ).fetchone()
        self.assertEqual(
            row,
            ("2024-01-01T09:15:00+00:00", "goodbye"),
            "reconcile must not overwrite a real goodbye's ended_reason",
        )

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

    def test_recent_ended_sessions_follow_end_time_not_start_time(self):
        cwd = "/project"
        record_session(self.conn, "started-first", cwd, "2024-01-01T08:00:00+00:00")
        record_session(self.conn, "started-last", cwd, "2024-01-01T09:00:00+00:00")
        record_session(self.conn, "running", cwd, "2024-01-01T12:00:00+00:00")
        close_session(self.conn, "started-first", "2024-01-01T11:00:00+00:00")
        close_session(self.conn, "started-last", "2024-01-01T10:00:00+00:00")

        result = get_recent_sessions_for_cwd(self.conn, cwd, ended=True)

        self.assertEqual([row["session_id"] for row in result], ["started-first", "started-last"])

    def test_recent_ended_sessions_break_equal_end_times_by_session_id(self):
        cwd = "/project"
        record_session(self.conn, "session-z", cwd, "2024-01-01T08:00:00+00:00")
        record_session(self.conn, "session-a", cwd, "2024-01-01T09:00:00+00:00")
        close_session(self.conn, "session-z", "2024-01-01T10:00:00.123+00:00")
        close_session(self.conn, "session-a", "2024-01-01T10:00:00.123+00:00")

        result = get_recent_sessions_for_cwd(self.conn, cwd, ended=True)

        self.assertEqual([row["session_id"] for row in result], ["session-z", "session-a"])

    def test_recent_running_sessions_follow_activity_with_start_fallback_and_ties(self):
        cwd = "/project"
        for session_id, started in (
            ("active-z", "2024-01-01T08:00:00+00:00"),
            ("active-a", "2024-01-01T09:00:00+00:00"),
            ("fallback", "2024-01-01T11:00:00+00:00"),
            ("ended", "2024-01-01T13:00:00+00:00"),
        ):
            record_session(self.conn, session_id, cwd, started)
        touch_session(self.conn, "active-z", "2024-01-01T12:00:00+00:00")
        touch_session(self.conn, "active-a", "2024-01-01T12:00:00+00:00")
        close_session(self.conn, "ended", "2024-01-01T14:00:00+00:00")
        self.conn.execute(
            "UPDATE _agent_sessions SET last_activity_at = NULL WHERE session_id = ?",
            ("fallback",),
        )

        result = get_recent_sessions_for_cwd(self.conn, cwd, ended=False)

        self.assertEqual(
            [row["session_id"] for row in result], ["active-z", "active-a", "fallback"]
        )

    def test_recent_sessions_return_last_activity_at(self):
        record_session(self.conn, "active", "/project", "2024-01-01T08:00:00+00:00")
        touch_session(self.conn, "active", "2024-01-01T12:00:00+00:00")

        result = get_recent_sessions_for_cwd(self.conn, "/project")

        self.assertEqual(result[0]["last_activity_at"], "2024-01-01T12:00:00+00:00")

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

    def test_recent_memories_skip_twelve_newer_empty_sessions(self):
        cwd = "/project"
        older = "memory-session"
        record_session(self.conn, older, cwd, "2024-01-01T10:00:00+00:00")
        self._mk_entity("memory-entity", agent_session_id=older)
        for i in range(12):
            record_session(self.conn, f"empty-{i}", cwd, f"2024-01-02T{i:02d}:00:00+00:00")

        result = get_recent_sessions_for_cwd(self.conn, cwd, with_content="memories")

        self.assertEqual([row["session_id"] for row in result], [older])

    def test_recent_memories_include_touch_only_session(self):
        cwd = "/project"
        creator = "creator-session"
        touched = "touched-session"
        record_session(self.conn, creator, "/other", "2024-01-01T10:00:00+00:00")
        record_session(self.conn, touched, cwd, "2024-01-01T11:00:00+00:00")
        self._mk_entity(
            "touched-entity",
            agent_session_id=creator,
            last_touched_session_id=touched,
        )

        result = get_recent_sessions_for_cwd(self.conn, cwd, with_content="memories")

        self.assertEqual([row["session_id"] for row in result], [touched])

    def test_recent_memories_ignore_archived_only_session(self):
        cwd = "/project"
        session_id = "archived-session"
        record_session(self.conn, session_id, cwd, "2024-01-01T10:00:00+00:00")
        self._mk_entity("archived-entity", agent_session_id=session_id, status="archived")

        self.assertEqual(get_recent_sessions_for_cwd(self.conn, cwd, with_content="memories"), [])

    def test_recent_traces_skip_twelve_newer_traceless_sessions(self):
        cwd = "/project"
        older = "trace-session"
        record_session(self.conn, older, cwd, "2024-01-01T10:00:00+00:00")
        self._mk_trace("trace-1", older)
        for i in range(12):
            record_session(self.conn, f"empty-{i}", cwd, f"2024-01-02T{i:02d}:00:00+00:00")

        result = get_recent_sessions_for_cwd(self.conn, cwd, with_content="traces")

        self.assertEqual([row["session_id"] for row in result], [older])

    def test_recent_memories_limit_counts_only_qualifying_sessions(self):
        cwd = "/project"
        for i in range(5):
            qualifying = f"qualifying-{i}"
            record_session(self.conn, qualifying, cwd, f"2024-01-01T{i * 2:02d}:00:00+00:00")
            self._mk_entity(f"entity-{i}", agent_session_id=qualifying)
            record_session(
                self.conn,
                f"empty-{i}",
                cwd,
                f"2024-01-01T{i * 2 + 1:02d}:00:00+00:00",
            )

        result = get_recent_sessions_for_cwd(self.conn, cwd, limit=3, with_content="memories")

        self.assertEqual(
            [row["session_id"] for row in result],
            ["qualifying-4", "qualifying-3", "qualifying-2"],
        )

    def test_recent_sessions_default_includes_empty_and_unknown_filter_rejected(self):
        cwd = "/project"
        record_session(self.conn, "empty", cwd, "2024-01-01T10:00:00+00:00")
        record_session(self.conn, "non-empty", cwd, "2024-01-01T11:00:00+00:00")
        self._mk_entity("non-empty-entity", agent_session_id="non-empty")

        result = get_recent_sessions_for_cwd(self.conn, cwd)

        self.assertEqual([row["session_id"] for row in result], ["non-empty", "empty"])
        with self.assertRaisesRegex(ValueError, "memories.*traces"):
            get_recent_sessions_for_cwd(self.conn, cwd, with_content="events")

    def test_memories_query_uses_both_session_indexes(self):
        predicate = agent_sessions._CONTENT_PREDICATES["memories"]
        plan = self.conn.execute(
            f"""EXPLAIN QUERY PLAN
                SELECT s.session_id
                FROM _agent_sessions AS s
                WHERE s.cwd = ? AND ({predicate})
                ORDER BY s.started_at DESC
                LIMIT ?""",
            ("/project", 10),
        ).fetchall()
        plan_text = "\n".join(row[3] for row in plan)

        self.assertIn("idx_entities_agent_session", plan_text)
        self.assertIn("idx_entities_last_touched_session", plan_text)
        self.assertNotIn("SCAN e", plan_text)


if __name__ == "__main__":
    unittest.main()
