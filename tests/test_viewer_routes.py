import unittest
import tempfile
import os
import shutil
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler
from saltmdb.domain.services import relation_service
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation


class DummyRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class DummyServer:
    pass


class BrokenWFile:
    def write(self, b):
        raise ConnectionAbortedError(
            10053, "An established connection was aborted by the software in your host machine"
        )


class TestViewerRoutes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_handler_get_db_connection(self):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        conn = handler.get_db_connection()
        self.assertIsNotNone(conn)
        conn.close()

    def test_client_disconnect_during_send_json_and_html(self):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        handler.requestline = "GET / HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.wfile = BrokenWFile()
        # Should catch ConnectionAbortedError silently without throwing
        handler.send_json({"test": "data"})
        handler.send_html("<html></html>")


class TestViewerBrowseAndHybridSearch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "viewer-browse.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _handler(self):
        return SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())

    @staticmethod
    def _capture(handler):
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        return captured

    def _insert_entity(self, entity_id, created_at, updated_at):
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, created_at, updated_at, updated_at, entity_id, f"content {entity_id}"),
        )
        self.conn.commit()

    def test_entities_sort_and_inclusive_utc_date_range_share_count_and_pages(self):
        self._insert_entity("early", "2026-08-10T23:00:00+00:00", "2026-08-11T01:00:00+00:00")
        self._insert_entity("middle", "2026-08-11T12:00:00+00:00", "2026-08-12T01:00:00+00:00")
        self._insert_entity("late", "2026-08-12T01:00:00+00:00", "2026-08-13T01:00:00+00:00")
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_entities(
            {
                "sort": ["created_desc"],
                "date_field": ["created"],
                "date_from": ["2026-08-11"],
                "date_to": ["2026-08-11"],
                "limit": ["1"],
            }
        )
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["data"]["total_count"], 1)
        self.assertEqual(captured["data"]["total_pages"], 1)
        self.assertEqual(captured["data"]["entities"][0]["id"], "middle")
        self.assertEqual(captured["data"]["sort"], "created_desc")

    def test_entities_reject_invalid_temporal_query(self):
        handler = self._handler()
        missing_field = self._capture(handler)
        handler.get_entities({"date_from": ["2026-08-11"]})
        self.assertEqual(missing_field["status"], 400)
        invalid_sort = self._capture(handler)
        handler.get_entities({"sort": ["relevance"]})
        self.assertEqual(invalid_sort["status"], 400)
        reversed_range = self._capture(handler)
        handler.get_entities(
            {"date_field": ["updated"], "date_from": ["2026-08-12"], "date_to": ["2026-08-11"]}
        )
        self.assertEqual(reversed_range["status"], 400)

    @patch("saltmdb.viewer.routes.memory_service.search_memory")
    def test_search_delegates_to_broad_hybrid_service(self, search_memory_mock):
        search_memory_mock.return_value = [
            {"id": "hybrid-hit", "title": "Hybrid hit", "score": 0.42}
        ]
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_search({"q": ["meaningful query"]})
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["data"]["mode"], "broad")
        self.assertEqual(captured["data"]["results"][0]["id"], "hybrid-hit")
        search_memory_mock.assert_called_once_with(
            query_keywords="meaningful query",
            limit=50,
            include_related=False,
            mode="broad",
            db_path=self.db_path,
        )

    @patch("saltmdb.viewer.routes.memory_service.search_memory")
    def test_search_passes_through_agent_session_id_filter(self, search_memory_mock):
        search_memory_mock.return_value = []
        handler = self._handler()
        self._capture(handler)
        handler.get_search({"q": ["meaningful query"], "agent_session_id": ["sess-alpha"]})
        search_memory_mock.assert_called_once_with(
            query_keywords="meaningful query",
            limit=50,
            include_related=False,
            mode="broad",
            db_path=self.db_path,
            agent_session_id="sess-alpha",
        )


class TestViewerAgentSessions(unittest.TestCase):
    """GET /api/entities?session_id=, /api/events?agent_session_id=, and /api/sessions.

    Covers the backend half of the agent-session-browse feature scoped in handover
    memory 1ce2f08b: exposing agent_session_id/last_touched_session_id on
    get_entities, adding the missing agent_session_id filter to get_events, and the
    new get_sessions distinct-session enumeration (nothing previously listed
    distinct session ids at all).
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "viewer-sessions.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _handler(self):
        return SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())

    @staticmethod
    def _capture(handler):
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        return captured

    def _insert_entity(
        self, entity_id, created_at, updated_at, agent_session_id=None, last_touched_session_id=None
    ):
        self.conn.execute(
            """INSERT INTO entities
               (id, created_at, updated_at, last_accessed_at, title, full_content,
                agent_session_id, last_touched_session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_id,
                created_at,
                updated_at,
                updated_at,
                entity_id,
                f"content {entity_id}",
                agent_session_id,
                last_touched_session_id,
            ),
        )
        self.conn.commit()

    def _insert_event(
        self, event_id, timestamp, event_type, agent_session_id=None, agent_id="tester"
    ):
        self.conn.execute(
            """INSERT INTO events (id, timestamp, agent_id, type, content, agent_session_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, timestamp, agent_id, event_type, f"content {event_id}", agent_session_id),
        )
        self.conn.commit()

    def test_get_entities_exposes_session_columns_and_session_id_filters_created_or_touched(self):
        # created by sess-a, never touched by anyone else
        self._insert_entity(
            "created-only",
            "2026-08-24T10:00:00+00:00",
            "2026-08-24T10:00:00+00:00",
            agent_session_id="sess-a",
            last_touched_session_id="sess-a",
        )
        # created by sess-b, later touched (e.g. supersede/consolidate) by sess-a
        self._insert_entity(
            "touched-only",
            "2026-08-24T09:00:00+00:00",
            "2026-08-24T11:00:00+00:00",
            agent_session_id="sess-b",
            last_touched_session_id="sess-a",
        )
        # unrelated to sess-a entirely
        self._insert_entity(
            "unrelated",
            "2026-08-24T09:00:00+00:00",
            "2026-08-24T09:00:00+00:00",
            agent_session_id="sess-c",
            last_touched_session_id="sess-c",
        )

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_entities({})
        by_id = {e["id"]: e for e in captured["data"]["entities"]}
        self.assertEqual(by_id["created-only"]["agent_session_id"], "sess-a")
        self.assertEqual(by_id["created-only"]["last_touched_session_id"], "sess-a")
        self.assertEqual(by_id["touched-only"]["agent_session_id"], "sess-b")
        self.assertEqual(by_id["touched-only"]["last_touched_session_id"], "sess-a")

        captured_filtered = self._capture(handler)
        handler.get_entities({"session_id": ["sess-a"]})
        filtered_ids = {e["id"] for e in captured_filtered["data"]["entities"]}
        self.assertEqual(filtered_ids, {"created-only", "touched-only"})

    def test_get_entity_detail_exposes_session_columns(self):
        self._insert_entity(
            "touched-only",
            "2026-08-24T09:00:00+00:00",
            "2026-08-24T11:00:00+00:00",
            agent_session_id="sess-b",
            last_touched_session_id="sess-a",
        )

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_entity_detail("touched-only")
        self.assertEqual(captured["data"]["agent_session_id"], "sess-b")
        self.assertEqual(captured["data"]["last_touched_session_id"], "sess-a")

    def test_get_events_agent_session_id_filter(self):
        self._insert_event(
            "evt-1", "2026-08-24T10:00:00+00:00", "decision", agent_session_id="sess-a"
        )
        self._insert_event("evt-2", "2026-08-24T10:05:00+00:00", "issue", agent_session_id="sess-b")

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_events({"agent_session_id": ["sess-a"]})
        ids = {e["id"] for e in captured["data"]["events"]}
        self.assertEqual(ids, {"evt-1"})

    def test_get_sessions_aggregates_memory_and_event_counts_sorted_by_recency(self):
        # sess-a: two memories (one created, one only touched) + one event; most recent activity.
        self._insert_entity(
            "a-created",
            "2026-08-24T08:00:00+00:00",
            "2026-08-24T08:00:00+00:00",
            agent_session_id="sess-a",
            last_touched_session_id="sess-a",
        )
        self._insert_entity(
            "a-touched",
            "2026-08-23T08:00:00+00:00",
            "2026-08-25T12:00:00+00:00",
            agent_session_id="sess-old",
            last_touched_session_id="sess-a",
        )
        self._insert_event(
            "a-evt", "2026-08-24T09:00:00+00:00", "decision", agent_session_id="sess-a"
        )
        # sess-old: one memory (as creator only, already counted for sess-a as toucher), older activity.
        self._insert_event(
            "old-evt", "2026-08-20T09:00:00+00:00", "issue", agent_session_id="sess-old"
        )

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({})
        self.assertEqual(captured["status"], 200)
        by_id = {s["session_id"]: s for s in captured["data"]["sessions"]}

        self.assertEqual(by_id["sess-a"]["memory_count"], 2)
        self.assertEqual(by_id["sess-a"]["event_count"], 1)
        self.assertEqual(by_id["sess-old"]["memory_count"], 1)
        self.assertEqual(by_id["sess-old"]["event_count"], 1)
        self.assertIsNone(by_id["sess-old"]["owner_id"])
        self.assertIsNone(by_id["sess-old"]["ended_at"])
        self.assertEqual(by_id["sess-old"]["liveness"], "unknown")

        # sess-a's last-touched memory update (2026-08-25T12:00) is its most recent activity,
        # more recent than sess-old's latest event (2026-08-20) -- so sess-a sorts first.
        session_order = [s["session_id"] for s in captured["data"]["sessions"]]
        self.assertEqual(session_order.index("sess-a"), 0)
        self.assertLess(session_order.index("sess-a"), session_order.index("sess-old"))

    def test_get_sessions_id_prefix_filter_and_pagination(self):
        for i in range(3):
            self._insert_entity(
                f"e{i}",
                "2026-08-24T08:00:00+00:00",
                "2026-08-24T08:00:00+00:00",
                agent_session_id=f"prefix-x-{i}",
                last_touched_session_id=f"prefix-x-{i}",
            )
        self._insert_entity(
            "e-other",
            "2026-08-24T08:00:00+00:00",
            "2026-08-24T08:00:00+00:00",
            agent_session_id="other-session",
            last_touched_session_id="other-session",
        )

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({"id_prefix": ["prefix-x-"]})
        ids = {s["session_id"] for s in captured["data"]["sessions"]}
        self.assertEqual(ids, {"prefix-x-0", "prefix-x-1", "prefix-x-2"})
        self.assertEqual(captured["data"]["total_count"], 3)

        captured_page = self._capture(handler)
        handler.get_sessions({"id_prefix": ["prefix-x-"], "limit": ["2"], "page": ["2"]})
        self.assertEqual(len(captured_page["data"]["sessions"]), 1)
        self.assertEqual(captured_page["data"]["total_pages"], 2)

    def test_get_sessions_exposes_persisted_lifecycle_metadata(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions (session_id, cwd, started_at, owner_id, last_activity_at, ended_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "lifecycle-session",
                "/project",
                "2026-08-25T08:00:00+00:00",
                "codex",
                "2026-08-25T09:00:00+00:00",
                None,
            ),
        )
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({})
        row = next(
            s for s in captured["data"]["sessions"] if s["session_id"] == "lifecycle-session"
        )
        self.assertEqual(row["owner_id"], "codex")
        self.assertEqual(row["last_activity_at"], "2026-08-25T09:00:00+00:00")
        self.assertEqual(row["liveness"], "unknown")

    def _handler_with_known_empty_liveness(self):
        """A daemon IS reachable (liveness_known=True) but reports zero active sessions --
        needed because ended/lost only ever surface when liveness_known is True; self._handler()'s
        bare DummyServer has no daemon_state at all, which always reads as liveness_known=False."""

        class EmptyLiveState:
            @staticmethod
            def viewer_snapshot():
                return {"active_agent_session_ids": []}

        server = DummyServer()
        server.daemon_state = EmptyLiveState()
        return SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), server)

    def test_get_sessions_shows_ended_for_a_clean_goodbye(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, ended_at, ended_reason) VALUES (?, ?, ?, ?, ?)",
            (
                "goodbye-session",
                "/project",
                "2026-08-25T08:00:00+00:00",
                "2026-08-25T09:00:00+00:00",
                "goodbye",
            ),
        )
        handler = self._handler_with_known_empty_liveness()
        captured = self._capture(handler)
        handler.get_sessions({})
        row = next(s for s in captured["data"]["sessions"] if s["session_id"] == "goodbye-session")
        self.assertEqual(row["liveness"], "ended")

    def test_get_sessions_shows_ended_for_a_legacy_row_with_no_ended_reason(self):
        """A row closed before ended_reason existed (or by a pre-migration daemon) must not be
        misread as "lost" -- absence of a reason defaults to the plain clean-ended reading."""
        self.conn.execute(
            "INSERT INTO _agent_sessions (session_id, cwd, started_at, ended_at) VALUES (?, ?, ?, ?)",
            (
                "legacy-ended-session",
                "/project",
                "2026-08-25T08:00:00+00:00",
                "2026-08-25T09:00:00+00:00",
            ),
        )
        handler = self._handler_with_known_empty_liveness()
        captured = self._capture(handler)
        handler.get_sessions({})
        row = next(
            s for s in captured["data"]["sessions"] if s["session_id"] == "legacy-ended-session"
        )
        self.assertEqual(row["liveness"], "ended")

    def test_get_sessions_shows_lost_for_an_orphaned_session(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, ended_at, ended_reason) VALUES (?, ?, ?, ?, ?)",
            (
                "orphaned-session",
                "/project",
                "2026-08-25T08:00:00+00:00",
                "2026-08-25T09:00:00+00:00",
                "orphaned",
            ),
        )
        handler = self._handler_with_known_empty_liveness()
        captured = self._capture(handler)
        handler.get_sessions({})
        row = next(s for s in captured["data"]["sessions"] if s["session_id"] == "orphaned-session")
        self.assertEqual(row["liveness"], "lost")
        self.assertEqual(row["ended_reason"], "orphaned")

    def test_get_sessions_derives_active_only_from_daemon_registry(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions (session_id, cwd, started_at, ended_at) VALUES (?, ?, ?, ?)",
            ("live-session", "/project", "2026-08-25T08:00:00+00:00", None),
        )
        self.conn.commit()

        class LiveState:
            @staticmethod
            def viewer_snapshot():
                return {"active_agent_session_ids": ["live-session"]}

        server = DummyServer()
        server.daemon_state = LiveState()
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), server)
        captured = self._capture(handler)
        handler.get_sessions({})
        row = next(s for s in captured["data"]["sessions"] if s["session_id"] == "live-session")
        self.assertEqual(row["liveness"], "active")

    def test_get_sessions_falls_back_to_unknown_when_daemon_liveness_fails(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions (session_id, cwd, started_at, ended_at) VALUES (?, ?, ?, ?)",
            ("unavailable-session", "/project", "2026-08-25T08:00:00+00:00", None),
        )
        self.conn.commit()

        class BrokenState:
            @staticmethod
            def viewer_snapshot():
                raise OSError("daemon unavailable")

        server = DummyServer()
        server.daemon_state = BrokenState()
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), server)
        captured = self._capture(handler)
        handler.get_sessions({})
        row = next(
            s for s in captured["data"]["sessions"] if s["session_id"] == "unavailable-session"
        )
        self.assertEqual(row["liveness"], "unknown")

    def test_get_sessions_state_filter(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, ended_at, ended_reason) VALUES (?, ?, ?, ?, ?)",
            (
                "ended-session",
                "/project/ended",
                "2026-08-24T08:00:00+00:00",
                "2026-08-24T09:00:00+00:00",
                "goodbye",
            ),
        )
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, ended_at, ended_reason) VALUES (?, ?, ?, ?, ?)",
            (
                "lost-session",
                "/project/lost",
                "2026-08-25T08:00:00+00:00",
                "2026-08-25T09:00:00+00:00",
                "orphaned",
            ),
        )
        self.conn.commit()

        handler = self._handler_with_known_empty_liveness()
        captured = self._capture(handler)
        handler.get_sessions({"state": ["lost"]})
        self.assertEqual(captured["status"], 200)
        self.assertEqual([s["session_id"] for s in captured["data"]["sessions"]], ["lost-session"])

    def test_get_sessions_state_filter_rejects_invalid_value(self):
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({"state": ["bogus"]})
        self.assertEqual(captured["status"], 400)

    def test_get_sessions_owner_id_substring_filter(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "claude-session",
                "/project/claude",
                "2026-08-24T08:00:00+00:00",
                "claude",
                "2026-08-24T09:00:00+00:00",
                None,
            ),
        )
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "antigravity-session",
                "/project/antigravity",
                "2026-08-25T08:00:00+00:00",
                "antigravity",
                "2026-08-25T09:00:00+00:00",
                None,
            ),
        )
        self.conn.commit()

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({"owner_id": ["clau"]})
        self.assertEqual(
            [s["session_id"] for s in captured["data"]["sessions"]], ["claude-session"]
        )

    def test_get_sessions_cwd_substring_filter(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "project-a-session",
                "/home/user/project-a",
                "2026-08-24T08:00:00+00:00",
                "tester",
                "2026-08-24T09:00:00+00:00",
                None,
            ),
        )
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "project-b-session",
                "/home/user/project-b",
                "2026-08-25T08:00:00+00:00",
                "tester",
                "2026-08-25T09:00:00+00:00",
                None,
            ),
        )
        self.conn.commit()

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({"cwd": ["project-a"]})
        self.assertEqual(
            [s["session_id"] for s in captured["data"]["sessions"]], ["project-a-session"]
        )

    def test_get_sessions_date_range_filter(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "early-session",
                "/project/early",
                "2026-08-20T10:00:00+00:00",
                "tester",
                "2026-08-20T11:00:00+00:00",
                None,
            ),
        )
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "late-session",
                "/project/late",
                "2026-08-25T10:00:00+00:00",
                "tester",
                "2026-08-25T11:00:00+00:00",
                None,
            ),
        )
        self.conn.commit()

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions(
            {
                "date_field": ["started_at"],
                "date_from": ["2026-08-24"],
                "date_to": ["2026-08-26"],
            }
        )
        self.assertEqual([s["session_id"] for s in captured["data"]["sessions"]], ["late-session"])

    def test_get_sessions_date_filter_rejects_bound_without_field(self):
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({"date_from": ["2026-08-24"]})
        self.assertEqual(captured["status"], 400)

    def test_get_sessions_sort_started_asc_and_desc(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "earliest-session",
                "/project/earliest",
                "2026-08-20T10:00:00+00:00",
                "tester",
                "2026-08-20T11:00:00+00:00",
                None,
            ),
        )
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "latest-session",
                "/project/latest",
                "2026-08-25T10:00:00+00:00",
                "tester",
                "2026-08-25T11:00:00+00:00",
                None,
            ),
        )
        self.conn.commit()
        self._insert_entity(
            "no-started-memory",
            "2026-08-24T10:00:00+00:00",
            "2026-08-24T10:00:00+00:00",
            agent_session_id="no-started-session",
        )

        handler = self._handler()
        captured_asc = self._capture(handler)
        handler.get_sessions({"sort": ["started_asc"]})
        self.assertEqual(
            [s["session_id"] for s in captured_asc["data"]["sessions"]],
            ["earliest-session", "latest-session", "no-started-session"],
        )

        captured_desc = self._capture(handler)
        handler.get_sessions({"sort": ["started_desc"]})
        self.assertEqual(
            [s["session_id"] for s in captured_desc["data"]["sessions"]],
            ["latest-session", "earliest-session", "no-started-session"],
        )

    def test_get_sessions_sort_rejects_invalid_value(self):
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({"sort": ["bogus"]})
        self.assertEqual(captured["status"], 400)

    def test_get_session_detail_not_found(self):
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_session_detail("does-not-exist")
        self.assertEqual(captured["status"], 404)

    def test_get_session_detail_legacy_session_with_no_lifecycle_row(self):
        self._insert_entity(
            "legacy-memory",
            "2026-08-24T10:00:00+00:00",
            "2026-08-24T10:00:00+00:00",
            agent_session_id="legacy-sess",
        )

        handler = self._handler()
        captured = self._capture(handler)
        handler.get_session_detail("legacy-sess")
        self.assertEqual(captured["status"], 200)
        self.assertIsNone(captured["data"]["cwd"])
        self.assertIsNone(captured["data"]["owner_id"])
        self.assertEqual(captured["data"]["memory_count"], 1)

    def test_get_session_detail_full_lifecycle_row(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions "
            "(session_id, cwd, started_at, owner_id, last_activity_at, ended_at, ended_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "full-session",
                "/project/full",
                "2026-08-25T08:00:00+00:00",
                "owner-full",
                "2026-08-25T09:00:00+00:00",
                "2026-08-25T10:00:00+00:00",
                "goodbye",
            ),
        )
        self.conn.commit()
        self._insert_entity(
            "full-memory",
            "2026-08-25T08:30:00+00:00",
            "2026-08-25T08:30:00+00:00",
            agent_session_id="full-session",
        )
        self._insert_event(
            "full-event",
            "2026-08-25T08:45:00+00:00",
            "decision",
            agent_session_id="full-session",
        )

        handler = self._handler_with_known_empty_liveness()
        captured = self._capture(handler)
        handler.get_session_detail("full-session")
        data = captured["data"]
        self.assertEqual(captured["status"], 200)
        self.assertEqual(data["session_id"], "full-session")
        self.assertEqual(data["owner_id"], "owner-full")
        self.assertEqual(data["cwd"], "/project/full")
        self.assertEqual(data["liveness"], "ended")
        self.assertEqual(data["started_at"], "2026-08-25T08:00:00+00:00")
        self.assertEqual(data["last_activity_at"], "2026-08-25T09:00:00+00:00")
        self.assertEqual(data["ended_at"], "2026-08-25T10:00:00+00:00")
        self.assertEqual(data["ended_reason"], "goodbye")
        self.assertEqual(data["memory_count"], 1)
        self.assertEqual(data["event_count"], 1)

    def test_get_session_detail_active_session(self):
        self.conn.execute(
            "INSERT INTO _agent_sessions (session_id, cwd, started_at, ended_at) VALUES (?, ?, ?, ?)",
            ("active-session", "/project/active", "2026-08-25T08:00:00+00:00", None),
        )
        self.conn.commit()

        class LiveState:
            @staticmethod
            def viewer_snapshot():
                return {"active_agent_session_ids": ["active-session"]}

        server = DummyServer()
        server.daemon_state = LiveState()
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), server)
        captured = self._capture(handler)
        handler.get_session_detail("active-session")
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["data"]["liveness"], "active")

    def test_get_sessions_rejects_invalid_pagination(self):
        handler = self._handler()
        captured = self._capture(handler)
        handler.get_sessions({"limit": ["0"]})
        self.assertEqual(captured["status"], 400)


class TestViewerRoutesLineageAndParentIds(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _handler(self):
        return SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())

    def _capture_json(self, handler):
        captured = {}

        def fake_send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        handler.send_json = fake_send_json
        return captured

    @staticmethod
    def _memory_id(result):
        return result["data"]["id"]

    def _mk(self, title, owner_id="viewer_tester"):
        res = store_memory(
            content=f"Raw content body for entity {title}",
            title=title,
            owner_id=owner_id,
            db_connection=self.conn,
        )
        return self._memory_id(res)

    def _consolidate_two(self, title_a, title_b, cons_title, marker):
        a = self._mk(title_a)
        b = self._mk(title_b)
        content = (
            f"# {cons_title}\n\n"
            f"Synthesized summary combining {title_a} and {title_b} facts for the {marker} scenario.\n"
            "- Detail alpha\n- Detail beta"
        )
        res = commit_consolidation(
            parent_ids=[a, b],
            title=cons_title,
            content=content,
            owner_id="viewer_tester",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", res)
        return a, b, res.split("ID: ")[1].strip()

    def test_get_lineage_delegates_and_matches_relation_service_directly(self):
        a, b, c1 = self._consolidate_two(
            "Viewer Lineage A", "Viewer Lineage B", "Viewer Lineage C1", "delegate-match"
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_lineage(c1)

        direct = relation_service.analyze_lineage(entity_id=c1, db_connection=self.conn)

        payload = captured["data"]
        self.assertEqual(payload["root_id"], c1)

        direct_ids = {n["id"] for n in direct["ancestors"]}
        payload_ids = {n["id"] for n in payload["nodes"]}
        self.assertEqual(
            direct_ids,
            payload_ids,
            "handler's nodes list must match relation_service.analyze_lineage() directly",
        )

        direct_by_id = {n["id"]: n for n in direct["ancestors"]}
        for node in payload["nodes"]:
            d = direct_by_id[node["id"]]
            self.assertEqual(node["depth"], d["generation_depth"])
            self.assertEqual(node["title"], d["title"])
            self.assertEqual(node["status"], d["status"])

    def test_get_lineage_nodes_have_depth_and_generation_depth_equal_and_expected_keys(self):
        a, b, c1 = self._consolidate_two(
            "Viewer Depth A", "Viewer Depth B", "Viewer Depth C1", "depth-keys"
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_lineage(c1)

        nodes = captured["data"]["nodes"]
        self.assertGreaterEqual(len(nodes), 1)
        for node in nodes:
            self.assertIn("depth", node)
            self.assertIn("generation_depth", node)
            self.assertEqual(
                node["depth"],
                node["generation_depth"],
                "the frontend's loadLineage() reads n.depth -- it must equal generation_depth",
            )
            self.assertIn("owner_id", node)
            self.assertIn("title", node)
            self.assertIn("status", node)

    def test_get_lineage_entity_not_found_returns_error_regression(self):
        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_lineage("nonexistent-entity-id-xyz-does-not-exist")
        self.assertEqual(captured["status"], 404)
        self.assertIn("error", captured["data"])

    def test_get_entities_and_entity_detail_parent_ids_populated_correctly(self):
        a, b, c1 = self._consolidate_two(
            "Viewer ParentIds A", "Viewer ParentIds B", "Viewer ParentIds C1", "parent-ids-pin"
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_entities({})
        entities_by_id = {e["id"]: e for e in captured["data"]["entities"]}
        self.assertIn(c1, entities_by_id, "consolidated entity must appear in get_entities results")
        self.assertEqual(
            set(entities_by_id[c1]["parent_ids"]),
            {a, b},
            "get_entities must correctly populate parent_ids via row['parent_ids'] key access",
        )

        captured2 = self._capture_json(handler)
        handler.get_entity_detail(c1)
        self.assertEqual(
            set(captured2["data"]["parent_ids"]),
            {a, b},
            "get_entity_detail must correctly populate parent_ids via row['parent_ids'] key access",
        )

    def test_get_entities_is_core_filter(self):
        core_id = self._memory_id(
            store_memory(
                content="Core architectural fact memory content",
                title="Core Architecture Fact",
                owner_id="viewer_tester",
                is_core=True,
                core_reason="Test fixture core reason for the viewer is_core filter regression test.",
                core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
                db_connection=self.conn,
            )
        )

        non_core_id = self._memory_id(
            store_memory(
                content="Non-core ephemeral detail content",
                title="Non Core Detail",
                owner_id="viewer_tester",
                is_core=False,
                db_connection=self.conn,
            )
        )

        handler = self._handler()

        # Omitted and blank filters preserve the unfiltered entity list.
        captured_all = self._capture_json(handler)
        handler.get_entities({})
        all_ids = {e["id"] for e in captured_all["data"]["entities"]}
        self.assertIn(core_id, all_ids)
        self.assertIn(non_core_id, all_ids)

        captured_blank = self._capture_json(handler)
        handler.get_entities({"is_core": ["   "]})
        blank_ids = {e["id"] for e in captured_blank["data"]["entities"]}
        self.assertEqual(blank_ids, all_ids)

        # Filter is_core=true
        captured_true = self._capture_json(handler)
        handler.get_entities({"is_core": ["true"]})
        entities_true = captured_true["data"]["entities"]
        true_ids = [e["id"] for e in entities_true]
        self.assertIn(core_id, true_ids)
        self.assertNotIn(non_core_id, true_ids)
        for e in entities_true:
            self.assertTrue(e["is_core"])

        # Filter is_core=false
        captured_false = self._capture_json(handler)
        handler.get_entities({"is_core": ["false"]})
        entities_false = captured_false["data"]["entities"]
        false_ids = [e["id"] for e in entities_false]
        self.assertIn(non_core_id, false_ids)
        self.assertNotIn(core_id, false_ids)
        for e in entities_false:
            self.assertFalse(e["is_core"])

        # Accepted aliases are equivalent, including case-insensitive input.
        captured_alias = self._capture_json(handler)
        handler.get_entities({"is_core": ["YeS"]})
        alias_ids = {e["id"] for e in captured_alias["data"]["entities"]}
        self.assertEqual(alias_ids, set(true_ids))

        # A supplied value outside the documented aliases is a client error.
        captured_invalid = self._capture_json(handler)
        handler.get_entities({"is_core": ["sometimes"]})
        self.assertEqual(captured_invalid["status"], 400)
        self.assertIn("error", captured_invalid["data"])

    def test_get_entities_is_core_combines_with_other_filters_and_pagination(self):
        def create_entity(title, is_core, memory_type):
            core_kwargs = {}
            if is_core:
                core_kwargs = {
                    "core_reason": "Test fixture core reason for the viewer entities is_core filter test.",
                    "core_exit_condition": "Test fixture exit condition: this regression test tears down its temp DB.",
                }
            result = store_memory(
                content=f"Unique content for {title}",
                title=title,
                owner_id="viewer_tester",
                is_core=is_core,
                memory_type=memory_type,
                db_connection=self.conn,
                **core_kwargs,
            )
            return self._memory_id(result)

        matching_ids = {
            create_entity("Core Decision One", True, "decision"),
            create_entity("Core Decision Two", True, "decision"),
        }
        create_entity("Non Core Decision", False, "decision")
        create_entity("Core Fact", True, "fact")

        handler = self._handler()
        query = {
            "is_core": ["1"],
            "status": ["raw"],
            "memory_type": ["decision"],
            "limit": ["1"],
        }

        captured_page_1 = self._capture_json(handler)
        handler.get_entities({**query, "page": ["1"]})
        page_1 = captured_page_1["data"]
        self.assertEqual(captured_page_1["status"], 200)
        self.assertEqual(page_1["total_count"], 2)
        self.assertEqual(page_1["total_pages"], 2)
        self.assertEqual(page_1["pagination"]["total"], 2)
        self.assertEqual(page_1["pagination"]["total_pages"], 2)
        self.assertEqual(len(page_1["entities"]), 1)
        self.assertTrue(page_1["entities"][0]["is_core"])
        self.assertEqual(page_1["entities"][0]["status"], "raw")
        self.assertEqual(page_1["entities"][0]["memory_type"], "decision")

        captured_page_2 = self._capture_json(handler)
        handler.get_entities({**query, "page": ["2"]})
        page_2 = captured_page_2["data"]
        self.assertEqual(page_2["total_count"], 2)
        self.assertEqual(page_2["pagination"]["total"], 2)
        self.assertEqual(len(page_2["entities"]), 1)
        self.assertEqual(
            {page_1["entities"][0]["id"], page_2["entities"][0]["id"]},
            matching_ids,
        )

    def test_get_search_is_core_filter(self):
        store_memory(
            content="Unique keyword quantum ground state core memory",
            title="Quantum Ground State",
            owner_id="viewer_tester",
            is_core=True,
            core_reason="Test fixture core reason for the viewer search is_core filter regression test.",
            core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
            db_connection=self.conn,
        )

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_search({"q": ["quantum"], "is_core": ["true"]})
        self.assertEqual(captured["status"], 200)
        results = captured["data"]["results"]
        self.assertIsInstance(results, list)
        for r in results:
            self.assertTrue(r.get("is_core"))


class TestViewerScatterplot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        # Real production schema (init_db creates entity_embeddings as an actual vec0
        # virtual table) -- required to exercise/catch a regression in the viewer's
        # own per-request connection needing the sqlite_vec extension loaded before it
        # can query that table, same as the consolidate_vector_clusters regression.
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _handler(self):
        return SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())

    def _capture_json(self, handler):
        captured = {}

        def fake_send_json(data, status=200):
            captured["data"] = data
            captured["status"] = status

        handler.send_json = fake_send_json
        return captured

    def _insert_ready_entity(self, entity_id, title):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, "
            "title, full_content, status, embedding_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', 'ready')",
            (entity_id, now, now, now, "viewer_tester", title, f"Content for {title}"),
        )
        vec = np.ones(384, dtype=np.float32) * (hash(entity_id) % 100 / 100.0)
        self.conn.execute(
            "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (entity_id, vec.tobytes()),
        )

    def test_get_scatterplot_with_ready_embeddings(self):
        self._insert_ready_entity("scatter-e1", "Scatterplot Entity One")
        self._insert_ready_entity("scatter-e2", "Scatterplot Entity Two")
        self.conn.commit()

        handler = self._handler()
        captured = self._capture_json(handler)
        handler.get_scatterplot()

        self.assertNotIn("error", captured["data"])
        points = captured["data"]["points"]
        self.assertEqual(len(points), 2)
        point_ids = {p["id"] for p in points}
        self.assertEqual(point_ids, {"scatter-e1", "scatter-e2"})
        for p in points:
            self.assertIn("x", p)
            self.assertIn("y", p)


if __name__ == "__main__":
    unittest.main()
