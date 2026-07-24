import unittest
import os
import tempfile
import shutil
from unittest.mock import patch, MagicMock

from saltmdb.domain.services.embedding_service import _is_valid_local_model, get_model, embed_text

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
            f.write("version https://git-lfs.github.com/spec/v1\noid sha256:123456\nsize 66465124\n")
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

if __name__ == "__main__":
    unittest.main()
