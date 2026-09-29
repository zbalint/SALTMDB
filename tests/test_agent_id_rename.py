"""Behavioural contract for the owner_id -> agent_id rename (docs/SPEC-AGENT-ID-RENAME.md).

This file is allowed to mention the old names: it asserts how they are handled.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.config import get_agent_id
from saltmdb.daemon import protocol
from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.daemon.server import _DaemonState
from saltmdb.db import agent_sessions
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.mcp import tools as mcp_tools
from saltmdb.mcp.identity import SESSION_IDENTITY

AGENT_ID_TABLES = ("entities", "_agent_sessions", "conversation_traces", "tool_call_telemetry")


def _env(**values):
    """Replace the identity env vars for one test; unset names stay unset."""
    cleaned = {
        k: v for k, v in os.environ.items() if k not in ("SALTMDB_AGENT_ID", "SALTMDB_OWNER_ID")
    }
    cleaned.update(values)
    return patch.dict(os.environ, cleaned, clear=True)


class AgentIdConfigTests(unittest.TestCase):
    def test_agent_id_env_var_is_used(self):
        with _env(SALTMDB_AGENT_ID="agent_qa"):
            self.assertEqual(get_agent_id(), "agent_qa")

    def test_owner_id_env_var_is_a_deprecated_fallback(self):
        with (
            _env(SALTMDB_OWNER_ID="agent_qa"),
            self.assertLogs("saltmdb.config", "WARNING") as logs,
        ):
            self.assertEqual(get_agent_id(), "agent_qa")
        self.assertEqual(len(logs.records), 1)
        self.assertIn("SALTMDB_OWNER_ID", logs.output[0])
        self.assertIn("SALTMDB_AGENT_ID", logs.output[0])

    def test_agent_id_wins_when_both_are_set_and_differ(self):
        with (
            _env(SALTMDB_AGENT_ID="codex", SALTMDB_OWNER_ID="claude"),
            self.assertLogs("saltmdb.config", "WARNING") as logs,
        ):
            self.assertEqual(get_agent_id(), "codex")
        self.assertEqual(len(logs.records), 1)
        self.assertIn("SALTMDB_AGENT_ID", logs.output[0])
        self.assertIn("SALTMDB_OWNER_ID", logs.output[0])

    def test_neither_variable_raises_naming_the_new_one(self):
        with _env(), self.assertRaises(RuntimeError) as ctx:
            get_agent_id()
        self.assertIn("SALTMDB_AGENT_ID", str(ctx.exception))

    def test_invalid_value_is_rejected_under_either_name(self):
        for name in ("SALTMDB_AGENT_ID", "SALTMDB_OWNER_ID"):
            with (
                self.subTest(name=name),
                _env(**{name: "Bad Name!"}),
                self.assertRaises(RuntimeError),
            ):
                get_agent_id()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn):
    return {
        row[0]: row[1]
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index'")
    }


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.db_path = os.path.join(self.temp_dir, "saltmdb.db")

    def _make_legacy_db(self):
        """A database in the pre-rename shape: init_db, then reverse the rename with raw SQL."""
        conn = init_db(self.db_path)
        for table in AGENT_ID_TABLES:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN agent_id TO owner_id")
        conn.execute("DROP INDEX idx_entities_agent_scope")
        conn.execute("DROP INDEX idx_traces_agent_status")
        conn.execute("CREATE INDEX idx_entities_owner_scope ON entities(owner_id, scope)")
        conn.execute(
            "CREATE INDEX idx_traces_owner_status ON conversation_traces(owner_id, status)"
        )
        return conn

    def test_fresh_database_uses_agent_id_everywhere(self):
        conn = init_db(self.db_path)
        for table in AGENT_ID_TABLES:
            with self.subTest(table=table):
                self.assertIn("agent_id", _columns(conn, table))
                self.assertNotIn("owner_id", _columns(conn, table))
        indexes = _indexes(conn)
        self.assertIn("idx_entities_agent_scope", indexes)
        self.assertIn("idx_traces_agent_status", indexes)
        self.assertNotIn("idx_entities_owner_scope", indexes)
        self.assertNotIn("idx_traces_owner_status", indexes)
        self.assertIn("agent_id", indexes["idx_entities_content_hash"])

    def test_legacy_database_is_migrated_in_place_and_rows_survive(self):
        conn = self._make_legacy_db()
        for entity_id, scope in (("private-1", "private"), ("shared-1", "shared")):
            conn.execute(
                "INSERT INTO entities (id, owner_id, scope, title, full_content, content_hash, "
                "created_at, updated_at, last_accessed_at) "
                "VALUES (?, 'claude', ?, 'visibilityprobe', 'visibilityprobe body', ?, '2026-01-01T00:00:00+00:00', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                (entity_id, scope, entity_id),
            )
        conn.commit()
        conn.close()

        migrated = init_db(self.db_path)
        for table in AGENT_ID_TABLES:
            with self.subTest(table=table):
                self.assertIn("agent_id", _columns(migrated, table))
                self.assertNotIn("owner_id", _columns(migrated, table))
        indexes = _indexes(migrated)
        self.assertNotIn("idx_entities_owner_scope", indexes)
        self.assertNotIn("idx_traces_owner_status", indexes)
        self.assertIn("idx_entities_agent_scope", indexes)
        self.assertIn("idx_traces_agent_status", indexes)
        rows = migrated.execute("SELECT id, agent_id, scope FROM entities ORDER BY id").fetchall()
        self.assertEqual(
            [tuple(r) for r in rows],
            [("private-1", "claude", "private"), ("shared-1", "claude", "shared")],
        )
        self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        def visible_to(agent_id):
            hits = memory_service.search_memory(
                agent_id=agent_id,
                query_keywords="visibilityprobe",
                db_connection=migrated,
                db_path=self.db_path,
            )
            return sorted(hit["id"] for hit in hits)

        self.assertEqual(visible_to("claude"), ["private-1", "shared-1"])
        self.assertEqual(visible_to("codex"), ["shared-1"])

    def test_migration_is_idempotent(self):
        self._make_legacy_db().close()
        first = init_db(self.db_path)
        schema_after_first = sorted(
            first.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
        )
        first.close()
        second = init_db(self.db_path)
        self.assertEqual(
            sorted(second.execute("SELECT type, name, sql FROM sqlite_master").fetchall()),
            schema_after_first,
        )

    def test_earliest_agent_sessions_shape_ends_with_agent_id(self):
        raw = sqlite3.connect(self.db_path)
        raw.execute(
            "CREATE TABLE _agent_sessions (session_id TEXT PRIMARY KEY, started_at DATETIME NOT NULL)"
        )
        raw.commit()
        raw.close()
        conn = init_db(self.db_path)
        self.assertIn("agent_id", _columns(conn, "_agent_sessions"))
        self.assertNotIn("owner_id", _columns(conn, "_agent_sessions"))

    def test_table_with_both_columns_is_refused(self):
        init_db(self.db_path).close()
        raw = sqlite3.connect(self.db_path)
        raw.execute("ALTER TABLE entities ADD COLUMN owner_id TEXT")
        raw.commit()
        raw.close()
        with self.assertRaises(RuntimeError) as ctx:
            init_db(self.db_path)
        self.assertIn("entities", str(ctx.exception))


class LegacyWireGuardTests(unittest.TestCase):
    """A stale adapter (started before the upgrade) must fail loudly, never lose its identity silently."""

    def _state(self):
        state = _DaemonState(
            db_path="/tmp/saltmdb_test_agent_id_guard.db", key="testkey", foreground=False
        )
        state.auth_token = "tok"
        return state

    def _assert_rename_error(self, response):
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], protocol.MALFORMED_REQUEST)
        self.assertIn("agent_id", response["error"]["message"])
        self.assertIn("restart", response["error"]["message"])

    def test_hello_carrying_owner_id_is_rejected_and_registers_no_session(self):
        state = self._state()
        response = state.handle_request(
            protocol.build_request("hello", {"owner_id": "agent_qa"}, token="tok"), session_id=3
        )
        self._assert_rename_error(response)
        self.assertNotIn(3, state._sessions)

    def test_tool_call_kwargs_carrying_owner_id_are_rejected(self):
        state = self._state()
        state.coordinator = object()  # reaches the guard; never used because the request is refused
        response = state.handle_request(
            protocol.build_request(
                "tool_call",
                {"tool": "search_memory", "kwargs": {"owner_id": "agent_qa"}},
                token="tok",
            ),
            session_id=3,
        )
        self._assert_rename_error(response)


class AdapterEnvelopeTests(unittest.TestCase):
    def setUp(self):
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_agent_id("test_agent")
        self.addCleanup(SESSION_IDENTITY.reset)

    def _forwarded(self, tool, kwargs):
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            mcp_tools.RpcBackend().call(tool, kwargs)
        return mock_call.call_args[0][2]

    def test_identity_tools_carry_the_adapter_agent_id_even_if_caller_supplied_another(self):
        for tool in ("log_event", "store_memory"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    self._forwarded(tool, {"agent_id": "claude"})["agent_id"], "test_agent"
                )

    def test_get_events_agent_id_is_a_read_filter_and_reaches_the_daemon_untouched(self):
        self.assertEqual(self._forwarded("get_events", {"agent_id": "codex"})["agent_id"], "codex")

    def test_non_injected_tool_has_a_stray_agent_id_removed(self):
        self.assertNotIn("agent_id", self._forwarded("search_tags", {"agent_id": "attacker"}))

    def test_bulk_item_stray_agent_id_is_stripped(self):
        items = [{"agent_id": "attacker", "source_id": "a", "target_id": "b"}]
        self.assertEqual(
            mcp_tools._strip_item_agent_id(items), [{"source_id": "a", "target_id": "b"}]
        )


class TelemetryAndSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path).close()
        self.coordinator = DbWriteCoordinator(self.db_path)
        self.coordinator.start()
        self.addCleanup(self.coordinator.shutdown, timeout=5)

    def _recorded_agent_id(self, tool):
        import time

        for _ in range(60):
            conn = sqlite3.connect(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT agent_id FROM tool_call_telemetry WHERE tool_name = ?", (tool,)
                ).fetchall()
            finally:
                conn.close()
            if rows:
                return rows[0][0]
            time.sleep(0.05)
        self.fail(f"no telemetry row recorded for {tool}")

    def test_identity_tool_call_records_the_callers_agent_id(self):
        dispatch_tool(
            "log_event",
            {
                "agent_id": "claude",
                "type": "event",
                "content": "agent_id rename telemetry probe",
                "error_code": None,
                "agent_session_id": None,
                "context_id": None,
            },
            self.coordinator,
        )
        self.assertEqual(self._recorded_agent_id("log_event"), "claude")

    def test_get_events_filter_is_not_recorded_as_the_caller(self):
        dispatch_tool("get_events", {"agent_id": "codex", "limit": 1}, self.coordinator)
        self.assertIsNone(self._recorded_agent_id("get_events"))

    def test_record_session_persists_agent_id(self):
        conn = init_db(self.db_path)
        agent_sessions.record_session(
            conn, "session-1", "/work", "2026-01-01T00:00:00+00:00", agent_id="agent_qa"
        )
        conn.commit()
        self.assertEqual(
            conn.execute(
                "SELECT agent_id FROM _agent_sessions WHERE session_id = 'session-1'"
            ).fetchone()[0],
            "agent_qa",
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
