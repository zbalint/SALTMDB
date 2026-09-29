import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerFeed(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "feed.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get(self, query=None, daemon_state=None):
        server = DummyServer()
        server.daemon_state = daemon_state
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), server)
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        handler.get_feed(query or {})
        return captured["status"], captured["data"]

    def _memory(self, entity_id, created_at, session=None, touched=None, status="raw"):
        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, title,
               full_content, status, agent_session_id, last_touched_session_id)
               VALUES (?, ?, ?, ?, 'claude', ?, ?, ?, ?, ?)""",
            (
                entity_id,
                created_at,
                created_at,
                created_at,
                f"title {entity_id}",
                f"body {entity_id}",
                status,
                session,
                touched,
            ),
        )
        self.conn.commit()

    def _event(self, event_id, timestamp, session=None):
        self.conn.execute(
            "INSERT INTO events (id, timestamp, agent_id, type, content, agent_session_id) "
            "VALUES (?, ?, 'claude', 'decision', ?, ?)",
            (event_id, timestamp, f"content {event_id}", session),
        )
        self.conn.commit()

    def _trace(self, trace_id, updated_at, session="s", status="completed", created_at=None):
        self.conn.execute(
            """INSERT INTO conversation_traces
               (id, agent_session_id, owner_id, harness, harness_session_id, harness_turn_id,
                status, user_prompt, user_prompt_hash, created_at, updated_at)
               VALUES (?, ?, 'claude', 'claude_code', 'hs', ?, ?, ?, 'h', ?, ?)""",
            (
                trace_id,
                session,
                trace_id,
                status,
                f"prompt {trace_id}",
                created_at or updated_at,
                updated_at,
            ),
        )
        self.conn.commit()

    def test_feed_merges_memories_events_and_traces_newest_first(self):
        self._memory("m1", "2026-09-29T10:00:00+00:00", session="s1")
        self._event("e1", "2026-09-29T10:00:03+00:00", session="s1")
        self._trace("t1", "2026-09-29T10:00:05+00:00", session="s2")

        status, data = self._get()

        self.assertEqual(status, 200)
        self.assertEqual(
            [(item["kind"], item["id"], item["session_id"]) for item in data["items"]],
            [("trace", "t1", "s2"), ("event", "e1", "s1"), ("memory", "m1", "s1")],
        )
        memory = data["items"][2]
        self.assertEqual(memory["title"], "title m1")
        self.assertEqual(memory["preview"], "body m1")
        self.assertEqual(memory["timestamp"], "2026-09-29T10:00:00+00:00")

    def test_since_cursor_returns_only_newer_items_without_repeats_or_gaps(self):
        self._memory("m1", "2026-09-29T10:00:00+00:00")
        self._event("e1", "2026-09-29T10:00:01+00:00")
        _, first = self._get()
        self.assertEqual(first["cursor"], "2026-09-29T10:00:01+00:00|event|e1")

        _, idle = self._get({"since": [first["cursor"]]})
        self.assertEqual(idle["items"], [])
        self.assertFalse(idle["has_more"])
        self.assertEqual(idle["cursor"], first["cursor"])

        # Same timestamp as the cursor but sorts after it (kind 'trace' > 'event'), plus a
        # strictly newer row: both are new, the cursor row itself is not.
        self._trace("t1", "2026-09-29T10:00:01+00:00")
        self._event("e2", "2026-09-29T10:00:02+00:00")
        _, second = self._get({"since": [first["cursor"]]})
        self.assertEqual([item["id"] for item in second["items"]], ["e2", "t1"])
        self.assertEqual(second["cursor"], "2026-09-29T10:00:02+00:00|event|e2")

    def test_catch_up_larger_than_limit_continues_in_order_without_skipping(self):
        for index in range(5):
            self._event(f"e{index}", f"2026-09-29T10:00:0{index}+00:00")
        cursor = "2026-09-29T09:00:00+00:00|event|start"

        seen = []
        for _ in range(3):
            _, page = self._get({"since": [cursor], "limit": ["2"]})
            seen.extend(item["id"] for item in reversed(page["items"]))
            cursor = page["cursor"]
            if not page["has_more"]:
                break

        self.assertEqual(seen, ["e0", "e1", "e2", "e3", "e4"])

    def test_malformed_cursor_is_rejected(self):
        status, data = self._get({"since": ["not-a-cursor"]})
        self.assertEqual(status, 400)
        self.assertIn("since", data["error"])

    def test_session_filter_matches_created_or_touched_memories_events_and_traces(self):
        self._memory("m-created", "2026-09-29T10:00:00+00:00", session="s1")
        self._memory("m-touched", "2026-09-29T10:00:01+00:00", session="s2", touched="s1")
        self._memory("m-other", "2026-09-29T10:00:02+00:00", session="s2")
        self._event("e-mine", "2026-09-29T10:00:03+00:00", session="s1")
        self._event("e-other", "2026-09-29T10:00:04+00:00", session="s2")
        self._event("e-none", "2026-09-29T10:00:05+00:00")
        self._trace("t-mine", "2026-09-29T10:00:06+00:00", session="s1")
        self._trace("t-other", "2026-09-29T10:00:07+00:00", session="s2")

        _, data = self._get({"session": ["s1"]})

        self.assertEqual(
            [item["id"] for item in data["items"]], ["t-mine", "e-mine", "m-touched", "m-created"]
        )

    def test_active_only_keeps_items_of_daemon_active_sessions(self):
        self._memory("m-active", "2026-09-29T10:00:00+00:00", session="s1")
        self._memory("m-touched", "2026-09-29T10:00:01+00:00", session="old", touched="s1")
        self._memory("m-old", "2026-09-29T10:00:02+00:00", session="old")
        self._event("e-active", "2026-09-29T10:00:03+00:00", session="s1")
        self._event("e-none", "2026-09-29T10:00:04+00:00")

        class LiveState:
            @staticmethod
            def viewer_snapshot():
                return {"active_agent_session_ids": ["s1"]}

        _, data = self._get({"active_only": ["1"]}, daemon_state=LiveState())

        self.assertTrue(data["liveness_known"])
        self.assertEqual(
            [item["id"] for item in data["items"]], ["e-active", "m-touched", "m-active"]
        )

    def test_active_only_with_no_active_sessions_returns_nothing(self):
        self._event("e1", "2026-09-29T10:00:00+00:00", session="s1")

        class IdleState:
            @staticmethod
            def viewer_snapshot():
                return {"active_agent_session_ids": []}

        _, data = self._get({"active_only": ["1"]}, daemon_state=IdleState())

        self.assertTrue(data["liveness_known"])
        self.assertEqual(data["items"], [])

    def test_active_only_without_daemon_liveness_reports_unknown_and_does_not_filter(self):
        self._event("e1", "2026-09-29T10:00:00+00:00", session="s1")

        _, data = self._get({"active_only": ["1"]}, daemon_state=None)

        self.assertFalse(data["liveness_known"])
        self.assertEqual([item["id"] for item in data["items"]], ["e1"])

    def test_completing_trace_resurfaces_as_the_same_item_with_new_status(self):
        self._trace(
            "t1",
            "2026-09-29T10:00:00+00:00",
            status="pending",
        )
        _, first = self._get()
        self.assertEqual([(i["id"], i["status"]) for i in first["items"]], [("t1", "pending")])

        self.conn.execute(
            "UPDATE conversation_traces SET status='completed', updated_at=? WHERE id='t1'",
            ("2026-09-29T10:05:00+00:00",),
        )
        self.conn.commit()
        _, second = self._get({"since": [first["cursor"]]})

        self.assertEqual([(i["id"], i["status"]) for i in second["items"]], [("t1", "completed")])

    def test_archived_history_rows_are_not_listed_as_memories(self):
        self._memory("m-live", "2026-09-29T10:00:00+00:00")
        self._memory("m-live_h_abcd", "2026-09-29T10:00:00+00:00", status="archived")

        _, data = self._get()

        self.assertEqual([item["id"] for item in data["items"]], ["m-live"])

    def test_previews_are_bounded_and_limit_is_validated(self):
        self._event("e1", "2026-09-29T10:00:00+00:00")
        self.conn.execute("UPDATE events SET content = ?", ("x" * 500,))
        self.conn.commit()

        _, data = self._get()
        self.assertEqual(len(data["items"][0]["preview"]), 200)

        for bad in ("0", "101", "abc"):
            with self.subTest(limit=bad):
                status, _ = self._get({"limit": [bad]})
                self.assertEqual(status, 400)

    def test_feed_is_routed_at_api_feed(self):
        self._event("e1", "2026-09-29T10:00:00+00:00", session="s1")
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        handler.path = "/api/feed?session=s1&limit=5"

        handler.do_GET()

        self.assertEqual(captured["status"], 200)
        self.assertEqual([item["id"] for item in captured["data"]["items"]], ["e1"])


if __name__ == "__main__":
    unittest.main()
