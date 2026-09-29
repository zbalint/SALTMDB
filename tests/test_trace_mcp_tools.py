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
        return {
            "status": "ok",
            "data": {"id": "entity-1", "new_id": "entity-1", "entity_id": "entity-1"},
        }


class _FakeWriteBackend:
    def __init__(self, data: dict[str, object], status: str = "ok") -> None:
        self.result = {"status": status, "data": data}

    def call(self, _tool_name: str, _kwargs: dict[str, object]) -> dict[str, object]:
        return self.result


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
        with (
            patch.dict(os.environ, {"SALTMDB_TRACE_CAPTURE_ENABLED": "true"}),
            patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend),
        ):
            tools.capture_trace_start("codex", "codex-session", "turn-1", "prompt")
            tools.store_memory("A title", content="A body")
            tools.capture_trace_memory_link("turn-1")
            tools.capture_trace_complete("turn-1", "assistant answer")
            tools.search_traces(agent_session_id="filter-session", limit=3)
            tools.get_trace("trace-1")

        calls = dict(self.backend.calls)
        self.assertEqual(calls["capture_trace_start"]["owner_id"], "trace_test_agent")
        self.assertEqual(calls["capture_trace_memory_link"]["owner_id"], "trace_test_agent")
        self.assertEqual(calls["capture_trace_memory_link"]["entity_id"], "entity-1")
        self.assertEqual(calls["capture_trace_memory_link"]["just_run_tool_name"], "store_memory")
        self.assertEqual(calls["capture_trace_complete"]["owner_id"], "trace_test_agent")
        self.assertNotIn("owner_id", calls["search_traces"])
        self.assertEqual(calls["search_traces"]["agent_session_id"], "filter-session")
        self.assertNotIn("owner_id", calls["get_trace"])
        for tool_name, kwargs in calls.items():
            if tool_name != "search_traces":
                self.assertNotIn("agent_session_id", kwargs)

    def test_capture_tools_reject_without_backend_call_when_flag_disabled(self):
        env = {k: v for k, v in os.environ.items() if k != "SALTMDB_TRACE_CAPTURE_ENABLED"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend),
        ):
            results = [
                tools.capture_trace_start("codex", "codex-session", "turn-1", "prompt"),
                tools.capture_trace_memory_link("turn-1", "entity-1", "store_memory"),
                tools.capture_trace_complete("turn-1", "assistant answer"),
            ]
            searched = tools.search_traces()

        for result in results:
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["errors"][0]["code"], "TRACE_CAPTURE_DISABLED")
        self.assertEqual([name for name, _ in self.backend.calls], ["search_traces"])
        self.assertEqual(searched["status"], "ok")

    def test_hook_output_returns_an_empty_object_and_still_captures(self):
        # Codex validates an mcp_tool hook's text result as hook JSON and rejects the SALTMDB
        # envelope; a hook entry opts in with hook_output=true to get a bare {} instead.
        with (
            patch.dict(os.environ, {"SALTMDB_TRACE_CAPTURE_ENABLED": "true"}),
            patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend),
        ):
            results = [
                tools.capture_trace_start("codex", "s", "turn-1", "prompt", hook_output=True),
                tools.capture_trace_memory_link("turn-1", hook_output=True),
                tools.capture_trace_complete("turn-1", "answer", hook_output=True),
            ]
        self.assertEqual(results, [{}, {}, {}])
        self.assertEqual(
            [name for name, _ in self.backend.calls],
            ["capture_trace_start", "capture_trace_complete"],
        )
        for _, kwargs in self.backend.calls:
            self.assertNotIn("hook_output", kwargs)

    def test_hook_output_stays_empty_when_capture_is_disabled(self):
        env = {k: v for k, v in os.environ.items() if k != "SALTMDB_TRACE_CAPTURE_ENABLED"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend),
        ):
            results = [
                tools.capture_trace_start("codex", "s", "turn-1", "prompt", hook_output=True),
                tools.capture_trace_memory_link("turn-1", hook_output=True),
                tools.capture_trace_complete("turn-1", "answer", hook_output=True),
            ]
        self.assertEqual(results, [{}, {}, {}])
        self.assertEqual(self.backend.calls, [])

    def test_hook_output_text_over_the_real_mcp_protocol_is_a_bare_empty_object(self):
        import asyncio
        import json

        def text_of(arguments: dict[str, object]) -> str:
            with (
                patch.dict(os.environ, {"SALTMDB_TRACE_CAPTURE_ENABLED": "true"}),
                patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend),
            ):
                content = asyncio.run(tools.mcp.call_tool("capture_trace_complete", arguments))
            return "".join(block.text for block in content)  # type: ignore[attr-defined]

        base = {"harness_turn_id": "turn-1", "final_assistant_message": "answer"}
        self.assertEqual(json.loads(text_of({**base, "hook_output": True})), {})
        self.assertEqual(json.loads(text_of({**base, "hook_output": "true"})), {})
        self.assertEqual(json.loads(text_of(base))["status"], "ok")

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

        dispatch_table = cast(dict[str, Callable[..., object]], getattr(dispatch, "DISPATCH_TABLE"))

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
                with patch(
                    "saltmdb.mcp.tools._backend_or_raise",
                    return_value=_FakeWriteBackend({"id": "entity-db"}),
                ):
                    _ = tools._call_and_record_write("store_memory", {})
                linked = cast(dict[str, object], tools.capture_trace_memory_link("turn-db"))
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
        self.assertNotIn("owner_id", kwargs)
        self.assertEqual(kwargs["agent_session_id"], "other")


if __name__ == "__main__":
    _ = unittest.main()


class TestTraceWriteLinking(unittest.TestCase):
    """The adapter, not a hook template, says which entities a turn wrote."""

    def setUp(self):
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("trace_test_agent")
        tools._drain_pending_trace_writes()
        self.backend = _CaptureBackend()

    def tearDown(self):
        tools._drain_pending_trace_writes()
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")

    def _record(self, tool_name, data, status="ok", capture="true"):
        with (
            patch.dict(os.environ, {"SALTMDB_TRACE_CAPTURE_ENABLED": capture}),
            patch(
                "saltmdb.mcp.tools._backend_or_raise", return_value=_FakeWriteBackend(data, status)
            ),
        ):
            return tools._call_and_record_write(tool_name, {})

    def _link(self, *args, **kwargs):
        with (
            patch.dict(os.environ, {"SALTMDB_TRACE_CAPTURE_ENABLED": "true"}),
            patch("saltmdb.mcp.tools._backend_or_raise", return_value=self.backend),
        ):
            return tools.capture_trace_memory_link(*args, **kwargs)

    def _linked_ok(self):
        def call(tool, kwargs):
            self.backend.calls.append((tool, kwargs))
            return {"status": "ok", "data": {"write_operation": "x", "linked": True}}

        self.backend.call = call  # type: ignore[method-assign]

    def test_each_write_tool_records_the_entity_id_from_its_own_result_shape(self):
        self._record("store_memory", {"id": "e-store"})
        self._record("revise_memory", {"old_id": "x", "new_id": "e-revise"})
        self._record("supersede_memory", {"old_id": "x", "new_id": "e-supersede"})
        self._record("consolidate_memories", {"entity_id": "e-consolidate"})
        self.assertEqual(
            tools._drain_pending_trace_writes(),
            [
                ("store_memory", "e-store"),
                ("revise_memory", "e-revise"),
                ("supersede_memory", "e-supersede"),
                ("consolidate_memories", "e-consolidate"),
            ],
        )

    def test_rejected_writes_and_disabled_capture_record_nothing(self):
        self._record("store_memory", {"id": "e1"}, status="rejected")
        self._record("store_memory", {"id": "e2"}, capture="false")
        self.assertEqual(tools._drain_pending_trace_writes(), [])

    def test_hook_call_links_every_pending_write_then_is_a_noop(self):
        self._record("store_memory", {"id": "e1"})
        self._record("revise_memory", {"new_id": "e2"})
        self._linked_ok()
        first = cast(dict, self._link("turn-1"))
        self.assertEqual([r["entity_id"] for r in first["data"]["results"]], ["e1", "e2"])
        self.assertEqual(
            [(k["entity_id"], k["just_run_tool_name"]) for _, k in self.backend.calls],
            [("e1", "store_memory"), ("e2", "revise_memory")],
        )
        second = cast(dict, self._link("turn-1"))
        self.assertEqual(second["data"]["results"], [])
        self.assertEqual(len(self.backend.calls), 2)

    def test_legacy_hook_arguments_are_ignored(self):
        self._record("store_memory", {"id": "real-id"})
        self._linked_ok()
        _ = self._link("turn-1", "attacker-chosen", "mcp__saltmdb__store_memory")
        self.assertEqual(self.backend.calls[0][1]["entity_id"], "real-id")
        self.assertEqual(self.backend.calls[0][1]["just_run_tool_name"], "store_memory")

    def test_unknown_turn_is_reported_per_entity_and_not_fatal(self):
        self._record("store_memory", {"id": "e1"})
        self.backend.call = lambda tool, kwargs: {  # type: ignore[method-assign]
            "status": "rejected",
            "errors": [{"code": "UNKNOWN_TRACE"}],
        }
        result = cast(dict, self._link("turn-x"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["data"]["results"], [{"entity_id": "e1", "error_code": "UNKNOWN_TRACE"}]
        )

    def test_daemon_failure_requeues_unprocessed_writes(self):
        self._record("store_memory", {"id": "e1"})
        self._record("store_memory", {"id": "e2"})

        def boom(_tool, _kwargs):
            raise RuntimeError("daemon down")

        self.backend.call = boom  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            self._link("turn-1")
        self.assertEqual(
            tools._drain_pending_trace_writes(), [("store_memory", "e1"), ("store_memory", "e2")]
        )
