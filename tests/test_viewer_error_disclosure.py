import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler

LEAKY = "database is locked at /home/alice/.saltmdb/saltmdb.db"


class DummyRequest:
    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"")


class DummyServer:
    pass


class TestViewerErrorDisclosure(unittest.TestCase):
    """Unexpected failures return a stable public message, never raw exception text."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "errors.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _call(self, method_name, *args):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        captured = {}
        handler.send_json = lambda data, status=200: captured.update(data=data, status=status)
        getattr(handler, method_name)(*args)
        return captured["status"], captured["data"]

    def test_search_backend_failure_hides_exception_text(self):
        with patch(
            "saltmdb.viewer.routes.memory_service.search_memory", side_effect=RuntimeError(LEAKY)
        ):
            status, data = self._call("get_search", {"q": ["anything"]})

        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "Hybrid search unavailable")
        self.assertNotIn("alice", str(data))

    def test_search_rejects_bad_is_core_as_a_client_error_with_its_message(self):
        status, data = self._call("get_search", {"q": ["x"], "is_core": ["maybe"]})

        self.assertEqual(status, 400)
        self.assertIn("is_core must be one of", data["error"])

    def test_scatterplot_failure_hides_exception_text(self):
        with patch(
            "saltmdb.viewer.routes.scatterplot.try_load_vector_extension",
            side_effect=RuntimeError(LEAKY),
        ):
            status, data = self._call("get_scatterplot")

        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "Embedding projection unavailable")
        self.assertNotIn("alice", str(data))


if __name__ == "__main__":
    unittest.main()
