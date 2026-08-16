"""Run the pinned ColBERT candidate against the preserved historical blind vault.

The runner reads only the immutable historical working-copy corpus, writes a new isolated
sidecar/result directory, and never opens the live SALTMDB database or changes runtime search.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "src")]

from retrieval_adapters import (  # noqa: E402
    LateInteractionEmbeddingAdapter,
    fastembed_late_interaction_factory,
)
from retrieval_index_runner import (  # noqa: E402
    IndexDocument,
    LateInteractionIndexRunner,
    timed_search,
)
from run_retrieval_bakeoff import adapter_model_lock  # noqa: E402


MODEL_ID = "answerdotai/answerai-colbert-small-v1"
SYSTEM_ID = "late_interaction:answerdotai/answerai-colbert-small-v1:centroid200_entity"
EXPECTED_QUERY_COUNT = 800
CANDIDATE_POOL_SIZE = 200


class HistoricalColbertError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_queries(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HistoricalColbertError("blind manifest is not JSON") from exc
    queries = document.get("queries") if isinstance(document, dict) else None
    if not isinstance(queries, list) or len(queries) != EXPECTED_QUERY_COUNT:
        raise HistoricalColbertError("blind manifest must contain exactly 800 queries")
    ids: set[str] = set()
    for query in queries:
        if not isinstance(query, dict) or query.get("split") != "blind":
            raise HistoricalColbertError("blind manifest contains a non-blind query")
        query_id, text = query.get("id"), query.get("query")
        if not isinstance(query_id, str) or not query_id or query_id in ids:
            raise HistoricalColbertError("blind manifest query ids must be unique")
        if not isinstance(text, str) or not text.strip():
            raise HistoricalColbertError("blind manifest contains an empty query")
        ids.add(query_id)
    return queries, hashlib.sha256(raw).hexdigest()


def load_documents(db_path: Path) -> list[IndexDocument]:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, title, full_content, content_hash FROM entities "
            "WHERE status != 'archived' ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise HistoricalColbertError("frozen corpus contains no searchable entities")
    representation_hash = sha256_file(db_path)
    documents: list[IndexDocument] = []
    for entity_id, title, body, content_hash in rows:
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            raise HistoricalColbertError(f"entity {entity_id} lacks canonical content_hash")
        text = "\n\n".join(part for part in (title, body) if isinstance(part, str) and part.strip())
        if not text:
            raise HistoricalColbertError(f"entity {entity_id} has no retrievable text")
        documents.append(
            IndexDocument(entity_id, entity_id, "entity", text, content_hash, representation_hash)
        )
    return documents


def write_json_atomically(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_checkpoint(
    path: Path, *, query_sha256: str, corpus_sha256: str, model_lock_fingerprint: str
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    if not path.is_file():
        return {}, []
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    identity = {
        "system_id": SYSTEM_ID,
        "query_manifest_sha256": query_sha256,
        "corpus_sha256": corpus_sha256,
        "model_lock_fingerprint": model_lock_fingerprint,
    }
    if any(checkpoint.get(key) != value for key, value in identity.items()):
        raise HistoricalColbertError("scoring checkpoint identity does not match this run")
    rankings, failures = checkpoint.get("rankings"), checkpoint.get("failures")
    if not isinstance(rankings, dict) or not isinstance(failures, list):
        raise HistoricalColbertError("scoring checkpoint has an invalid schema")
    return rankings, failures


def load_or_build_centroids(
    sidecar_path: Path, cache_path: Path, *, sidecar_sha256: str, dimension: int
) -> tuple[np.ndarray, np.ndarray]:
    if cache_path.is_file():
        with np.load(cache_path, allow_pickle=False) as cache:
            if (
                cache["sidecar_sha256"].item() == sidecar_sha256
                and cache["centroids"].ndim == 2
                and cache["centroids"].shape[1] == dimension
            ):
                return cache["item_ids"], cache["centroids"]
    connection = sqlite3.connect(f"file:{sidecar_path.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT item_id, token_count, vectors, vectors_sha256 "
            "FROM token_vectors ORDER BY item_id"
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise HistoricalColbertError("completed ColBERT sidecar has no token vectors")
    item_ids: list[str] = []
    centroids: list[np.ndarray] = []
    for item_id, token_count, vectors, vectors_sha256 in rows:
        if vectors_sha256 != hashlib.sha256(vectors).hexdigest():
            raise HistoricalColbertError("ColBERT sidecar vector checksum mismatch")
        matrix = np.frombuffer(vectors, dtype="<f4").reshape(token_count, dimension)
        item_ids.append(item_id)
        centroids.append(np.mean(matrix, axis=0, dtype=np.float64).astype(np.float32))
    item_id_array = np.asarray(item_ids)
    centroid_array = np.vstack(centroids)
    temporary = cache_path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle,
            sidecar_sha256=np.asarray(sidecar_sha256),
            item_ids=item_id_array,
            centroids=centroid_array,
        )
    temporary.replace(cache_path)
    return item_id_array, centroid_array


def select_centroid_candidates(
    query_tokens: np.ndarray, item_ids: np.ndarray, centroids: np.ndarray
) -> list[str]:
    if len(item_ids) < CANDIDATE_POOL_SIZE:
        raise HistoricalColbertError("historical corpus is smaller than the candidate pool")
    query_centroid = np.mean(np.asarray(query_tokens, dtype=np.float32), axis=0)
    scores = centroids @ query_centroid
    candidate_indices = np.argpartition(scores, -CANDIDATE_POOL_SIZE)[-CANDIDATE_POOL_SIZE:]
    ordered = sorted(
        candidate_indices, key=lambda index: (-float(scores[index]), str(item_ids[index]))
    )
    return [str(item_ids[index]) for index in ordered]


def run(
    *,
    corpus_db: Path,
    expected_corpus_sha256: str,
    queries_path: Path,
    model_lock_path: Path,
    model_cache_dir: Path,
    output_dir: Path,
    resume: bool = False,
) -> dict[str, Any]:
    if output_dir.exists() and not resume:
        raise HistoricalColbertError("output directory must be new")
    if resume and not (output_dir / "colbert_entity_index.sqlite").is_file():
        raise HistoricalColbertError("resume requires an existing ColBERT sidecar")
    actual_corpus_hash = sha256_file(corpus_db)
    if actual_corpus_hash != expected_corpus_sha256:
        raise HistoricalColbertError("frozen corpus SHA-256 does not match its historical manifest")
    queries, query_sha256 = load_queries(queries_path)
    lock_document = json.loads(model_lock_path.read_text(encoding="utf-8"))
    lock = adapter_model_lock(lock_document, model_cache_dir, kind="late_interaction")
    if lock.spec.model_id != MODEL_ID:
        raise HistoricalColbertError("model lock is not the pinned ColBERT candidate")
    adapter = LateInteractionEmbeddingAdapter(lock.spec, lock, fastembed_late_interaction_factory)
    documents = load_documents(corpus_db)
    output_dir.mkdir(parents=True, exist_ok=resume)
    sidecar_path = output_dir / "colbert_entity_index.sqlite"
    checkpoint_path = output_dir / "historical_colbert_scoring_checkpoint.json"
    rankings, failures = load_checkpoint(
        checkpoint_path,
        query_sha256=query_sha256,
        corpus_sha256=actual_corpus_hash,
        model_lock_fingerprint=adapter.compatibility_key,
    )
    completed_ids = set(rankings) | {failure["query_id"] for failure in failures}
    document_hashes = {document.entity_id: document.source_hash for document in documents}
    with LateInteractionIndexRunner(
        sidecar_path, adapter, representation_root=actual_corpus_hash
    ) as index:
        if resume:
            receipt = index.receipt()
            if not receipt["ready"] or receipt["completed_count"] != len(documents):
                raise HistoricalColbertError("resume requires a complete, ready ColBERT sidecar")
        else:
            receipt = index.build(documents, batch_size=16)
        item_ids, centroids = load_or_build_centroids(
            sidecar_path,
            output_dir / "colbert_entity_centroids.npz",
            sidecar_sha256=receipt["sidecar_sha256"],
            dimension=adapter.dimension,
        )
        for ordinal, query in enumerate(queries, start=1):
            query_id = query["id"]
            if query_id in completed_ids:
                continue
            query_tokens = adapter.embed_query(query["query"])
            candidates = select_centroid_candidates(query_tokens, item_ids, centroids)
            hits, latency_ms, error = timed_search(
                index.search_subset, query["query"], candidates, limit=20
            )
            if error is not None or len(hits) != 20:
                failures.append({"query_id": query_id, "error": error or "fewer than 20 hits"})
            else:
                rankings[query_id] = [
                    {
                        "rank": hit.rank,
                        "entity_id": hit.entity_id,
                        "content_hash": document_hashes[hit.entity_id],
                        "score": hit.score,
                        "latency_ms": latency_ms,
                    }
                    for hit in hits
                ]
            completed_ids.add(query_id)
            write_json_atomically(
                checkpoint_path,
                {
                    "system_id": SYSTEM_ID,
                    "query_manifest_sha256": query_sha256,
                    "corpus_sha256": actual_corpus_hash,
                    "model_lock_fingerprint": adapter.compatibility_key,
                    "rankings": rankings,
                    "failures": failures,
                },
            )
            if ordinal % 10 == 0 or ordinal == len(queries):
                print(
                    f"scored {len(completed_ids)}/{len(queries)} historical blind queries",
                    flush=True,
                )
    bundle = {
        "artifact_type": "HistoricalRetrievalBundle",
        "system_id": SYSTEM_ID,
        "query_manifest_sha256": query_sha256,
        "corpus_sha256": actual_corpus_hash,
        "model_lock_fingerprint": adapter.compatibility_key,
        "model_descriptor": lock.spec.to_dict(),
        "query_count": len(queries),
        "complete_query_count": len(rankings),
        "rankings": rankings,
        "failures": failures,
        "sidecar_receipt": receipt,
        "candidate_retrieval": {
            "stage_one": "mean_token_centroid_inner_product",
            "candidate_pool_size": CANDIDATE_POOL_SIZE,
            "stage_two": "exact_float64_colbert_maxsim",
        },
    }
    write_json_atomically(output_dir / "historical_colbert_bundle.json", bundle)
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-db", type=Path, required=True)
    parser.add_argument("--corpus-sha256", required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--model-lock", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Reuse a completed isolated sidecar")
    args = parser.parse_args()
    run(
        corpus_db=args.corpus_db,
        expected_corpus_sha256=args.corpus_sha256,
        queries_path=args.queries,
        model_lock_path=args.model_lock,
        model_cache_dir=args.model_cache,
        output_dir=args.output_dir,
        resume=args.resume,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HistoricalColbertError as exc:
        raise SystemExit(str(exc)) from exc
