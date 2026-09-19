import inspect
import os
import shutil
import sqlite3
import tempfile
import unittest
from typing import Any, cast
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY
from saltmdb.daemon import dispatch, protocol


class TestRetrieveContextWiring(unittest.TestCase):
    temp_dir: str = ""
    db_path: str = ""
    conn: sqlite3.Connection = cast(sqlite3.Connection, cast(object, None))
    previous_db_path: str | None = None
    previous_backend: Any = None

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        self.previous_db_path = os.environ.get("SALTMDB_DB_PATH")
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("wiring-owner")
        self.previous_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self.previous_backend)
        SESSION_IDENTITY.reset()
        if self.previous_db_path is None:
            os.environ.pop("SALTMDB_DB_PATH", None)
        else:
            os.environ["SALTMDB_DB_PATH"] = self.previous_db_path
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_registration_and_protocol_classification_are_read_only(self):
        self.assertIn("retrieve_context", dispatch.DISPATCH_TABLE)
        self.assertIn("retrieve_context", protocol.READ_TOOLS)
        self.assertNotIn("retrieve_context", dispatch.MUTATING_TOOLS)
        self.assertNotIn("retrieve_context", protocol.WRITE_TOOLS)

    def test_dispatch_forwards_omitted_optional_values_as_none(self):
        with patch.object(
            dispatch.retrieve_context_service,
            "assemble_retrieve_context",
            return_value={},
        ) as assemble:
            dispatch._dispatch_retrieve_context(query="q", owner_id="owner")

        assemble.assert_called_once_with(
            query="q",
            owner_id="owner",
            limit=None,
            budget_tokens=None,
            strategy="local",
        )

    def test_dispatch_requires_string_query_but_allows_empty_string(self):
        with self.assertRaises(ValueError):
            dispatch._dispatch_retrieve_context(owner_id="owner")
        with self.assertRaises(ValueError):
            dispatch._dispatch_retrieve_context(query=123, owner_id="owner")

        with patch.object(
            dispatch.retrieve_context_service,
            "assemble_retrieve_context",
            return_value={},
        ) as assemble:
            dispatch._dispatch_retrieve_context(query="", owner_id="owner")

        assemble.assert_called_once_with(
            query="",
            owner_id="owner",
            limit=None,
            budget_tokens=None,
            strategy="local",
        )

    def test_public_schema_exposes_query_controls_without_owner_id(self):
        self.assertEqual(
            list(inspect.signature(tools.retrieve_context).parameters),
            ["query", "limit", "budget_tokens", "strategy"],
        )
        self.assertNotIn("owner_id", inspect.signature(tools.retrieve_context).parameters)
        registered = tools.mcp._tool_manager._tools["retrieve_context"]
        self.assertNotIn("owner_id", registered.parameters.get("properties", {}))

    def test_public_tool_reaches_real_dispatch_and_returns_envelope(self):
        query = "wiring-end-to-end-query"
        result = store_memory(
            content=f"{query} fixture body",
            title="Wiring end-to-end memory",
            owner_id="wiring-owner",
            db_connection=self.conn,
        )
        if not isinstance(result, dict):
            self.fail(f"fixture store failed: {result}")
        self.assertEqual(result["status"], "ok")

        envelope = tools.retrieve_context(query=query, limit=1)

        self.assertEqual(
            set(envelope),
            {"query", "memories", "edges", "lineage", "conflict_sets", "metadata"},
        )
        self.assertEqual(envelope["query"], query)


if __name__ == "__main__":
    unittest.main()
