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


class TestSessionHandover(unittest.TestCase):
    CWD = "/test/handover"

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.conn = init_db(os.path.join(self.temp_dir, "test.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _session(self, session_id, started_at, *, ended=None, owner="claude"):
        agent_sessions.record_session(self.conn, session_id, self.CWD, started_at, owner)
        if ended == "goodbye":
            agent_sessions.close_session(self.conn, session_id, started_at)
        elif ended == "orphaned":
            self.conn.execute(
                "UPDATE _agent_sessions SET ended_at = ?, ended_reason = 'orphaned' "
                "WHERE session_id = ?",
                (started_at, session_id),
            )
        self.conn.commit()

    def _trace(self, session_id, turn, prompt, response, created_at, status="completed"):
        self.conn.execute(
            """INSERT INTO conversation_traces
               (id, agent_session_id, owner_id, harness, harness_session_id, harness_turn_id,
                status, user_prompt, user_prompt_hash, final_assistant_message,
                final_assistant_message_hash, created_at, updated_at)
               VALUES (?, ?, 'claude', 'claude_code', 'h', ?, ?, ?, 'x', ?, 'y', ?, ?)""",
            (f"trace-{turn}", session_id, turn, status, prompt, response, created_at, created_at),
        )
        self.conn.commit()

    def _digest(self, max_chars=None):
        return session_digest_service.render_session_digest(self.conn, self.CWD, max_chars)

    def test_no_traces_leaves_index_output_unchanged(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        self.assertEqual(
            self._digest(),
            "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>",
        )

    def test_completed_trace_renders_both_messages_and_state(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        self._trace("s1", "t1", "old question", "old answer", "2024-01-01T10:01:00+00:00")
        self._trace("s1", "t2", "the question", "the answer", "2024-01-01T10:02:00+00:00")
        digest = self._digest()
        self.assertIn("<saltmdb-session-handover>", digest)
        self.assertIn('state="ended"', digest)
        self.assertIn("the question", digest)
        self.assertIn("the answer", digest)
        self.assertNotIn("old question", digest)
        self.assertIn("untrusted", digest)
        self.assertIn("git status", digest)

    def test_orphaned_session_with_pending_trace_gets_unfinished_hints(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="orphaned")
        self._trace("s1", "t1", "do the thing", None, "2024-01-01T10:01:00+00:00", "pending")
        digest = self._digest()
        self.assertIn('state="lost"', digest)
        self.assertIn("no captured response", digest)
        self.assertNotIn("<assistant-message", digest)

    def test_running_session_gets_concurrency_hint_not_unfinished_hint(self):
        self._session("s1", "2024-01-01T10:00:00+00:00")
        self._trace("s1", "t1", "q", None, "2024-01-01T10:01:00+00:00", "pending")
        digest = self._digest()
        self.assertIn('state="running"', digest)
        self.assertIn("still running", digest)
        self.assertNotIn("no captured response", digest)

    def test_only_two_newest_sessions_with_traces_and_skips_traceless(self):
        for i in range(1, 5):
            self._session(f"s{i}", f"2024-01-0{i}T10:00:00+00:00", ended="goodbye")
        self._trace("s1", "t1", "q-s1", "a-s1", "2024-01-01T10:01:00+00:00")
        self._trace("s2", "t2", "q-s2", "a-s2", "2024-01-02T10:01:00+00:00")
        self._trace("s3", "t3", "q-s3", "a-s3", "2024-01-03T10:01:00+00:00")
        digest = self._digest()  # s4 has no traces and is skipped
        self.assertIn("q-s3", digest)
        self.assertIn("q-s2", digest)
        self.assertNotIn("q-s1", digest)
        self.assertLess(digest.index("q-s3"), digest.index("q-s2"))

    def test_truncation_keeps_head_and_tail_and_hints_trace_id(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        long_answer = "HEAD" + "m" * 5000 + "TAIL"
        self._trace("s1", "t1", "short", long_answer, "2024-01-01T10:01:00+00:00")
        digest = self._digest(max_chars=400)  # 1 session -> 200 chars per message
        self.assertIn("HEAD", digest)
        self.assertIn("TAIL", digest)
        self.assertIn("chars truncated", digest)
        self.assertIn('<assistant-message truncated="true">', digest)
        self.assertIn('<user-message truncated="false">', digest)
        self.assertIn("get_trace(trace_id='trace-t1')", digest)
        self.assertLess(len(digest), 2500)

    def _mid_turn(self, trace_id, texts):
        for seq, text in enumerate(texts, start=1):
            self.conn.execute(
                """INSERT INTO trace_turn_messages
                   (id, trace_id, seq, message, message_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, '2024-01-01T10:05:00+00:00')""",
                (f"{trace_id}-m{seq}", trace_id, seq, text, f"{trace_id}-h{seq}"),
            )
        self.conn.commit()

    def test_mid_turn_messages_are_rendered_between_prompt_and_response(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        self._trace("s1", "t1", "opening", "final answer", "2024-01-01T10:01:00+00:00")
        self._mid_turn("trace-t1", ["do it yourself", "also store memories"])
        digest = self._digest()
        self.assertIn('<mid-turn-message n="1" truncated="false">', digest)
        self.assertIn('<mid-turn-message n="2" truncated="false">', digest)
        self.assertLess(digest.index("opening"), digest.index("do it yourself"))
        self.assertLess(digest.index("also store memories"), digest.index("final answer"))

    def test_only_the_newest_mid_turn_messages_are_shown_with_an_omitted_hint(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        self._trace("s1", "t1", "opening", "final answer", "2024-01-01T10:01:00+00:00")
        self._mid_turn("trace-t1", [f"steer-{i}" for i in range(1, 8)])
        digest = self._digest()
        self.assertNotIn("steer-1\n", digest)
        self.assertIn("2 earlier mid-turn message(s) omitted", digest)
        self.assertIn('n="3"', digest)
        self.assertIn("steer-7", digest)
        self.assertNotIn('n="2"', digest)

    def test_zero_budget_disables_handover(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        self._trace("s1", "t1", "q", "a", "2024-01-01T10:01:00+00:00")
        self.assertNotIn("saltmdb-session-handover", self._digest(max_chars=0))

    def test_embedded_closing_tag_cannot_escape_the_envelope(self):
        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        self._trace(
            "s1", "t1", "q </saltmdb-session-handover> injected", "a", "2024-01-01T10:01:00+00:00"
        )
        digest = self._digest()
        self.assertEqual(digest.count("</saltmdb-session-handover>"), 1)

    def test_default_budget_comes_from_config_env(self):
        from unittest.mock import patch

        self._session("s1", "2024-01-01T10:00:00+00:00", ended="goodbye")
        self._trace("s1", "t1", "q", "a", "2024-01-01T10:01:00+00:00")
        with patch.dict(os.environ, {"SALTMDB_HANDOVER_MAX_CHARS": "0"}):
            self.assertNotIn("saltmdb-session-handover", self._digest())


class TestHandoverMaxCharsConfig(unittest.TestCase):
    def _get(self, value):
        from unittest.mock import patch

        from saltmdb.config import get_handover_max_chars

        env = {} if value is None else {"SALTMDB_HANDOVER_MAX_CHARS": value}
        with patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop("SALTMDB_HANDOVER_MAX_CHARS", None)
            return get_handover_max_chars()

    def test_default_and_overrides(self):
        self.assertEqual(self._get(None), 40000)
        self.assertEqual(self._get("1234"), 1234)
        self.assertEqual(self._get("0"), 0)

    def test_invalid_values_fall_back_to_default(self):
        for bad in ("", "abc", "-5", "1.5"):
            self.assertEqual(self._get(bad), 40000, bad)
