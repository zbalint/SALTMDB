import os
import shutil
import tempfile
import unittest
import sqlite3
from typing import cast
from unittest.mock import patch

import sqlite_vec

from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.memory_service import get_relevance_preview_data
from saltmdb.config import SNIPPET_ELLIPSIS


DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


class TestRelevancePreviewChunks(unittest.TestCase):
    """Unit tests for extractive chunk selection against controlled vector fixtures."""

    temp_dir: str = ""
    db_path: str = ""
    conn: sqlite3.Connection = cast(sqlite3.Connection, cast(object, None))

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_entity(
        self, entity_id: str, full_content: str, content_hash: str, status: str = "raw"
    ) -> None:
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, datetime('now'), datetime('now'), datetime('now'), 'test_user', ?, ?, ?, ?)",
            (entity_id, status, entity_id, full_content, content_hash),
        )
        self.conn.commit()

    def _insert_chunk(
        self,
        entity_id: str,
        chunk_index: int,
        vector: list[float],
        char_start: int,
        char_end: int,
        content_hash: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"{entity_id}::{chunk_index}",
                entity_id,
                sqlite_vec.serialize_float32(vector),
                chunk_index,
                char_start,
                char_end,
                content_hash,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _sections(count: int, width: int = 48) -> tuple[str, list[tuple[int, int]]]:
        sections = [
            f"chunk-{index}-" + ("x" * (width - len(f"chunk-{index}-"))) for index in range(count)
        ]
        content = "".join(sections)
        offsets = []
        start = 0
        for section in sections:
            end = start + len(section)
            offsets.append((start, end))
            start = end
        return content, offsets

    def test_opening_chunk_is_added_without_displacing_best_nonzero_chunk(self):
        entity_id = "opening-is-additive"
        content, offsets = self._sections(4)
        content_hash = "opening-current"
        self._insert_entity(entity_id, content, content_hash)
        self._insert_chunk(entity_id, 0, _axis_vector(1), *offsets[0], content_hash)
        self._insert_chunk(entity_id, 1, _axis_vector(0), *offsets[1], content_hash)
        self._insert_chunk(entity_id, 2, _axis_vector(1), *offsets[2], content_hash)
        self._insert_chunk(entity_id, 3, _axis_vector(1), *offsets[3], content_hash)

        with patch.object(embedding_service, "embed_query_texts", return_value=[_axis_vector(0)]):
            result = get_relevance_preview_data("query", [entity_id], self.db_path)

        preview = result[entity_id]["text"]
        self.assertIn("chunk-0-", preview)
        self.assertIn("chunk-1-", preview)
        self.assertLess(preview.index("chunk-0-"), preview.index("chunk-1-"))

    def test_adjacent_selected_spans_merge_into_one_excerpt(self):
        entity_id = "adjacent-selected-chunks"
        content, offsets = self._sections(10)
        content_hash = "merge-current"
        self._insert_entity(entity_id, content, content_hash)
        for chunk_index, (start, end) in enumerate(offsets):
            vector = _axis_vector(0) if chunk_index in (1, 2) else _axis_vector(1)
            self._insert_chunk(entity_id, chunk_index, vector, start, end, content_hash)

        with patch.object(embedding_service, "embed_query_texts", return_value=[_axis_vector(0)]):
            result = get_relevance_preview_data("query", [entity_id], self.db_path)

        preview = result[entity_id]["text"]
        self.assertNotIn(SNIPPET_ELLIPSIS, preview)
        self.assertEqual(preview.count("chunk-1-"), 1)
        self.assertEqual(preview.count("chunk-2-"), 1)

    def test_zero_chunk_and_stale_only_candidates_are_absent(self):
        fresh_id = "fresh-candidate"
        zero_id = "zero-chunk-candidate"
        stale_id = "stale-only-candidate"
        self._insert_entity(fresh_id, "fresh content", "fresh-hash")
        self._insert_entity(zero_id, "no chunks", "zero-hash")
        self._insert_entity(stale_id, "stale content", "current-hash")
        self._insert_chunk(fresh_id, 0, _axis_vector(0), 0, 13, "fresh-hash")
        self._insert_chunk(stale_id, 0, _axis_vector(0), 0, 13, "old-hash")

        with patch.object(embedding_service, "embed_query_texts", return_value=[_axis_vector(0)]):
            result = get_relevance_preview_data(
                "query", [fresh_id, zero_id, stale_id], self.db_path
            )

        self.assertIn(fresh_id, result)
        self.assertNotIn(zero_id, result)
        self.assertNotIn(stale_id, result)

    def test_embedding_failure_is_fail_soft(self):
        with patch.object(
            embedding_service,
            "embed_query_texts",
            side_effect=RuntimeError("forced preview embedding failure"),
        ):
            result = get_relevance_preview_data("query", ["candidate"], self.db_path)

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
