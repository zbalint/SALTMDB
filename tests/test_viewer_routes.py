import unittest
import tempfile
import os
import shutil
import json
from saltmdb.db.schema import init_db
from saltmdb.viewer.routes import SALTMDBHandler

class DummyRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
    def makefile(self, *args, **kwargs):
        import io
        return io.BytesIO(b"")

class DummyServer:
    pass

class TestViewerRoutes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_handler_get_db_connection(self):
        handler = SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())
        conn = handler.get_db_connection()
        self.assertIsNotNone(conn)
        conn.close()

if __name__ == "__main__":
    unittest.main()
