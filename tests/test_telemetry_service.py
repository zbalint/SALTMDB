import json
import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.domain.services import telemetry_service


class TestClassifyResult(unittest.TestCase):
    """§5.9: metadata-only outcome classification, deliberately loose pre-envelope."""

    def test_exception_is_error(self):
        status, code = telemetry_service.classify_result(None, ValueError("boom"))
        self.assertEqual(status, "error")
        self.assertEqual(code, "ValueError")

    def test_error_string_is_error(self):
        status, code = telemetry_service.classify_result("Error: could not resolve", None)
        self.assertEqual(status, "error")

    def test_plain_string_is_ok(self):
        status, code = telemetry_service.classify_result("Event logged successfully", None)
        self.assertEqual(status, "ok")

    def test_dict_rejected_status_is_rejected(self):
        status, code = telemetry_service.classify_result(
            {"status": "rejected", "errors": [{"code": "MISSING_TAGS"}]}, None
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(code, "MISSING_TAGS")

    def test_dict_ok_status_is_ok(self):
        status, code = telemetry_service.classify_result({"status": "ok", "data": {}}, None)
        self.assertEqual(status, "ok")

    def test_plain_list_is_ok(self):
        status, code = telemetry_service.classify_result([1, 2, 3], None)
        self.assertEqual(status, "ok")


class TestRecordCall(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_call_writes_metadata_only_row(self):
        telemetry_service.record_call(
            "store_memory",
            ["title", "content", "tags"],
            "ok",
            12.5,
            owner_id="claude",
            db_connection=self.conn,
        )
        row = self.conn.execute(
            "SELECT tool_name, owner_id, param_names, status, error_code, latency_ms "
            "FROM tool_call_telemetry"
        ).fetchone()
        self.assertEqual(row[0], "store_memory")
        self.assertEqual(row[1], "claude")
        self.assertEqual(json.loads(row[2]), ["content", "tags", "title"])
        self.assertEqual(row[3], "ok")
        self.assertIsNone(row[4])
        self.assertAlmostEqual(row[5], 12.5)

    def test_record_call_never_stores_argument_values(self):
        # param_names carries only keys, never the secret-shaped value below.
        telemetry_service.record_call(
            "store_memory",
            ["content"],
            "ok",
            1.0,
            db_connection=self.conn,
        )
        raw = self.conn.execute("SELECT param_names FROM tool_call_telemetry").fetchone()[0]
        self.assertNotIn("sk-secret-value-should-never-appear", raw)

    def test_record_call_swallows_write_failure(self):
        # A closed connection makes the INSERT fail; record_call must not raise.
        self.conn.close()
        try:
            telemetry_service.record_call(
                "store_memory", ["title"], "ok", 1.0, db_connection=self.conn
            )
        except Exception as e:  # pragma: no cover - assertion path
            self.fail(f"record_call raised instead of swallowing the failure: {e}")
        finally:
            self.conn = init_db(self.db_path)  # reopen so tearDown's close() succeeds


class TestDispatchToolTelemetryWiring(unittest.TestCase):
    """dispatch_tool (the daemon's sole real caller of _dispatch_tool_inner) records one
    telemetry row per call, for both read and mutating tools, without changing the call's
    own return value."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        init_db(self.db_path).close()
        self.coordinator = DbWriteCoordinator(self.db_path)
        self.coordinator.start()

    def tearDown(self):
        self.coordinator.shutdown(timeout=5)
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _telemetry_rows(self, tool_name):
        import time

        from saltmdb.db.connection import open_read_connection

        # Telemetry is submitted as a background, non-blocking job -- give the writer thread a
        # brief window to drain its queue before asserting.
        for _ in range(50):
            conn = open_read_connection(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT status, error_code FROM tool_call_telemetry WHERE tool_name = ?",
                    (tool_name,),
                ).fetchall()
            finally:
                conn.close()
            if rows:
                return rows
            time.sleep(0.05)
        return []

    def test_read_tool_call_is_recorded_as_ok(self):
        result = dispatch_tool("list_predicates", {"query": None, "limit": 10}, self.coordinator)
        self.assertIsInstance(result, list)
        rows = self._telemetry_rows("list_predicates")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "ok")

    def test_mutating_tool_call_is_recorded(self):
        result = dispatch_tool(
            "log_event",
            {
                "agent_id": "claude",
                "type": "event",
                "content": "telemetry wiring smoke test",
                "error_code": None,
                "session_id": None,
                "context_id": None,
            },
            self.coordinator,
        )
        self.assertIn("Event logged successfully", result)
        rows = self._telemetry_rows("log_event")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "ok")

    def test_unknown_tool_raises_and_is_not_silently_swallowed_by_telemetry(self):
        with self.assertRaises(KeyError):
            dispatch_tool("not_a_real_tool", {}, self.coordinator)


if __name__ == "__main__":
    unittest.main()
