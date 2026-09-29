import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from saltmdb.db.agent_sessions import close_session, record_session
from saltmdb.db.schema import init_db
from saltmdb.utils.text import compute_content_hash


class TestTraceService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "trace-service.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _start(self, turn_id="turn-a", agent_id="owner-a", agent_session_id="agent-session-a"):
        from saltmdb.domain.services import trace_service

        result = trace_service.capture_trace_start(
            agent_session_id=agent_session_id,
            agent_id=agent_id,
            harness="claude_code",
            harness_session_id="claude-session-a",
            harness_turn_id=turn_id,
            user_prompt=f"Prompt for {turn_id}",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def _entity(self, entity_id="entity-a", content="Entity content"):
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO entities
                (id, created_at, updated_at, last_accessed_at, agent_id, scope,
                 title, full_content, content_hash)
            VALUES (?, ?, ?, ?, ?, 'shared', ?, ?, ?)
            """,
            (
                entity_id,
                now,
                now,
                now,
                "owner-a",
                entity_id,
                content,
                compute_content_hash(content),
            ),
        )
        return entity_id

    def test_duplicate_trace_start_is_idempotent(self):
        from saltmdb.domain.services import trace_service

        first = trace_service.capture_trace_start(
            agent_session_id="agent-session-a",
            agent_id="owner-a",
            harness="claude_code",
            harness_session_id="claude-session-a",
            harness_turn_id="prompt-a",
            user_prompt="Remember this prompt exactly.",
            db_connection=self.conn,
        )
        second = trace_service.capture_trace_start(
            agent_session_id="agent-session-a",
            agent_id="owner-a",
            harness="claude_code",
            harness_session_id="claude-session-a",
            harness_turn_id="prompt-a",
            user_prompt="A duplicate delivery.",
            db_connection=self.conn,
        )

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(first["data"]["id"], second["data"]["id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversation_traces").fetchone()[0], 1
        )

    def test_duplicate_trace_memory_link_is_idempotent(self):
        from saltmdb.domain.services import trace_service

        trace_id = self._start()
        self._entity()
        first = trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "turn-a",
            "entity-a",
            "store_memory",
            db_connection=self.conn,
        )
        second = trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "turn-a",
            "entity-a",
            "store_memory",
            db_connection=self.conn,
        )

        self.assertEqual(first["data"]["trace_id"], trace_id)
        self.assertTrue(first["data"]["linked"])
        self.assertFalse(second["data"]["linked"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM trace_memory_links WHERE trace_id = ?", (trace_id,)
            ).fetchone()[0],
            1,
        )

    def test_different_entity_hashes_create_distinct_links(self):
        from saltmdb.domain.services import trace_service

        trace_id = self._start()
        self._entity()
        trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "turn-a",
            "entity-a",
            "store_memory",
            db_connection=self.conn,
        )
        updated = datetime.now(UTC).isoformat()
        content = "Updated entity content"
        self.conn.execute(
            "UPDATE entities SET content_hash = ?, full_content = ?, updated_at = ? WHERE id = ?",
            (compute_content_hash(content), content, updated, "entity-a"),
        )
        result = trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "turn-a",
            "entity-a",
            "store_memory",
            db_connection=self.conn,
        )

        self.assertTrue(result["data"]["linked"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM trace_memory_links WHERE trace_id = ?", (trace_id,)
            ).fetchone()[0],
            2,
        )

    def test_real_store_insert_and_update_classification(self):
        from saltmdb.domain.services import memory_service, trace_service

        first = memory_service.store_memory(
            content=(
                "The trace provenance test stores a durable memory through the real write service "
                "before it verifies insert and update operation classification."
            ),
            title="Trace provenance insert",
            agent_id="owner-a",
            db_connection=self.conn,
        )
        self.assertEqual(first["status"], "ok")
        entity_id = first["data"]["id"]
        trace_id = self._start(turn_id="real-store")
        inserted = trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "real-store",
            entity_id,
            "store_memory",
            db_connection=self.conn,
        )

        updated = memory_service.store_memory(
            content=(
                "The trace provenance test stores a durable memory through the real write service "
                "before it verifies insert and update operation classification."
            ),
            title="Trace provenance insert",
            agent_id="owner-a",
            entity_id=entity_id,
            weight=2,
            db_connection=self.conn,
        )
        self.assertEqual(updated["status"], "ok", updated)
        changed = trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "real-store",
            entity_id,
            "store_memory",
            db_connection=self.conn,
        )

        self.assertEqual(inserted["data"]["write_operation"], "store_memory_new")
        self.assertTrue(inserted["data"]["linked"])
        self.assertEqual(changed["data"]["write_operation"], "store_memory_update")
        self.assertFalse(changed["data"]["linked"])
        operations = self.conn.execute(
            "SELECT write_operation FROM trace_memory_links WHERE trace_id = ? "
            "AND entity_id = ? ORDER BY created_at, id",
            (trace_id, entity_id),
        ).fetchall()
        self.assertEqual([row[0] for row in operations], ["store_memory_new"])

    def test_all_replacement_operation_literals_are_accepted(self):
        from saltmdb.domain.services import trace_service

        self._start(turn_id="operations")
        for index, operation in enumerate(
            ("revise_memory", "supersede_memory", "consolidate_memories")
        ):
            entity_id = self._entity(f"entity-{index}", f"Operation {operation} content")
            result = trace_service.capture_trace_memory_link(
                "agent-session-a",
                "owner-a",
                "operations",
                entity_id,
                operation,
                db_connection=self.conn,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data"]["write_operation"], operation)

    def test_complete_round_trips_large_text_and_terminal_guard(self):
        from saltmdb.domain.services import trace_service

        user_prompt = "user prompt " * 50_000
        started = trace_service.capture_trace_start(
            "agent-session-a",
            "owner-a",
            "claude_code",
            "claude-session-a",
            "turn-a",
            user_prompt,
            db_connection=self.conn,
        )
        message = "assistant history " * 30_000
        completed = trace_service.capture_trace_complete(
            "agent-session-a", "owner-a", "turn-a", message, db_connection=self.conn
        )
        repeated = trace_service.capture_trace_complete(
            "agent-session-a", "owner-a", "turn-a", "replacement", db_connection=self.conn
        )
        fetched = trace_service.get_trace(completed["data"]["trace_id"], db_connection=self.conn)

        self.assertEqual(started["data"]["status"], "pending")
        self.assertEqual(completed["data"]["status"], "completed")
        self.assertEqual(repeated["data"]["status"], "completed")
        self.assertEqual(repeated["warnings"][0]["code"], "ALREADY_TERMINAL")
        self.assertEqual(fetched["data"]["user_prompt"], user_prompt)
        self.assertEqual(fetched["data"]["final_assistant_message"], message)
        self.assertEqual(fetched["data"]["status"], "completed")

    def test_complete_preserves_incomplete_terminal_trace(self):
        from saltmdb.domain.services import trace_service

        trace_id = self._start(turn_id="incomplete")
        self.conn.execute(
            """
            UPDATE conversation_traces
            SET status = 'incomplete', final_assistant_message = ?, updated_at = ?
            WHERE id = ?
            """,
            ("original incomplete text", datetime.now(UTC).isoformat(), trace_id),
        )
        result = trace_service.capture_trace_complete(
            "agent-session-a", "owner-a", "incomplete", "replacement", db_connection=self.conn
        )
        fetched = trace_service.get_trace(trace_id, db_connection=self.conn)

        self.assertEqual(result["data"]["status"], "incomplete")
        self.assertEqual(result["warnings"][0]["code"], "ALREADY_TERMINAL")
        self.assertEqual(fetched["data"]["final_assistant_message"], "original incomplete text")

    def test_abandonment_sweep_handles_ended_and_missing_sessions(self):
        from saltmdb.domain.services import trace_service

        ended_trace = self._start(turn_id="ended", agent_session_id="ended-session")
        record_session(self.conn, "ended-session", "/tmp", datetime.now(UTC).isoformat(), "owner-a")
        close_session(self.conn, "ended-session", datetime.now(UTC).isoformat())
        ended_result = trace_service.search_traces(db_connection=self.conn)
        self.assertEqual(
            next(
                item for item in ended_result["data"]["results"] if item["trace_id"] == ended_trace
            )["status"],
            "incomplete",
        )

        old_trace = self._start(turn_id="old", agent_session_id="old-session")
        old_time = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        self.conn.execute(
            "UPDATE conversation_traces SET created_at = ?, updated_at = ? WHERE id = ?",
            (old_time, old_time, old_trace),
        )
        unresolved_result = trace_service.get_trace(old_trace, db_connection=self.conn)
        self.assertEqual(unresolved_result["data"]["status"], "incomplete")

    def test_timeout_fallback_marks_unresolved_session_row_incomplete(self):
        from saltmdb.domain.services import trace_service

        trace_id = self._start(turn_id="ambiguous", agent_session_id="ambiguous-session")
        record_session(
            self.conn, "ambiguous-session", "/tmp", datetime.now(UTC).isoformat(), "owner-a"
        )
        old_time = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        self.conn.execute(
            "UPDATE conversation_traces SET created_at = ?, updated_at = ? WHERE id = ?",
            (old_time, old_time, trace_id),
        )

        result = trace_service.get_trace(trace_id, db_connection=self.conn)

        self.assertEqual(result["data"]["status"], "incomplete")

    def test_search_returns_bounded_preview_and_inert_keyword_warning(self):
        from saltmdb.domain.services import trace_service

        trace_id = self._start(turn_id="search")
        self._entity()
        trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "search",
            "entity-a",
            "store_memory",
            db_connection=self.conn,
        )
        trace_service.capture_trace_complete(
            "agent-session-a", "owner-a", "search", "Assistant response", db_connection=self.conn
        )

        result = trace_service.search_traces(
            query_keywords="not semantic yet", limit=1, db_connection=self.conn
        )
        item = result["data"]["results"][0]

        self.assertEqual(item["trace_id"], trace_id)
        self.assertEqual(item["linked_entity_ids"], ["entity-a"])
        self.assertIn("user_prompt_snippet", item)
        self.assertIn("final_assistant_message_snippet", item)
        self.assertNotIn("user_prompt", item)
        self.assertEqual(result["warnings"][0]["code"], "TRACE_SEARCH_NOT_YET_SEMANTIC")

    def _start_text(self, text, turn_id="turn-a"):
        from saltmdb.domain.services import trace_service

        return trace_service.capture_trace_start(
            agent_session_id="agent-session-a",
            agent_id="owner-a",
            harness="claude_code",
            harness_session_id="claude-session-a",
            harness_turn_id=turn_id,
            user_prompt=text,
            db_connection=self.conn,
        )

    def test_same_turn_different_text_is_appended_as_a_mid_turn_message_in_order(self):
        from saltmdb.domain.services import trace_service

        first = self._start_text("Prompt for turn-a")
        self.assertIs(first["data"]["message_appended"], False)
        trace_id = first["data"]["id"]

        second = self._start_text("steer one")
        third = self._start_text("steer two")
        self.assertEqual(second["data"]["id"], trace_id)
        self.assertIs(second["data"]["message_appended"], True)
        self.assertIs(third["data"]["message_appended"], True)

        data = trace_service.get_trace(trace_id, db_connection=self.conn)["data"]
        self.assertEqual(data["user_prompt"], "Prompt for turn-a")
        self.assertEqual(
            [(m["seq"], m["message"]) for m in data["mid_turn_messages"]],
            [(1, "steer one"), (2, "steer two")],
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversation_traces").fetchone()[0], 1
        )

    def test_hook_retries_are_idempotent_for_opening_prompt_and_mid_turn_messages(self):
        from saltmdb.domain.services import trace_service

        trace_id = self._start_text("Prompt for turn-a")["data"]["id"]
        self.assertIs(self._start_text("Prompt for turn-a")["data"]["message_appended"], False)
        self.assertIs(self._start_text("steer")["data"]["message_appended"], True)
        self.assertIs(self._start_text("steer")["data"]["message_appended"], False)

        data = trace_service.get_trace(trace_id, db_connection=self.conn)["data"]
        self.assertEqual([m["message"] for m in data["mid_turn_messages"]], ["steer"])

    def test_mid_turn_messages_do_not_leak_across_turns(self):
        from saltmdb.domain.services import trace_service

        a = self._start_text("Prompt A", "turn-a")["data"]["id"]
        b = self._start_text("Prompt B", "turn-b")["data"]["id"]
        self._start_text("steer for A", "turn-a")
        self.assertEqual(
            trace_service.get_trace(b, db_connection=self.conn)["data"]["mid_turn_messages"], []
        )
        self.assertEqual(
            len(trace_service.get_trace(a, db_connection=self.conn)["data"]["mid_turn_messages"]), 1
        )

    @staticmethod
    def _notification(status, summary):
        return (
            "<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
            f"<output-file>/tmp/b1.output</output-file>\n<status>{status}</status>\n"
            f"<summary>{summary}</summary>\n</task-notification>"
        )

    def _stored_prompt(self, text, turn_id="turn-a", harness="claude_code"):
        from saltmdb.domain.services import trace_service

        result = trace_service.capture_trace_start(
            agent_session_id="agent-session-a",
            agent_id="owner-a",
            harness=harness,
            harness_session_id="claude-session-a",
            harness_turn_id=turn_id,
            user_prompt=text,
            db_connection=self.conn,
        )
        return trace_service.get_trace(result["data"]["id"], db_connection=self.conn)["data"][
            "user_prompt"
        ]

    def test_task_notification_is_stored_as_a_labelled_description(self):
        stored = self._stored_prompt(
            self._notification(
                "completed", 'Background command "Run the tests" completed (exit code 0)'
            )
        )
        self.assertEqual(stored, "[background task completed] Run the tests")

    def test_failed_task_notification_keeps_status_and_nonzero_exit_code(self):
        stored = self._stored_prompt(
            self._notification(
                "failed", 'Background command "Run the tests" failed with exit code 3'
            )
        )
        self.assertEqual(stored, "[background task failed] Run the tests (exit 3)")

    def test_description_may_contain_quotes_and_markup_characters(self):
        stored = self._stored_prompt(
            self._notification(
                "completed", 'Background command "echo "a" && cat <f>" completed (exit code 0)'
            )
        )
        self.assertEqual(stored, '[background task completed] echo "a" && cat <f>')

    def test_unrecognised_status_and_summary_shape_pass_through(self):
        stored = self._stored_prompt(self._notification("killed", "Subagent finished its work"))
        self.assertEqual(stored, "[background task killed] Subagent finished its work")

    def test_notification_without_summary_falls_back_to_the_raw_text_under_the_label(self):
        raw = "<task-notification>\n<task-id>b1</task-id>\n</task-notification>"
        self.assertEqual(self._stored_prompt(raw), f"[background task] {raw}")

    def test_notification_transform_is_claude_code_only_and_needs_the_leading_tag(self):
        raw = self._notification("completed", 'Background command "x" completed (exit code 0)')
        self.assertEqual(self._stored_prompt(raw, harness="codex"), raw)
        mentioned = f"please explain this: {raw}"
        self.assertEqual(self._stored_prompt(mentioned, turn_id="turn-b"), mentioned)

    def test_notification_hook_retries_stay_idempotent(self):
        raw = self._notification("completed", 'Background command "x" completed (exit code 0)')
        first = self._start_text(raw)
        again = self._start_text(raw)
        self.assertEqual(first["data"]["id"], again["data"]["id"])
        self.assertIs(again["data"]["message_appended"], False)

    def test_reads_are_cross_agent_but_writes_stay_bound_to_the_writing_agent(self):
        from saltmdb.domain.services import trace_service

        # agent_id is attribution, not access control: any caller reads any agent's trace.
        trace_id = self._start()
        self._entity()
        fetched = trace_service.get_trace(trace_id, db_connection=self.conn)
        self.assertEqual(fetched["status"], "ok")
        self.assertEqual(fetched["data"]["agent_id"], "owner-a")
        results = trace_service.search_traces(db_connection=self.conn)["data"]["results"]
        self.assertEqual([r["trace_id"] for r in results], [trace_id])
        linked = trace_service.capture_trace_memory_link(
            "agent-session-a",
            "owner-a",
            "turn-a",
            "entity-a",
            "store_memory",
            db_connection=self.conn,
        )
        self.assertEqual(linked["status"], "ok")
        provenance = trace_service.entity_trace_provenance(self.conn, "entity-a", limit=5)
        self.assertEqual([row["trace_id"] for row in provenance], [trace_id])
        # ...but a different agent still cannot link to or complete this agent's trace.
        self.assertEqual(
            trace_service.capture_trace_memory_link(
                "agent-session-a",
                "owner-b",
                "turn-a",
                "entity-a",
                "store_memory",
                db_connection=self.conn,
            )["errors"][0]["code"],
            "UNKNOWN_TRACE",
        )
        self.assertEqual(
            trace_service.capture_trace_complete(
                "agent-session-a", "owner-b", "turn-a", "not accepted", db_connection=self.conn
            )["warnings"][0]["code"],
            "UNKNOWN_TRACE",
        )


if __name__ == "__main__":
    unittest.main()
