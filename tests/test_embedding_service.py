import unittest
import os
import tempfile
import shutil

import numpy as np

from saltmdb.domain.services.embedding_service import (
    _is_valid_local_model,
    embed_text,
    embed_texts,
    compute_entity_chunk_embeddings,
)


class TestEmbeddingService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_is_valid_local_model_nonexistent(self):
        self.assertFalse(_is_valid_local_model(os.path.join(self.temp_dir, "nonexistent")))

    def test_is_valid_local_model_missing_onnx(self):
        model_dir = os.path.join(self.temp_dir, "model_dir")
        os.makedirs(model_dir, exist_ok=True)
        self.assertFalse(_is_valid_local_model(model_dir))

    def test_is_valid_local_model_lfs_pointer_too_small(self):
        model_dir = os.path.join(self.temp_dir, "model_dir")
        os.makedirs(model_dir, exist_ok=True)
        onnx_path = os.path.join(model_dir, "model_optimized.onnx")
        with open(onnx_path, "w") as f:
            f.write(
                "version https://git-lfs.github.com/spec/v1\noid sha256:123456\nsize 66465124\n"
            )
        self.assertFalse(_is_valid_local_model(model_dir))

    def test_is_valid_local_model_valid_size(self):
        model_dir = os.path.join(self.temp_dir, "model_dir")
        os.makedirs(model_dir, exist_ok=True)
        onnx_path = os.path.join(model_dir, "model_optimized.onnx")
        with open(onnx_path, "wb") as f:
            f.seek(11 * 1024 * 1024 - 1)
            f.write(b"\0")
        self.assertTrue(_is_valid_local_model(model_dir))

    def test_real_embedding_generation(self):
        vec = embed_text("Hello SALTMDB embedding model test")
        self.assertEqual(len(vec), 384)
        self.assertIsInstance(vec[0], float)


class TestEmbedTexts(unittest.TestCase):
    def test_empty_list_returns_empty_list(self):
        self.assertEqual(embed_texts([]), [])

    def test_mixed_empty_and_real_strings_preserve_alignment(self):
        results = embed_texts(["", "   ", "real content for embedding"])
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0], [0.0] * 384)
        self.assertEqual(results[1], [0.0] * 384)
        self.assertEqual(len(results[2]), 384)
        self.assertTrue(any(v != 0.0 for v in results[2]))

    def test_batched_equivalent_to_looped_embed_text(self):
        texts = ["alpha memory content", "beta memory content", "gamma memory content"]
        batched = embed_texts(texts)
        looped = [embed_text(t) for t in texts]
        for batched_vec, looped_vec in zip(batched, looped):
            self.assertTrue(
                np.allclose(np.array(batched_vec), np.array(looped_vec), atol=1e-5),
                "Batched embed_texts() must be numerically equivalent to looped embed_text() calls",
            )


class TestComputeEntityChunkEmbeddings(unittest.TestCase):
    def test_short_content_produces_one_chunk(self):
        rows = compute_entity_chunk_embeddings("entity-1", "Short content, one chunk expected.")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "entity-1::0")
        self.assertEqual(rows[0]["entity_id"], "entity-1")
        self.assertEqual(rows[0]["chunk_index"], 0)
        self.assertEqual(rows[0]["char_start"], 0)
        self.assertEqual(len(rows[0]["embedding"]), 384)

    def test_long_content_produces_sequential_chunks_with_verifiable_offsets(self):
        from saltmdb.config import CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
        from saltmdb.utils.chunking import chunk_text

        full_content = "".join(f"paragraph-{i:04d} " for i in range(200))  # > 2400 chars
        self.assertGreater(len(full_content), 2400)
        rows = compute_entity_chunk_embeddings("entity-2", full_content)
        expected_chunks = chunk_text(full_content, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        self.assertGreater(len(rows), 1)
        self.assertEqual(len(rows), len(expected_chunks))
        for i, (r, ec) in enumerate(zip(rows, expected_chunks)):
            self.assertEqual(r["id"], f"entity-2::{i}")
            self.assertEqual(r["chunk_index"], i)
            self.assertEqual(r["char_start"], ec["char_start"])
            self.assertEqual(r["char_end"], ec["char_end"])
            self.assertLessEqual(r["char_end"], len(full_content))

    def test_empty_full_content_produces_no_chunks(self):
        self.assertEqual(compute_entity_chunk_embeddings("entity-3", ""), [])
        self.assertEqual(compute_entity_chunk_embeddings("entity-3", "   "), [])
        self.assertEqual(compute_entity_chunk_embeddings("entity-3", None), [])


if __name__ == "__main__":
    unittest.main()
