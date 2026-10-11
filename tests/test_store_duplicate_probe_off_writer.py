"""Store duplicate advice preserves its response without occupying the daemon writer."""

import copy
import os
import sqlite3
import tempfile
import threading
import unittest
from typing import Any, cast
from unittest.mock import Mock, patch

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import DISPATCH_TABLE, dispatch_tool
from saltmdb.db.connection import open_read_connection
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service, reranker_service
from saltmdb.domain.services.memory_service import duplicates


class TestStoreDuplicateProbeOffWriter(unittest.TestCase):
    db_path: str = ""
    conn: sqlite3.Connection | None = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = os.path.join(temporary.name, "store.db")
        self.conn = init_db(self.db_path)
        self.addCleanup(lambda: self.conn.close() if self.conn is not None else None)
        self.enterContext(patch.dict(os.environ, {"SALTMDB_DB_PATH": self.db_path}))
        self.enterContext(patch.object(memory_service._embed_pool, "submit", return_value=None))
        self.enterContext(
            patch("saltmdb.domain.services.librarian_service.trigger_librarian", return_value=None)
        )
        model = Mock()
        model.rerank.side_effect = lambda _query, texts: [5.0] * len(texts)
        self.enterContext(patch.object(reranker_service, "get_model", return_value=model))

    def _kwargs(self, variant: str = "A") -> dict[str, Any]:
        return {
            "title": f"Authentication policy {variant}",
            "content": (
                "# Authentication policy\n\n"
                "OAuth access tokens are short lived and refresh tokens rotate on every use. "
                "API requests carry bearer headers and invalid refresh tokens are rejected. "
                f"Policy revision {variant} applies to service clients."
            ),
            "agent_id": "test-owner",
            "tags": ["auth-policy"],
            "context_id": "test-context",
        }

    def _store(self, variant: str = "A", **extra: Any) -> dict[str, Any]:
        if self.conn is None:
            raise AssertionError("setUp did not initialize a database connection")
        kwargs = self._kwargs(variant)
        kwargs.update({"db_path": self.db_path, "db_connection": self.conn})
        kwargs.update(extra)
        raw_result = memory_service.store_memory(**kwargs)
        if not isinstance(raw_result, dict):
            raise AssertionError(f"store_memory returned unexpected string: {raw_result}")
        return cast(dict[str, Any], raw_result)

    def _coordinator(self) -> DbWriteCoordinator:
        coordinator = DbWriteCoordinator(self.db_path)
        coordinator.start()
        self.addCleanup(coordinator.shutdown, timeout=5)
        return coordinator

    def _near_warning(self, result: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            next(item for item in result["warnings"] if item["code"] == "NEAR_DUPLICATE"),
        )

    def test_1_direct_inline_candidates_and_guidance(self):
        first = self._store()
        result = self._store("B")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["data"]["duplicate_candidates"],
            [
                {
                    "id": first["data"]["id"],
                    "title": "Authentication policy A",
                    "agent_id": "test-owner",
                    "scope": "shared",
                    "similarity_score": 5.0,
                }
            ],
        )
        near = self._near_warning(result)
        self.assertEqual(
            near["detail"]["duplicate_candidates"], result["data"]["duplicate_candidates"]
        )
        self.assertEqual(
            near["detail"]["guidance"],
            {
                "single": "supersede_memory",
                "several": "consolidate_memories",
                "related_not_redundant": "manage_relation",
            },
        )

    def test_2_sink_defers_probe_and_contains_redacted_request(self):
        first_sink: list[dict[str, Any]] = []
        first = self._store(probe_sink=first_sink)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first_sink[0]["exclude_ids"], [first["data"]["id"]])
        sink: list[dict[str, Any]] = []
        kwargs = self._kwargs("B")
        kwargs["content"] += " password: FictionalTestPassword123"
        with patch.object(
            duplicates, "check_duplicate_memories", wraps=duplicates.check_duplicate_memories
        ) as probe:
            raw_result = memory_service.store_memory(
                **kwargs, db_connection=self.conn, probe_sink=sink
            )
        if not isinstance(raw_result, dict):
            raise AssertionError(f"store_memory returned unexpected string: {raw_result}")
        result = cast(dict[str, Any], raw_result)
        probe.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["duplicate_candidates"], [])
        self.assertNotIn("NEAR_DUPLICATE", [item["code"] for item in result["warnings"]])
        self.assertEqual(
            sink,
            [
                {
                    **kwargs,
                    "content": self._kwargs("B")["content"] + " [REDACTED_SECRET]",
                    "exclude_ids": [result["data"]["id"]],
                    "db_path": self.db_path,
                }
            ],
        )
        rejected_sink = []
        rejected = self._store("A", probe_sink=rejected_sink)
        self.assertEqual(rejected["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")
        self.assertEqual(rejected_sink, [])
        update_sink = []
        updated = self._store(
            entity_id=first["data"]["id"], metadata={"reviewed": True}, probe_sink=update_sink
        )
        self.assertEqual(updated["status"], "ok")
        self.assertEqual(update_sink, [])

    def test_3_apply_matches_inline_advice_and_appends_warning_last(self):
        self._store()
        inline = self._store("B")
        sink = []
        deferred = self._store("C", probe_sink=sink)
        # B must not appear in the C probe; use the same candidate set as the inline store.
        memory_service.archive_memory(inline["data"]["id"], db_path=self.db_path)
        memory_service.apply_duplicate_probe(deferred, sink[0])
        self.assertEqual(
            deferred["data"]["duplicate_candidates"], inline["data"]["duplicate_candidates"]
        )
        self.assertEqual(self._near_warning(deferred), self._near_warning(inline))
        self.assertEqual(deferred["warnings"][-1]["code"], "NEAR_DUPLICATE")

    def test_3_empty_or_error_probe_preserves_envelope(self):
        sink = []
        result = self._store(probe_sink=sink)
        for response in (
            {"potential_duplicates": []},
            {"error": "", "potential_duplicates": [{"id": "ignored"}]},
            {"error": "unavailable", "potential_duplicates": [{"id": "ignored"}]},
        ):
            with self.subTest(response=response):
                original = copy.deepcopy(result)
                with patch.object(duplicates, "check_duplicate_memories", return_value=response):
                    memory_service.apply_duplicate_probe(result, sink[0])
                self.assertEqual(result, original)

    def test_4_daemon_matches_direct_response_and_probes_on_caller_thread(self):
        self._store()
        inline = self._store("B")
        memory_service.archive_memory(inline["data"]["id"], db_path=self.db_path)
        coordinator = self._coordinator()
        probe_threads = []
        original = duplicates.check_duplicate_memories

        def probe(**kwargs):
            probe_threads.append(threading.get_ident())
            self.assertNotEqual(threading.get_ident(), coordinator.writer_thread_id)
            return original(**kwargs)

        with patch.object(duplicates, "check_duplicate_memories", side_effect=probe):
            result = dispatch_tool("store_memory", self._kwargs("B"), coordinator)
        if not isinstance(result, dict):
            raise AssertionError(f"dispatch_tool returned unexpected string: {result}")
        self.assertEqual(probe_threads, [threading.get_ident()])
        self.assertEqual(
            [item["code"] for item in result["warnings"]],
            [item["code"] for item in inline["warnings"]],
        )
        self.assertEqual(
            [item["title"] for item in result["data"]["duplicate_candidates"]],
            ["Authentication policy A"],
        )
        self.assertEqual(
            self._near_warning(result)["detail"]["guidance"],
            self._near_warning(inline)["detail"]["guidance"],
        )
        self.assertEqual(result["data"].keys(), inline["data"].keys())
        self.assertEqual(result["effective"], inline["effective"])

    def test_5_probe_observes_committed_store_through_fresh_reader(self):
        self._store()
        coordinator = self._coordinator()
        original = duplicates.check_duplicate_memories
        observed_ids: list[str] = []

        def probe(**kwargs: Any):
            reader = open_read_connection(self.db_path)
            try:
                row = reader.execute(
                    "SELECT id FROM entities WHERE id = ?", (kwargs["exclude_ids"][0],)
                ).fetchone()
                self.assertIsNotNone(row, "probe must run after the new row commits")
                observed_ids.append(row[0])
            finally:
                reader.close()
            return original(**kwargs)

        with patch.object(duplicates, "check_duplicate_memories", side_effect=probe):
            result = cast(
                dict[str, Any], dispatch_tool("store_memory", self._kwargs("B"), coordinator)
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(observed_ids, [result["data"]["id"]])

    def test_6_writer_completes_event_while_probe_is_blocked(self):
        self._store()
        coordinator = self._coordinator()
        entered = threading.Event()
        release = threading.Event()
        event_finished = threading.Event()
        stores = []
        events = []
        original = duplicates.check_duplicate_memories

        def probe(**kwargs):
            entered.set()
            self.assertTrue(release.wait(20), "test did not release duplicate probe")
            return original(**kwargs)

        def store():
            stores.append(dispatch_tool("store_memory", self._kwargs("B"), coordinator))

        def event():
            try:
                events.append(
                    dispatch_tool(
                        "log_event",
                        {
                            "type": "test",
                            "content": "Writer remains available during advisory scoring.",
                            "agent_id": "test-owner",
                        },
                        coordinator,
                    )
                )
            finally:
                event_finished.set()

        with patch.object(duplicates, "check_duplicate_memories", side_effect=probe):
            store_thread = threading.Thread(target=store)
            event_thread = threading.Thread(target=event)
            store_thread.start()
            try:
                self.assertTrue(entered.wait(20), "store did not reach duplicate probe")
                event_thread.start()
                self.assertTrue(
                    event_finished.wait(2), "log_event was blocked behind the duplicate probe"
                )
                self.assertFalse(release.is_set())
                self.assertEqual(events[0]["status"], "ok")
                self.assertEqual(stores, [])
            finally:
                release.set()
                store_thread.join(20)
                if event_thread.ident is not None:
                    event_thread.join(20)
        self.assertFalse(store_thread.is_alive())
        self.assertFalse(event_thread.is_alive())
        self.assertEqual(stores[0]["status"], "ok")
        self.assertEqual(
            [item["title"] for item in stores[0]["data"]["duplicate_candidates"]],
            ["Authentication policy A"],
        )

    def test_7_probe_exception_preserves_success_and_logs_warning(self):
        self._store()
        coordinator = self._coordinator()
        with (
            patch.object(
                duplicates, "check_duplicate_memories", side_effect=RuntimeError("probe failed")
            ),
            self.assertLogs("saltmdb.daemon.dispatch", "WARNING") as logs,
        ):
            result = cast(
                dict[str, Any], dispatch_tool("store_memory", self._kwargs("B"), coordinator)
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["duplicate_candidates"], [])
        self.assertNotIn("NEAR_DUPLICATE", [item["code"] for item in result["warnings"]])
        self.assertTrue(any(record.exc_info for record in logs.records))
        self.assertEqual(
            memory_service.get_memory(result["data"]["id"], db_path=self.db_path)["status"], "ok"
        )

    def test_7_rejected_or_nondict_store_does_not_apply_nonempty_sink(self):
        coordinator = self._coordinator()
        for envelope in (
            {"status": "rejected", "errors": [{"code": "INTERNAL_ERROR"}], "warnings": []},
            "Error: rejected",
        ):
            with self.subTest(envelope=envelope):

                def reject(**kwargs):
                    kwargs["probe_sink"].append({"db_path": self.db_path})
                    return envelope

                with (
                    patch.dict(DISPATCH_TABLE, {"store_memory": reject}),
                    patch.object(duplicates, "check_duplicate_memories") as probe,
                ):
                    result = dispatch_tool("store_memory", self._kwargs(), coordinator)
                self.assertEqual(result, envelope)
                probe.assert_not_called()

    def test_8_client_sink_is_overwritten_and_advice_still_returned(self):
        self._store()
        coordinator = self._coordinator()
        client_sink = [{"untrusted": True}]
        result = cast(
            dict[str, Any],
            dispatch_tool(
                "store_memory", {**self._kwargs("B"), "probe_sink": client_sink}, coordinator
            ),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [item["title"] for item in result["data"]["duplicate_candidates"]],
            ["Authentication policy A"],
        )
        self.assertEqual(self._near_warning(result)["code"], "NEAR_DUPLICATE")
        self.assertEqual(client_sink, [{"untrusted": True}])
