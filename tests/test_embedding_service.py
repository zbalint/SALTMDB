import unittest
import os
import tempfile
import shutil
from unittest.mock import MagicMock, patch

import numpy as np

from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.embedding_service import (
    _is_valid_local_model,
    embed_text,
    embed_texts,
    embed_texts_in_batches,
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


class TestEmbedTextsInBatches(unittest.TestCase):
    def test_empty_list_returns_empty_list(self):
        self.assertEqual(embed_texts_in_batches([]), [])

    def test_calls_embed_texts_in_bounded_batches_and_preserves_order(self):
        calls = []

        def fake_embed_texts(texts):
            calls.append(list(texts))
            return [[float(len(t))] for t in texts]

        texts = [f"row-{i}" for i in range(10)]
        with patch(
            "saltmdb.domain.services.embedding_service.embed_texts",
            side_effect=fake_embed_texts,
        ):
            result = embed_texts_in_batches(texts, batch_size=4)

        self.assertEqual(calls, [texts[0:4], texts[4:8], texts[8:10]])
        self.assertEqual(result, [[float(len(t))] for t in texts])

    def test_never_calls_embed_texts_with_more_than_batch_size_items(self):
        calls = []

        def fake_embed_texts(texts):
            calls.append(len(texts))
            return [[0.0] for _ in texts]

        with patch(
            "saltmdb.domain.services.embedding_service.embed_texts",
            side_effect=fake_embed_texts,
        ):
            embed_texts_in_batches([f"x{i}" for i in range(75)], batch_size=32)

        self.assertEqual(calls, [32, 32, 11])
        self.assertTrue(all(n <= 32 for n in calls))

    def test_default_batch_size_matches_config_constant(self):
        from saltmdb.config import EMBEDDING_BATCH_SIZE

        calls = []

        def fake_embed_texts(texts):
            calls.append(len(texts))
            return [[0.0] for _ in texts]

        with patch(
            "saltmdb.domain.services.embedding_service.embed_texts",
            side_effect=fake_embed_texts,
        ):
            embed_texts_in_batches([f"x{i}" for i in range(EMBEDDING_BATCH_SIZE + 5)])

        self.assertEqual(calls, [EMBEDDING_BATCH_SIZE, 5])


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

    def test_large_content_never_sends_embed_texts_more_than_batch_size_chunks(self):
        """Regression test for the unbounded single embed_texts() call that previously caused a
        host freeze (Gate D bakeoff) and a WSL2 OOM (Needle evaluation) elsewhere in this
        project -- compute_entity_chunk_embeddings must route through embed_texts_in_batches,
        never call embed_texts directly with an entity's full chunk list.
        """
        from saltmdb.config import EMBEDDING_BATCH_SIZE

        full_content = "".join(f"paragraph-{i:05d} " for i in range(4000))  # ~60,000 chars
        batch_sizes = []

        def fake_embed_texts(texts):
            batch_sizes.append(len(texts))
            return [[0.0] * 384 for _ in texts]

        with patch(
            "saltmdb.domain.services.embedding_service.embed_texts",
            side_effect=fake_embed_texts,
        ):
            rows = compute_entity_chunk_embeddings("entity-4", full_content)

        self.assertGreater(len(batch_sizes), 1, "content should require more than one batch")
        self.assertTrue(all(n <= EMBEDDING_BATCH_SIZE for n in batch_sizes))
        self.assertEqual(sum(batch_sizes), len(rows))


class TestGetModelArenaOption(unittest.TestCase):
    saved_model: object | None = None

    def setUp(self):
        self.saved_model = embedding_service._model
        embedding_service._model = None

    def tearDown(self):
        embedding_service._model = self.saved_model

    def test_get_model_uses_bundled_model_with_arena_disabled(self):
        with (
            patch.object(embedding_service, "_is_valid_local_model", return_value=True),
            patch("fastembed.TextEmbedding") as mock_ctor,
        ):
            embedding_service.get_model()

        mock_ctor.assert_called_once()
        self.assertIs(mock_ctor.call_args.kwargs.get("enable_cpu_mem_arena"), False)
        self.assertIs(mock_ctor.call_args.kwargs["local_files_only"], True)

    def test_get_model_falls_back_to_online_when_bundle_invalid(self):
        with (
            patch.object(embedding_service, "_is_valid_local_model", return_value=False),
            patch("fastembed.TextEmbedding") as mock_ctor,
        ):
            embedding_service.get_model()

        mock_ctor.assert_called_once_with(
            model_name="BAAI/bge-small-en-v1.5", enable_cpu_mem_arena=False
        )

    def test_get_model_falls_back_to_online_when_bundled_load_raises(self):
        with (
            patch.object(embedding_service, "_is_valid_local_model", return_value=True),
            patch("fastembed.TextEmbedding") as mock_ctor,
        ):
            mock_ctor.side_effect = [RuntimeError("corrupt bundle"), MagicMock()]
            embedding_service.get_model()

        self.assertEqual(mock_ctor.call_count, 2)
        for constructor_call in mock_ctor.call_args_list:
            self.assertIs(constructor_call.kwargs.get("enable_cpu_mem_arena"), False)


if __name__ == "__main__":
    unittest.main()
