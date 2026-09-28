import inspect
import os
import shutil
import tempfile
import unittest
from collections.abc import Callable
from typing import cast
from typing_extensions import override
from unittest.mock import patch

from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class _CaptureBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, tool_name: str, kwargs: dict[str, object]) -> dict[str, object]:
        self.calls.append((tool_name, kwargs))
        return {"status": "ok", "data": {}}


class TestTraceMcpTools(unittest.TestCase):
    backend: _CaptureBackend = _CaptureBackend()

    @override
    def setUp(self):
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("trace_test_agent")
        self.backend = _CaptureBackend()

    @override
    def tearDown(self):
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")

    def test_capture_signatures_do_not_expose_agent_session_id(self):
        for name in (
            "capture_trace_start",
            "capture_trace_memory_link",
            "capture_trace_complete",
        ):
            function = cast(Callable[..., object], getattr(tools, name))
            self.assertNotIn("agent_session_id", inspect.signature(function).parameters)

    def test_wrappers_bind_owner_and_never_accept_caller_owner(self):
        with patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend):
            tools.capture_trace_start("codex", "codex-session", "turn-1", "prompt")
            tools.capture_trace_memory_link("turn-1", "entity-1", "store_memory")
            tools.capture_trace_complete("turn-1", "assistant answer")
            tools.search_traces(agent_session_id="filter-session", limit=3)
            tools.get_trace("trace-1")

        calls = dict(self.backend.calls)
        self.assertEqual(calls["capture_trace_start"]["owner_id"], "trace_test_agent")
        self.assertEqual(calls["capture_trace_memory_link"]["owner_id"], "trace_test_agent")
        self.assertEqual(calls["capture_trace_complete"]["owner_id"], "trace_test_agent")
        self.assertEqual(calls["search_traces"]["owner_id"], "trace_test_agent")
        self.assertEqual(calls["search_traces"]["agent_session_id"], "filter-session")
        self.assertEqual(calls["get_trace"]["owner_id"], "trace_test_agent")
        for tool_name, kwargs in calls.items():
            if tool_name != "search_traces":
                self.assertNotIn("agent_session_id", kwargs)

    def test_get_memory_forwards_trace_provenance_opt_in(self):
        with patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend):
            tools.get_memory("entity-1", include_trace_provenance=True)

        kwargs = self.backend.calls[-1][1]
        self.assertEqual(kwargs["entity_id"], "entity-1")
        self.assertTrue(kwargs["include_trace_provenance"])


    def test_rpc_backend_mints_capture_session_identity(self):
        backend = tools.RpcBackend()
        rpc_call = cast(Callable[[str, dict[str, object]], object], backend.call)
        calls: list[dict[str, object]] = []

        def daemon_call(
            _path: str, _tool_name: str, kwargs: dict[str, object], **_transport: object
        ) -> dict[str, object]:
            calls.append(kwargs)
            return {"status": "ok"}

        with patch("saltmdb.mcp.tools.daemon_client.call", side_effect=daemon_call):
            for tool_name in (
                "capture_trace_start",
                "capture_trace_memory_link",
                "capture_trace_complete",
            ):
                _ = rpc_call(tool_name, {"owner_id": "caller-supplied", "value": "ignored"})
                kwargs = calls[-1]
                self.assertEqual(kwargs["owner_id"], "trace_test_agent")
                self.assertEqual(kwargs["agent_session_id"], SESSION_IDENTITY.agent_session_id)

    def test_bound_adapter_identity_reaches_trace_database_row(self):
        from saltmdb.daemon import dispatch
        from saltmdb.db.schema import init_db

        dispatch_table = cast(
            dict[str, Callable[..., object]], getattr(dispatch, "DISPATCH_TABLE")
        )

        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "trace-mcp.db")
        conn = init_db(db_path)

        def daemon_call(
            path: str, tool_name: str, kwargs: dict[str, object], **_transport: object
        ) -> dict[str, object]:
            result = dispatch_table[tool_name](**kwargs, db_path=path)
            if not isinstance(result, dict):
                raise TypeError(f"Dispatch returned non-dict result: {result!r}")
            return cast(dict[str, object], result)

        set_backend_for_test = cast(
            Callable[[object], object | None], getattr(tools, "_set_backend_for_test")
        )
        previous_backend = set_backend_for_test(tools.RpcBackend())
        try:
            with (
                patch.dict(os.environ, {"SALTMDB_TRACE_CAPTURE_ENABLED": "true"}),
                patch("saltmdb.config.get_db_path", return_value=db_path),
                patch("saltmdb.mcp.tools.daemon_client.call", side_effect=daemon_call),
            ):
                started = cast(
                    dict[str, object],
                    tools.capture_trace_start(
                        "codex", "codex-session", "turn-db", "database-bound identity"
                    ),
                )
                now = "2026-09-01T10:00:00+00:00"
                _ = conn.execute(
                    """
                    INSERT INTO entities
                        (id, created_at, updated_at, last_accessed_at, owner_id, scope,
                         title, full_content, content_hash)
                    VALUES (?, ?, ?, ?, ?, 'shared', ?, ?, ?)
                    """,
                    (
                        "entity-db",
                        now,
                        now,
                        now,
                        "trace_test_agent",
                        "DB entity",
                        "Database-bound entity",
                        "db-content-hash",
                    ),
                )
                linked = cast(
                    dict[str, object],
                    tools.capture_trace_memory_link("turn-db", "entity-db", "store_memory"),
                )
                completed = cast(
                    dict[str, object],
                    tools.capture_trace_complete("turn-db", "database answer"),
                )
            self.assertEqual(started["status"], "ok")
            self.assertEqual(linked["status"], "ok")
            self.assertEqual(completed["status"], "ok")
            row = cast(
                tuple[str, str, str],
                conn.execute(
                    """SELECT agent_session_id, owner_id, status
                    FROM conversation_traces WHERE harness_turn_id = ?""",
                    ("turn-db",),
                ).fetchone(),
            )
            link = cast(
                tuple[str],
                conn.execute(
                    "SELECT entity_id FROM trace_memory_links WHERE entity_id = ?",
                    ("entity-db",),
                ).fetchone(),
            )
            self.assertEqual(row[0], SESSION_IDENTITY.agent_session_id)
            self.assertEqual(row[1], "trace_test_agent")
            self.assertEqual(row[2], "completed")
            self.assertEqual(link[0], "entity-db")
        finally:
            _ = set_backend_for_test(previous_backend)
            _ = conn.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_rpc_search_agent_session_is_a_filter_not_adapter_identity(self):
        backend = tools.RpcBackend()
        rpc_call = cast(Callable[[str, dict[str, object]], object], backend.call)
        calls: list[dict[str, object]] = []

        def daemon_call(
            _path: str, _tool_name: str, kwargs: dict[str, object], **_transport: object
        ) -> dict[str, object]:
            calls.append(kwargs)
            return {"status": "ok"}

        with patch("saltmdb.mcp.tools.daemon_client.call", side_effect=daemon_call):
            _ = rpc_call(
                "search_traces", {"owner_id": "caller-supplied", "agent_session_id": "other"}
            )

        kwargs = calls[-1]
        self.assertEqual(kwargs["owner_id"], "trace_test_agent")
        self.assertEqual(kwargs["agent_session_id"], "other")


if __name__ == "__main__":
    _ = unittest.main()
