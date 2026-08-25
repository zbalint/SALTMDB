"""Embedding scatterplot endpoint: GET /api/scatterplot."""

import logging
from typing import TYPE_CHECKING

from saltmdb.db.vector_schema import try_load_vector_extension

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)


def _blas_free_pca(X, n_components=2, n_iter=3, seed=42):
    """Randomized power-iteration PCA that avoids heavy LAPACK/BLAS routines.

    On Windows in windowless processes (CREATE_NO_WINDOW), OpenBLAS can crash
    at the native DLL level when called from np.linalg.svd or np.linalg.eigh
    on large matrices. This implementation uses only small sequential dot
    products that do not trigger the problematic native code paths.

    Args:
        X: (n_samples, n_features) centered data matrix (numpy float32/64).
        n_components: Number of principal components to return.
        n_iter: Power-iteration refinement passes (3 is sufficient for PCA).
        seed: Random seed for reproducibility.

    Returns:
        coords: (n_samples, n_components) projected coordinates.
    """
    import numpy as np

    def _gram_schmidt(A):
        # Modified Gram-Schmidt using only elementwise multiply + sum, which
        # numpy evaluates with its own reduction loop rather than dispatching
        # to a BLAS/LAPACK routine. np.linalg.qr (LAPACK DGEQRF) was found to
        # still crash on Windows even with all other LAPACK/BLAS calls removed,
        # so orthonormalization here avoids BLAS entirely, not just LAPACK.
        Qo = np.zeros_like(A)
        for i in range(A.shape[1]):
            v = A[:, i].copy()
            for j in range(i):
                v = v - np.sum(Qo[:, j] * v) * Qo[:, j]
            norm = np.sqrt(np.sum(v * v))
            Qo[:, i] = v / norm if norm > 1e-10 else v
        return Qo

    rng = np.random.default_rng(seed)
    n_samples, n_features = X.shape

    # Random projection matrix: (n_features, n_components)
    Q = rng.standard_normal((n_features, n_components)).astype(X.dtype)

    # Power iteration: repeatedly multiply X @ X.T @ sketch to align with
    # top singular directions. Each step is an (n, k) or (n, n) multiply
    # but n_components is tiny (2), so these are cheap column-wise ops.
    for _ in range(n_iter):
        # Project: (n_samples, n_components)
        Z = X @ Q
        # Back-project: (n_features, n_components)
        Q = _gram_schmidt(X.T @ Z)

    # Final projection onto the orthonormal basis Q
    return X @ Q[:, :n_components]


class ScatterplotMixin(ViewerHandlerProtocol):
    """Provides get_scatterplot(); mixed into the final SALTMDBHandler elsewhere."""

    def get_scatterplot(self):  # noqa: C901
        def _checkpoint(msg):
            # A native-level crash (e.g. OpenBLAS DLL abort) kills the process
            # without raising a Python exception, so ordinary error logging
            # never runs. Log + flush BEFORE each risky step so viewer.log
            # shows the last checkpoint reached even if the process dies
            # immediately after, pinpointing the exact crashing line.
            logger.info("get_scatterplot checkpoint: %s", msg)
            for h in logger.handlers:
                h.flush()
            for h in logging.getLogger().handlers:
                h.flush()

        conn = None
        try:
            _checkpoint("start")
            conn = self.get_db_connection()
            _checkpoint("db connected")
            if not try_load_vector_extension(conn):
                self.send_json({"points": [], "error": "sqlite_vec extension unavailable"})
                return
            _checkpoint("vector extension loaded")
            cursor = conn.execute("""
                SELECT e.id, e.title, e.status, e.owner_id, e.is_core, ee.embedding
                FROM entities e
                JOIN entity_embeddings ee ON e.id = ee.entity_id
                WHERE e.status IN ('raw', 'consolidated') AND e.embedding_status = 'ready'
                LIMIT 500
            """)
            rows = cursor.fetchall()
            _checkpoint(f"fetched {len(rows)} rows")
            if not rows:
                self.send_json({"points": []})
                return

            import numpy as np

            _checkpoint("numpy imported")

            valid_items = []
            vectors = []
            for r in rows:
                blob = r["embedding"]
                if blob:
                    vec = np.frombuffer(blob, dtype=np.float32)
                    if vec.shape[0] == 384:
                        vectors.append(vec)
                        valid_items.append(
                            {
                                "id": r["id"],
                                "title": r["title"] or r["id"][:8],
                                "status": r["status"],
                                "owner_id": r["owner_id"] or "system",
                                "is_core": bool(r["is_core"]),
                            }
                        )

            _checkpoint(f"parsed {len(vectors)} vectors via np.frombuffer")

            if len(vectors) < 2:
                self.send_json({"points": []})
                return

            X = np.vstack(vectors)
            _checkpoint(f"np.vstack done, X.shape={X.shape}")
            X_centered = X - np.mean(X, axis=0)
            _checkpoint("np.mean/centering done")
            # Randomized power-iteration PCA — avoids all LAPACK/BLAS heavy
            # routines (SVD, eigh, matmul on large matrices) that crash
            # OpenBLAS in windowless Windows processes.
            # Uses only small dot products; stable across all platforms.
            coords_2d = _blas_free_pca(X_centered, n_components=2, n_iter=3, seed=42)
            _checkpoint("_blas_free_pca done")

            points = []
            for idx, item in enumerate(valid_items):
                item["x"] = round(float(coords_2d[idx, 0]), 4)
                item["y"] = round(float(coords_2d[idx, 1]), 4)
                points.append(item)

            self.send_json({"points": points})
            _checkpoint("response sent")
        except Exception as e:
            logger.error("Error in get_scatterplot: %s", e, exc_info=True)
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                conn.close()
