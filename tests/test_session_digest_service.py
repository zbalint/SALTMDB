"""Tests for saltmdb.domain.services.session_digest_service."""

import os
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime

from saltmdb.db.schema import init_db
from saltmdb.db import agent_sessions
from saltmdb.domain.services import session_digest_service


class TestSessionDigestService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk_entity(
        self,
        title,
        *,
        agent_session_id=None,
        last_touched_session_id=None,
        status="raw",
        memory_type="fact",
    ):
        """Create an entity in the database."""
        entity_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id,
            scope, status, title, memory_type, full_content, valid_from, agent_session_id,
            last_touched_session_id) VALUES (?, ?, ?, ?, 'tester', 'shared', ?, ?, ?,
            'body content', ?, ?, ?)""",
            (
                entity_id,
                now,
                now,
                now,
                status,
                title,
                memory_type,
                now,
                agent_session_id,
                last_touched_session_id,
            ),
        )
        self.conn.commit()
        return entity_id

    def test_no_prior_session_returns_empty_envelope(self):
        """Empty envelope when no prior session exists for this cwd."""
        cwd = "/some/directory"
        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertEqual(digest, "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>")

    def test_prior_session_with_no_surviving_memories_returns_empty_envelope(self):
        """Empty envelope when prior session exists but has no non-archived memories."""
        cwd = "/test/project"
        session_id = "prior-session-123"
        started_at = "2024-01-01T10:00:00+00:00"
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        # Create an archived entity from that session
        self._mk_entity("Archived Memory", agent_session_id=session_id, status="archived")

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertEqual(digest, "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>")

    def test_prior_session_with_non_archived_entities_renders_digest(self):
        """Digest includes non-archived entities created by or touched by prior session."""
        cwd = "/test/project"
        session_id = "prior-session-456"
        started_at = "2024-01-01T10:00:00+00:00"
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        # Create entities created by the prior session
        mem1_id = self._mk_entity("Memory 1", agent_session_id=session_id)
        mem2_id = self._mk_entity("Memory 2", agent_session_id=session_id)

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertIn("<saltmdb-last-session-digest", digest)
        self.assertIn(session_id, digest)
        self.assertIn(started_at, digest)
        self.assertIn("Memory 1", digest)
        self.assertIn("Memory 2", digest)
        self.assertIn(mem1_id, digest)
        self.assertIn(mem2_id, digest)

    def test_entities_from_last_touched_session_id_appear_in_digest(self):
        """Entities with last_touched_session_id matching the prior session are included."""
        cwd = "/test/project"
        session_id = "prior-session-789"
        started_at = "2024-01-01T10:00:00+00:00"
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        # Create entity touched (but not created) by the prior session
        mem_id = self._mk_entity("Touched Memory", last_touched_session_id=session_id)

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertIn("Touched Memory", digest)
        self.assertIn(mem_id, digest)

    def test_archived_entities_excluded_from_digest(self):
        """Archived entities from prior session do not appear in digest."""
        cwd = "/test/project"
        session_id = "prior-session-archived"
        started_at = "2024-01-01T10:00:00+00:00"
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        # Mix of archived and non-archived
        self._mk_entity("Active Memory", agent_session_id=session_id, status="raw")
        self._mk_entity("Archived Memory", agent_session_id=session_id, status="archived")

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertIn("Active Memory", digest)
        self.assertNotIn("Archived Memory", digest)

    def test_entities_from_different_owner_ids_both_appear(self):
        """Digest includes entities from multiple owner_ids (no owner_id filter)."""
        cwd = "/test/project"
        session_id = "multi-owner-session"
        started_at = "2024-01-01T10:00:00+00:00"
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        # Create entities from different owners in the same session
        # (manually insert with different owner_id)
        entity_id_1 = str(uuid.uuid4())
        entity_id_2 = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id,
            scope, status, title, memory_type, full_content, valid_from, agent_session_id)
            VALUES (?, ?, ?, ?, ?, 'shared', 'raw', ?, 'fact', 'body', ?, ?)""",
            (entity_id_1, now, now, now, "owner1", "Alice's Memory", now, session_id),
        )
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id,
            scope, status, title, memory_type, full_content, valid_from, agent_session_id)
            VALUES (?, ?, ?, ?, ?, 'shared', 'raw', ?, 'fact', 'body', ?, ?)""",
            (entity_id_2, now, now, now, "owner2", "Bob's Memory", now, session_id),
        )
        self.conn.commit()

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertIn("Alice's Memory", digest)
        self.assertIn("Bob's Memory", digest)

    def test_session_for_different_cwd_never_contributes(self):
        """Sessions for other cwds are never returned, even if more recent."""
        cwd_a = "/project/a"
        cwd_b = "/project/b"
        session_a = "session-a"
        session_b = "session-b"

        agent_sessions.record_session(self.conn, session_a, cwd_a, "2024-01-01T10:00:00+00:00")
        agent_sessions.record_session(self.conn, session_b, cwd_b, "2024-01-01T11:00:00+00:00")

        self._mk_entity("Memory A", agent_session_id=session_a)
        self._mk_entity("Memory B", agent_session_id=session_b)

        # Query cwd_a's digest (should not include cwd_b's entities even though session_b is newer)
        digest_a = session_digest_service.render_last_session_digest(self.conn, cwd_a)
        self.assertIn("Memory A", digest_a)
        self.assertNotIn("Memory B", digest_a)
        self.assertIn(session_a, digest_a)
        self.assertNotIn(session_b, digest_a)

    def test_memory_type_preserved_in_digest(self):
        """The memory_type field is included in the digest for each memory."""
        cwd = "/test/project"
        session_id = "type-test-session"
        started_at = "2024-01-01T10:00:00+00:00"
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        self._mk_entity("Fact Memory", agent_session_id=session_id, memory_type="fact")
        self._mk_entity("Procedure Memory", agent_session_id=session_id, memory_type="procedure")
        self._mk_entity("Decision Memory", agent_session_id=session_id, memory_type="decision")

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        # Each line should include [type] notation
        self.assertIn("[fact]", digest)
        self.assertIn("[procedure]", digest)
        self.assertIn("[decision]", digest)

    def test_falls_back_past_newer_content_free_session(self):
        """A newer registered session with zero surviving entities (e.g. a concurrently-started
        sibling session's hello, registered before it has produced anything) does not shadow an
        older session that has real content -- the digest should walk back to the older one
        instead of rendering empty. Regression test for the live repro in SALTMDB memory 8402f500
        (concurrent Claude + Codex sessions in the same cwd)."""
        cwd = "/test/project"
        older_session = "older-session-with-content"
        newer_empty_session = "newer-session-empty"

        agent_sessions.record_session(self.conn, older_session, cwd, "2024-01-01T10:00:00+00:00")
        self._mk_entity("Real Memory", agent_session_id=older_session)

        # Registered later, but never produced anything (e.g. a sibling session's hello that
        # fired before its own first tool call).
        agent_sessions.record_session(
            self.conn, newer_empty_session, cwd, "2024-01-01T11:00:00+00:00"
        )

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertIn(older_session, digest)
        self.assertIn("Real Memory", digest)
        self.assertNotIn(newer_empty_session, digest)

    def test_empty_envelope_when_all_recent_sessions_are_content_free(self):
        """If every recent session for this cwd (within the lookback window) has zero surviving
        entities, the digest is still the empty envelope, not an error."""
        cwd = "/test/project"
        agent_sessions.record_session(self.conn, "empty-1", cwd, "2024-01-01T10:00:00+00:00")
        agent_sessions.record_session(self.conn, "empty-2", cwd, "2024-01-01T11:00:00+00:00")

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        self.assertEqual(digest, "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>")

    def test_title_escaping_in_digest(self):
        """Titles with special YAML characters are escaped."""
        cwd = "/test/project"
        session_id = "escape-test-session"
        started_at = "2024-01-01T10:00:00+00:00"
        agent_sessions.record_session(self.conn, session_id, cwd, started_at)

        # Title with backslash, quotes, and newlines
        self._mk_entity(
            'Title with "quotes" and \\backslash and\nnewline',
            agent_session_id=session_id,
        )

        digest = session_digest_service.render_last_session_digest(self.conn, cwd)
        # _escape_yaml_line escapes backslashes first, then quotes, then collapses newlines to
        # spaces -- so the raw title's `"quotes"` becomes `\"quotes\"` and `\backslash` becomes
        # `\\backslash` in the rendered digest line.
        self.assertIn('\\"quotes\\"', digest)
        self.assertIn("\\\\backslash", digest)
        self.assertNotIn("\n-", digest[digest.find("Title") :])  # no raw newline in the title line


if __name__ == "__main__":
    unittest.main()
