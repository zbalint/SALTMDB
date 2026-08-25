"""Tests for saltmdb.db.agent_sessions: session tracking for last-session bootstrap digest."""

import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.db.agent_sessions import (
    record_session,
    get_last_session_for_cwd,
    get_recent_sessions_for_cwd,
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
