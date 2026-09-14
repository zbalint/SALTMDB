import os
import shutil
import tempfile
import unittest
import sqlite3
from typing import cast
from unittest.mock import patch

import sqlite_vec

from saltmdb.config import CHUNK_OVERLAP_CHARS, CHUNK_SIZE_CHARS
from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY
from saltmdb.utils.chunking import chunk_text


DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


class TestRelevancePreviewMCP(unittest.TestCase):
    """MCP-level relevance-preview contract through the real tools/backend seam."""
    temp_dir: str = ""
    db_path: str = ""
    conn: sqlite3.Connection = cast(sqlite3.Connection, cast(object, None))
    _prev_backend: object = cast(object, None)

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
        os.environ.pop("SALTMDB_DB_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title: str, content: str, tag: str) -> str:
        response = tools.store_memory(title=title, content=content, tags=[tag])
        self.assertEqual(response["status"], "ok")
        return response["data"]["id"]

    def _seed_chunk_embeddings(self, entity_id: str) -> None:
        row = self.conn.execute(
            "SELECT full_content, content_hash FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        full_content, content_hash = row
        chunks = chunk_text(full_content, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        self.assertTrue(chunks)
        for chunk_index, chunk in enumerate(chunks):
            self.conn.execute(
                "INSERT INTO entity_chunk_embeddings "
                "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{entity_id}::{chunk_index}",
                    entity_id,
                    sqlite_vec.serialize_float32(
                        _axis_vector(0 if chunk_index == 0 else 1)
                    ),
                    chunk_index,
                    chunk["char_start"],
                    chunk["char_end"],
                    content_hash,
                ),
            )
        self.conn.commit()

    def test_query_results_include_extractive_preview_for_multi_and_single_chunk_memories(self):
        long_content = (
            "# Preview Overview\n\n"
            "Preview needle is in the opening section.\n\n"
            + "\n\n".join(
                f"## Supporting Section {index}\n\n"
                f"Section {index} records durable preview context and a distinct detail "
                f"for later retrieval."
                for index in range(40)
            )
        )
        self.assertGreater(len(long_content), CHUNK_SIZE_CHARS)
        short_content = "Preview needle appears in this short memory."
        self.assertEqual(
            len(chunk_text(short_content, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)), 1
        )

        long_id = self._store("Long Preview Memory", long_content, "#preview-mcp")
        short_id = self._store("Short Preview Memory", short_content, "#preview-mcp")
        self._seed_chunk_embeddings(long_id)
        self._seed_chunk_embeddings(short_id)

        with patch.object(
            embedding_service, "embed_query_text", return_value=_axis_vector(0)
        ), patch.object(
            embedding_service, "embed_query_texts", return_value=[_axis_vector(0)]
        ):
            results = tools.search_memory(
                query_keywords="preview needle", limit=5, mode="broad", include_related=False
            )

        results_by_id = {item["id"]: item for item in results}
        self.assertIn(long_id, results_by_id)
        self.assertIn(short_id, results_by_id)
        expected_meta = {
            "auto_generated": True,
            "extractive": True,
            "query_specific": True,
            "complete": False,
        }
        for entity_id, stored_content in (
            (long_id, long_content),
            (short_id, short_content),
        ):
            item = results_by_id[entity_id]
            self.assertIsInstance(item["relevance_preview"], str)
            self.assertTrue(item["relevance_preview"])
            self.assertEqual(item["relevance_preview_meta"], expected_meta)
            self.assertIn(item["relevance_preview"], stored_content)

    def test_browse_results_never_include_relevance_preview(self):
        entity_id = self._store(
            "Browse-only Preview Memory",
            "This memory is returned by a tag browse and has no query.",
            "#preview-browse",
        )

        results = tools.search_memory(tags_filter=["#preview-browse"], limit=5)

        results_by_id = {item["id"]: item for item in results}
        self.assertIn(entity_id, results_by_id)
        for item in results:
            self.assertNotIn("relevance_preview", item)
            self.assertNotIn("relevance_preview_meta", item)


if __name__ == "__main__":
    unittest.main()
