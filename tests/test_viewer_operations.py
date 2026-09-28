import io
import os
import shutil
import tempfile
import unittest

from saltmdb.daemon.server import _DaemonState
from saltmdb.db.schema import init_db
from saltmdb.viewer.context import ViewerReadGateway
from saltmdb.viewer.routes import SALTMDBHandler


class DummyRequest:
    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerOperations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "ops.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _operations(self):
        state = _DaemonState(self.db_path, "test", True)
        state.service_port = 12345
        state.viewer_port = 9876
        server = DummyServer()
        server.viewer_gateway = ViewerReadGateway(self.db_path, state)
        server.daemon_state = state
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 0), server)
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        handler.get_operations()
        self.assertEqual(captured["status"], 200)
        return captured["data"]

    def _snapshot(self, name, size):
        backups = os.path.join(self.temp_dir, "backups")
        os.makedirs(backups, exist_ok=True)
        with open(os.path.join(backups, name), "wb") as handle:
            handle.write(b"x" * size)

    def test_reports_the_latest_real_snapshot_not_a_phantom_backup_file(self):
        self._snapshot("saltmdb_snapshot_20260101_000000.db", 10)
        self._snapshot("saltmdb_snapshot_20260102_000000.db", 20)

        data = self._operations()

        self.assertEqual(
            data["database"]["latest_snapshot"]["name"], "saltmdb_snapshot_20260102_000000.db"
        )
        self.assertEqual(data["database"]["latest_snapshot"]["bytes"], 20)
        self.assertNotIn("backup_bytes", data["database"]["files"])

    def test_no_snapshot_is_reported_as_none(self):
        self.assertIsNone(self._operations()["database"]["latest_snapshot"])

    def test_placeholder_maintenance_block_is_gone_and_warnings_are_real(self):
        healthy = self._operations()
        self.assertNotIn("maintenance", healthy)
        self.assertEqual(healthy["warnings"], [])

        self.conn.execute(
            """INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title,
                                     full_content, embedding_status)
               VALUES ('bad', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00',
                       '2026-09-01T00:00:00+00:00', 'Bad', 'body', 'failed')"""
        )
        self.conn.commit()

        degraded = self._operations()
        self.assertEqual([w["code"] for w in degraded["warnings"]], ["EMBEDDINGS_FAILED"])


if __name__ == "__main__":
    unittest.main()
