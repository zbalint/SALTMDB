import os
import shutil
import tempfile
import unittest

from saltmdb.config import CONTENT_FILE_DUMP_THRESHOLD_CHARS
from saltmdb.db.schema import init_db
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY
from saltmdb.utils.text import large_content_descriptor


class TestLargeContentDescriptor(unittest.TestCase):
    """Unit coverage for the read-side dump-to-file utility, independent of the MCP surface."""

    def setUp(self):
        self.dump_dir = tempfile.mkdtemp()
        self._prev_dump_dir = os.environ.get("SALTMDB_CONTENT_DUMP_DIR")
        os.environ["SALTMDB_CONTENT_DUMP_DIR"] = self.dump_dir

    def tearDown(self):
        if self._prev_dump_dir is None:
            os.environ.pop("SALTMDB_CONTENT_DUMP_DIR", None)
        else:
            os.environ["SALTMDB_CONTENT_DUMP_DIR"] = self._prev_dump_dir
        shutil.rmtree(self.dump_dir, ignore_errors=True)

    def test_small_content_returned_inline_unchanged(self):
        small = "A short memory body." * 10
        self.assertLess(len(small), CONTENT_FILE_DUMP_THRESHOLD_CHARS)
        self.assertEqual(large_content_descriptor(small), {"content": small})

    def test_content_exactly_at_threshold_stays_inline(self):
        exact = "x" * CONTENT_FILE_DUMP_THRESHOLD_CHARS
        self.assertEqual(large_content_descriptor(exact), {"content": exact})

    def test_oversized_content_dumped_to_file_with_preview(self):
        large = "y" * (CONTENT_FILE_DUMP_THRESHOLD_CHARS + 1)
        result = large_content_descriptor(large)
        self.assertNotIn("content", result)
        self.assertEqual(result["content_preview"], large[:500])
        path = result["content_file_path"]
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.startswith(self.dump_dir))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), large)


class TestContentFilePathMcpSurface(unittest.TestCase):
    """Integration coverage: store_memory/revise_memory/supersede_memory accepting
    content_file_path as an alternative to inline content, through the real tools.py wrapper."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_file(self, text: str) -> str:
        path = os.path.join(self.temp_dir, "source.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_store_memory_with_content_file_path_reads_the_file(self):
        path = self._write_file("A complete and sufficiently descriptive body from a file.")
        result = tools.store_memory(
            title="File-sourced memory",
            tags=["#file"],
            content_file_path=path,
        )
        self.assertEqual(result["status"], "ok")
        stored = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (result["data"]["id"],)
        ).fetchone()[0]
        self.assertEqual(stored, "A complete and sufficiently descriptive body from a file.")

    def test_store_memory_rejects_both_content_and_content_file_path(self):
        path = self._write_file("irrelevant")
        result = tools.store_memory(
            title="Bad call",
            tags=["#file"],
            content="inline",
            content_file_path=path,
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "CONTENT_AND_FILE_PATH_BOTH_SET")

    def test_store_memory_rejects_neither_content_nor_content_file_path(self):
        result = tools.store_memory(title="Bad call", tags=["#file"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "MISSING_CONTENT")

    def test_store_memory_reports_unreadable_content_file_path(self):
        result = tools.store_memory(
            title="Bad call",
            tags=["#file"],
            content_file_path=os.path.join(self.temp_dir, "does-not-exist.md"),
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "CONTENT_FILE_READ_FAILED")

    def test_store_memory_reports_non_utf8_content_file_path(self):
        # UnicodeDecodeError is a ValueError subclass, not an OSError -- must still surface as
        # the same structured CONTENT_FILE_READ_FAILED rejection, not an uncaught exception.
        path = os.path.join(self.temp_dir, "binary.md")
        with open(path, "wb") as f:
            f.write(b"\xff\xfe not valid utf-8 \x80\x81")
        result = tools.store_memory(
            title="Bad call",
            tags=["#file"],
            content_file_path=path,
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "CONTENT_FILE_READ_FAILED")

    def test_supersede_memory_with_content_file_path_reads_the_file(self):
        original = tools.store_memory(
            title="Original",
            tags=["#file"],
            content="A complete and sufficiently descriptive original body.",
        )
        path = self._write_file("A complete and sufficiently descriptive newer body from a file.")
        result = tools.supersede_memory(
            entity_id=original["data"]["id"],
            title="Newer",
            tags=["#file"],
            reason="A later decision replaced the old one.",
            content_file_path=path,
        )
        self.assertEqual(result["status"], "ok")
        stored = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (result["data"]["new_id"],)
        ).fetchone()[0]
        self.assertEqual(
            stored, "A complete and sufficiently descriptive newer body from a file."
        )

    def test_get_memory_dumps_oversized_content_to_file(self):
        os.environ["SALTMDB_CONTENT_DUMP_DIR"] = tempfile.mkdtemp()
        section = (
            "## Section\n\nA complete and sufficiently descriptive paragraph of body text.\n\n"
        )
        large_body = "# Large memory\n\n" + section * (
            (CONTENT_FILE_DUMP_THRESHOLD_CHARS // len(section)) + 2
        )
        self.assertGreater(len(large_body), CONTENT_FILE_DUMP_THRESHOLD_CHARS)
        stored = tools.store_memory(title="Large memory", tags=["#large"], content=large_body)
        self.assertEqual(stored["status"], "ok")
        fetched = tools.get_memory(entity_id=stored["data"]["id"])
        self.assertNotIn("content", fetched["data"])
        self.assertEqual(fetched["data"]["content_preview"], large_body[:500])
        stored_content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (stored["data"]["id"],)
        ).fetchone()[0]
        with open(fetched["data"]["content_file_path"], encoding="utf-8") as f:
            self.assertEqual(f.read(), stored_content)


if __name__ == "__main__":
    unittest.main()
