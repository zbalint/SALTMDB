"""Historical benchmark script, not part of the maintained test suite.

Round 1 of the chunk-size/overlap embedding-throughput benchmark for the memory-core rework's
Foundation phase (see plans/ and SALTMDB memory `5c09effa`). Produced the evidence behind the
settled defaults in src/saltmdb/config.py: CHUNK_SIZE_CHARS=1200, CHUNK_OVERLAP_CHARS=200.

Moved out of tests/ (and off the test_* naming convention) so `python -m unittest discover -s
tests` never collects it: this is a standalone unittest.TestCase that prints throughput/matrix-
cost measurements rather than asserting production behavior, and it depends on scikit-learn,
which is NOT a declared project dependency (see pyproject.toml) -- CI installs via `pip install
.` with no dev extras, so committing this under tests/ would ImportError at collection and break
the entire suite's discovery. Run manually (with scikit-learn installed) via:
    python scripts/benchmarking/benchmark_embedding_speed.py
Frozen as historical evidence -- the benchmarking phase for chunk-size/overlap tuning is closed
(see SALTMDB memory `403bc0a7`); do not re-run as part of any rework phase without new cause.
"""

import unittest
import time
import json
import os
import tracemalloc
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from saltmdb.domain.services import embedding_service


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks = []
    start = 0
    if not text or len(text.strip()) == 0:
        return []
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


class TestEmbeddingSpeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        embedding_service.get_model()
        fixture_path = os.path.join(os.path.dirname(__file__), "sample_texts.json")
        with open(fixture_path, "r") as f:
            cls.texts = json.load(f)

    def test_chunking_and_matrix_speed(self):  # noqa: C901, PLR0915
        settings = [(400, 67), (800, 133), (1200, 200), (2000, 333)]

        print("\n=== CHUNKING & EMBEDDING THROUGHPUT (Averaged over 3 runs) ===")
        all_embeddings = {}
        for size, overlap in settings:
            chunk_counts = []

            # Compute embeddings once to get chunks (and warm up), then we can time it properly
            setting_embeddings = []
            total_chunks = 0

            # Collect chunks first
            all_entity_chunks = []
            for text in self.texts:
                chunks = _chunk_text(text, size, overlap)
                all_entity_chunks.append(chunks)
                chunk_counts.append(len(chunks))
                total_chunks += len(chunks)

            # Time the embedding process over 3 iterations
            embed_times = []
            for _ in range(3):
                start_embed = time.perf_counter()
                current_embeddings = []
                for chunks in all_entity_chunks:
                    entity_embeddings = []
                    for chunk in chunks:
                        vec = embedding_service.embed_text(chunk)
                        entity_embeddings.append(vec)
                    current_embeddings.append(np.array(entity_embeddings))
                embed_times.append(time.perf_counter() - start_embed)
                # Save the last run's embeddings for matrix math
                setting_embeddings = current_embeddings

            all_embeddings[(size, overlap)] = setting_embeddings

            avg_chunks = np.mean(chunk_counts)
            avg_embed_total = np.mean(embed_times)
            avg_embed = (avg_embed_total / total_chunks) if total_chunks > 0 else 0

            print(f"Setting {size}/{overlap}:")
            print(
                f"  Entities: {len(self.texts)} | Chunks per entity: avg={avg_chunks:.1f}, min={min(chunk_counts)}, max={max(chunk_counts)}, total={total_chunks}"
            )
            print(
                f"  Embedding throughput: {avg_embed_total:.4f}s total ({avg_embed:.4f}s avg per chunk)"
            )

        print("\n=== MATRIX COMPUTE COST ===")

        def compute_pair(emb_a, emb_b):
            if len(emb_a) == 0 or len(emb_b) == 0:
                return 0.0
            sim = cosine_similarity(emb_a, emb_b)
            return np.mean(np.max(sim, axis=1))

        # Time a full cluster (all pairs) given N entities
        def time_cluster(embs_subset, track_ram=False):
            if track_ram:
                tracemalloc.start()
            start = time.perf_counter()
            for i in range(len(embs_subset)):
                for j in range(i + 1, len(embs_subset)):
                    compute_pair(embs_subset[i], embs_subset[j])
            end = time.perf_counter()
            peak = None
            if track_ram:
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            return end - start, peak

        for size, overlap in settings:
            embs = all_embeddings[(size, overlap)]

            # Time N=20 Cluster
            cluster_20_time, _ = time_cluster(embs[:20])

            # Time N=50 Cluster
            cluster_50_time, peak_ram_50 = time_cluster(embs[:50], track_ram=True)

            # Time N=100 Cluster
            cluster_100_time, peak_ram_100 = time_cluster(embs[:100], track_ram=True)

            # Time Rerank 1 vs 50
            query_emb = embs[0]
            candidates_emb = embs[1:51]
            start = time.perf_counter()
            for cand in candidates_emb:
                compute_pair(query_emb, cand)
            rerank_time = time.perf_counter() - start

            print(f"Setting {size}/{overlap}:")
            print(
                f"  N=20 Cluster={cluster_20_time:.4f}s | N=50 Cluster={cluster_50_time:.4f}s | N=100 Cluster={cluster_100_time:.4f}s | 1vs50 Rerank={rerank_time:.4f}s"
            )
            print(
                f"  [RAM] Peak for N=50: {peak_ram_50 / 10**6:.3f} MB | Peak for N=100: {peak_ram_100 / 10**6:.3f} MB"
            )


if __name__ == "__main__":
    unittest.main()
