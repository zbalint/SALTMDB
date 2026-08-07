"""Tests for the optional ONNX cross-encoder Stage-2 reranker (roadmap `ba2cf66f` P1#7, design
memos `1fddc04a`/`8115fa4a`). All tests here mock the underlying fastembed model -- no real model
download, matching the design memo's own explicit test list ("Unit-test lazy import,
candidate/input caps, ordering, disable/failure fallback, and evidence integration with a fake
runner")."""

import math
import os
import unittest
from unittest.mock import MagicMock, patch

from saltmdb.domain.services import reranker_service


class TestIsCrossEncoderEnabled(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("SALTMDB_RERANKER_MODEL")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        else:
            os.environ["SALTMDB_RERANKER_MODEL"] = self._orig

    def test_unset_env_var_disabled(self):
        os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        self.assertFalse(reranker_service.is_cross_encoder_enabled())

    def test_empty_string_disabled(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "   "
        self.assertFalse(reranker_service.is_cross_encoder_enabled())

    def test_unsupported_model_name_disabled_and_logs_warning(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "not/a-real-model"
        with self.assertLogs("saltmdb.domain.services.reranker_service", level="WARNING") as cm:
            self.assertFalse(reranker_service.is_cross_encoder_enabled())
        self.assertTrue(any("not/a-real-model" in msg for msg in cm.output))

    def test_supported_model_name_enabled(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        self.assertTrue(reranker_service.is_cross_encoder_enabled())


class TestScorePairs(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("SALTMDB_RERANKER_MODEL")
        reranker_service._model = None
        reranker_service._model_name = None

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        else:
            os.environ["SALTMDB_RERANKER_MODEL"] = self._orig
        reranker_service._model = None
        reranker_service._model_name = None

    def test_disabled_returns_none_without_loading_a_model(self):
        os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        with patch.object(reranker_service, "get_model") as mock_get_model:
            result = reranker_service.score_pairs("query", ["candidate"])
        self.assertIsNone(result)
        # Proves the "lazy" half of "lazy Reranker protocol": get_model() (which does the actual
        # `from fastembed.rerank.cross_encoder import TextCrossEncoder` import) is never even
        # attempted on the disabled path -- score_pairs short-circuits on is_cross_encoder_enabled()
        # before touching any model-loading code.
        mock_get_model.assert_not_called()

    def test_empty_candidates_returns_none(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        self.assertIsNone(reranker_service.score_pairs("query", []))

    def test_enabled_scores_and_returns_list(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        fake_model = MagicMock()
        fake_model.rerank.return_value = [3.0, -1.0]
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            result = reranker_service.score_pairs("query", ["a", "b"])
        self.assertEqual(result, [3.0, -1.0])

    def test_candidate_count_capped(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        from saltmdb.config import CROSS_ENCODER_MAX_CANDIDATES

        many_candidates = [f"candidate {i}" for i in range(CROSS_ENCODER_MAX_CANDIDATES + 5)]
        fake_model = MagicMock()
        fake_model.rerank.return_value = [0.0] * CROSS_ENCODER_MAX_CANDIDATES
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            reranker_service.score_pairs("query", many_candidates)
        called_query, called_candidates = fake_model.rerank.call_args[0]
        self.assertEqual(len(called_candidates), CROSS_ENCODER_MAX_CANDIDATES)
        self.assertEqual(called_candidates, many_candidates[:CROSS_ENCODER_MAX_CANDIDATES])

    def test_candidate_text_truncated(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        from saltmdb.config import CROSS_ENCODER_MAX_CHARS

        long_candidate = "x" * (CROSS_ENCODER_MAX_CHARS + 500)
        fake_model = MagicMock()
        fake_model.rerank.return_value = [0.0]
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            reranker_service.score_pairs("query", [long_candidate])
        _called_query, called_candidates = fake_model.rerank.call_args[0]
        self.assertEqual(len(called_candidates[0]), CROSS_ENCODER_MAX_CHARS)

    def test_query_text_truncated(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        from saltmdb.config import CROSS_ENCODER_MAX_QUERY_CHARS

        long_query = "q" * (CROSS_ENCODER_MAX_QUERY_CHARS + 500)
        fake_model = MagicMock()
        fake_model.rerank.return_value = [0.0]
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            reranker_service.score_pairs(long_query, ["candidate"])
        called_query, _called_candidates = fake_model.rerank.call_args[0]
        self.assertEqual(len(called_query), CROSS_ENCODER_MAX_QUERY_CHARS)

    def test_runner_exception_returns_none(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        fake_model = MagicMock()
        fake_model.rerank.side_effect = RuntimeError("boom")
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            result = reranker_service.score_pairs("query", ["a"])
        self.assertIsNone(result)

    def test_wrong_cardinality_output_returns_none(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        fake_model = MagicMock()
        fake_model.rerank.return_value = [1.0]  # 2 candidates requested, 1 score returned
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            result = reranker_service.score_pairs("query", ["a", "b"])
        self.assertIsNone(result)

    def test_non_numeric_output_returns_none(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        fake_model = MagicMock()
        fake_model.rerank.return_value = ["not-a-number"]
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            result = reranker_service.score_pairs("query", ["a"])
        self.assertIsNone(result)

    def test_nan_output_returns_none(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        fake_model = MagicMock()
        fake_model.rerank.return_value = [math.nan]
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            result = reranker_service.score_pairs("query", ["a"])
        self.assertIsNone(result)

    def test_infinite_output_returns_none(self):
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"
        fake_model = MagicMock()
        fake_model.rerank.return_value = [math.inf]
        with patch.object(reranker_service, "get_model", return_value=fake_model):
            result = reranker_service.score_pairs("query", ["a"])
        self.assertIsNone(result)


class TestBundledModel(unittest.TestCase):
    """Roadmap ba2cf66f P1#7 + user request: the benchmark-winning candidate
    (Xenova/ms-marco-MiniLM-L-6-v2, ~88MB, under the project's 100MB bundling budget) is bundled
    locally under src/saltmdb/models/, mirroring embedding_service.py's bi-encoder bundling
    convention. Every other supported model name stays online-load-only."""

    def setUp(self):
        reranker_service._model = None
        reranker_service._model_name = None

    def tearDown(self):
        reranker_service._model = None
        reranker_service._model_name = None

    def test_bundled_model_file_present_and_plausible_size(self):
        """Live check against the actual repo asset (not mocked) -- a regression guard against
        the bundled file going missing or being replaced by a truncated/LFS-pointer stand-in."""
        self.assertTrue(reranker_service._is_valid_bundled_model())

    def test_is_valid_bundled_model_false_when_file_missing(self):
        with patch.object(reranker_service, "_BUNDLED_MODEL_ONNX_PATH", "/nonexistent/model.onnx"):
            self.assertFalse(reranker_service._is_valid_bundled_model())

    def test_is_valid_bundled_model_false_when_file_too_small(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".onnx") as f:
            f.write(b"not a real model, just an LFS pointer stand-in")
            f.flush()
            with patch.object(reranker_service, "_BUNDLED_MODEL_ONNX_PATH", f.name):
                self.assertFalse(reranker_service._is_valid_bundled_model())

    def test_get_model_uses_bundled_cache_dir_for_bundled_model_name(self):
        with (
            patch.object(reranker_service, "_is_valid_bundled_model", return_value=True),
            patch("fastembed.rerank.cross_encoder.TextCrossEncoder") as mock_ctor,
        ):
            mock_ctor.return_value = MagicMock()
            reranker_service.get_model(reranker_service._BUNDLED_MODEL_NAME)

        mock_ctor.assert_called_once_with(
            model_name=reranker_service._BUNDLED_MODEL_NAME,
            cache_dir=reranker_service._BUNDLED_MODEL_CACHE_DIR,
            local_files_only=True,
        )

    def test_get_model_falls_back_to_online_when_bundle_invalid(self):
        with (
            patch.object(reranker_service, "_is_valid_bundled_model", return_value=False),
            patch("fastembed.rerank.cross_encoder.TextCrossEncoder") as mock_ctor,
        ):
            mock_ctor.return_value = MagicMock()
            reranker_service.get_model(reranker_service._BUNDLED_MODEL_NAME)

        mock_ctor.assert_called_once_with(model_name=reranker_service._BUNDLED_MODEL_NAME)

    def test_get_model_falls_back_to_online_when_bundled_load_raises(self):
        with (
            patch.object(reranker_service, "_is_valid_bundled_model", return_value=True),
            patch("fastembed.rerank.cross_encoder.TextCrossEncoder") as mock_ctor,
        ):
            mock_ctor.side_effect = [RuntimeError("corrupt bundle"), MagicMock()]
            reranker_service.get_model(reranker_service._BUNDLED_MODEL_NAME)

        self.assertEqual(mock_ctor.call_count, 2)
        # Second (fallback) call must be the plain online-load shape, no cache_dir/local_files_only.
        _args, kwargs = mock_ctor.call_args_list[1]
        self.assertEqual(kwargs, {"model_name": reranker_service._BUNDLED_MODEL_NAME})

    def test_get_model_non_bundled_name_never_touches_bundle_logic(self):
        with (
            patch.object(reranker_service, "_is_valid_bundled_model") as mock_valid,
            patch("fastembed.rerank.cross_encoder.TextCrossEncoder") as mock_ctor,
        ):
            mock_ctor.return_value = MagicMock()
            reranker_service.get_model("BAAI/bge-reranker-base")

        mock_valid.assert_not_called()
        mock_ctor.assert_called_once_with(model_name="BAAI/bge-reranker-base")


class TestGetModelLazySingleton(unittest.TestCase):
    def setUp(self):
        reranker_service._model = None
        reranker_service._model_name = None

    def tearDown(self):
        reranker_service._model = None
        reranker_service._model_name = None

    def test_same_model_name_reuses_singleton(self):
        with patch("fastembed.rerank.cross_encoder.TextCrossEncoder") as mock_ctor:
            mock_ctor.return_value = MagicMock()
            reranker_service.get_model("Xenova/ms-marco-MiniLM-L-6-v2")
            reranker_service.get_model("Xenova/ms-marco-MiniLM-L-6-v2")
        self.assertEqual(mock_ctor.call_count, 1)

    def test_different_model_name_triggers_fresh_construct(self):
        with patch("fastembed.rerank.cross_encoder.TextCrossEncoder") as mock_ctor:
            mock_ctor.return_value = MagicMock()
            reranker_service.get_model("Xenova/ms-marco-MiniLM-L-6-v2")
            reranker_service.get_model("BAAI/bge-reranker-base")
        self.assertEqual(mock_ctor.call_count, 2)


if __name__ == "__main__":
    unittest.main()
