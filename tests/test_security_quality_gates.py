import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.domain.services.disposition_service import _decode_review_token
from saltmdb.mcp.tools import _normalize_list_or_str
from saltmdb.utils.text import resolve_entity_id


class VectorExtensionCleanupTests(unittest.TestCase):
    def _coordinator(self, conn):
        coordinator = DbWriteCoordinator(":memory:")
        coordinator._conn = conn
        return coordinator

    def test_success_disables_extension_loading(self):
        conn = MagicMock()
        sqlite_vec = MagicMock()
        coordinator = self._coordinator(conn)

        with patch("saltmdb.daemon.db_write_coordinator.sqlite_vec", sqlite_vec):
            coordinator._load_vector_extension()

        conn.enable_load_extension.assert_any_call(True)
        conn.enable_load_extension.assert_any_call(False)
        sqlite_vec.load.assert_called_once_with(conn)

    def test_load_failure_still_disables_and_scalar_write_survives(self):
        conn = sqlite3.connect(":memory:")
        sqlite_vec = MagicMock()
        sqlite_vec.load.side_effect = sqlite3.OperationalError("extension unavailable")
        coordinator = self._coordinator(conn)

        with patch("saltmdb.daemon.db_write_coordinator.sqlite_vec", sqlite_vec):
            with self.assertLogs("saltmdb.daemon.db_write_coordinator", level="WARNING") as logs:
                coordinator._load_vector_extension()

        conn.execute("CREATE TABLE scalar_data (value TEXT)")
        conn.execute("INSERT INTO scalar_data VALUES (?)", ("survives",))
        self.assertEqual(conn.execute("SELECT value FROM scalar_data").fetchone()[0], "survives")
        self.assertIn("scalar writes remain enabled", " ".join(logs.output))
        conn.close()

    def test_disable_failure_is_logged_after_load_failure(self):
        conn = MagicMock()
        conn.enable_load_extension.side_effect = [None, sqlite3.OperationalError("disable failed")]
        sqlite_vec = MagicMock()
        sqlite_vec.load.side_effect = RuntimeError("load failed")
        coordinator = self._coordinator(conn)

        with patch("saltmdb.daemon.db_write_coordinator.sqlite_vec", sqlite_vec):
            with self.assertLogs("saltmdb.daemon.db_write_coordinator", level="WARNING") as logs:
                coordinator._load_vector_extension()

        self.assertEqual(conn.enable_load_extension.call_args_list[0].args, (True,))
        self.assertEqual(conn.enable_load_extension.call_args_list[1].args, (False,))
        joined = " ".join(logs.output)
        self.assertIn("extension load unavailable", joined)
        self.assertIn("Could not disable sqlite extension loading", joined)


class NarrowFallbackTests(unittest.TestCase):
    def test_malformed_review_token_returns_none(self):
        self.assertIsNone(_decode_review_token("%%%not-a-token%%%"))

    def test_malformed_json_list_uses_string_fallback_and_logs(self):
        with self.assertLogs("saltmdb.mcp.tools", level="DEBUG") as logs:
            result = _normalize_list_or_str("[not valid json]")

        self.assertEqual(result, ["[not valid json]"])
        self.assertIn("malformed JSON list input", " ".join(logs.output))

    def test_entity_resolution_returns_input_when_database_lookup_fails(self):
        conn = MagicMock()
        conn.execute.side_effect = sqlite3.OperationalError("database unavailable")

        with self.assertLogs("saltmdb.utils.text", level="DEBUG") as logs:
            result = resolve_entity_id(conn, "not-an-id")

        self.assertEqual(result, "not-an-id")
        self.assertIn("lookup unavailable", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
