"""Execute one frozen, local-only development retrieval cell.

The command consumes a signed BakeoffSpec, CorpusRepresentationManifest, development query
manifest, frozen corpus text export, and verified local ModelLock.  It never downloads a model
and deliberately refuses blind queries; blind execution belongs to the post-winner custody path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "src"))

from bakeoff_state import (  # noqa: E402
    BakeoffContractError,
    sign_artifact,
    validate_bakeoff_spec,
    validate_corpus_manifest,
    validate_model_lock as validate_model_lock_artifact,
)
from build_evaluation_queries import load_manifest  # noqa: E402
from lexical_adapter import bm25_search, include_current_heads  # noqa: E402
from retrieval_adapters import (  # noqa: E402
    DenseEmbeddingAdapter,
    LateInteractionEmbeddingAdapter,
    ModelFile,
    ModelLock,
    fastembed_dense_factory,
    fastembed_late_interaction_factory,
)
from retrieval_architecture import EmbeddingSpec  # noqa: E402
from retrieval_index_runner import (  # noqa: E402
    DenseIndexRunner,
    IndexDocument,
    LateInteractionIndexRunner,
    authoritative_documents,
    timed_search,
)
from saltmdb.db.connection import close_connection, get_connection  # noqa: E402


class RetrievalBakeoffError(ValueError):
    """A frozen retrieval cell is incomplete, stale, or unsafe to execute."""


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RetrievalBakeoffError(f"{path.name} must contain a JSON object")
    return value


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def adapter_model_lock(artifact: Mapping[str, Any], cache_dir: Path, *, kind: str) -> ModelLock:
    """Translate the signed cross-stage ModelLock into the executable local adapter lock."""
    validated = validate_model_lock_artifact(dict(artifact))
    if kind not in {"dense", "late_interaction"}:
        raise RetrievalBakeoffError("retrieval kind must be dense or late_interaction")
    spec = EmbeddingSpec(
        model_id=validated["source_repository"],
        revision=validated["resolved_revision"],
        dimension=validated["dimension"],
        query_prefix=validated["query_prefix"],
        document_prefix=validated["document_prefix"],
        normalization=validated["normalization"],
        tokenizer=validated["source_repository"],
        max_input_tokens=validated["maximum_input_tokens"],
        kind=kind,
    )
    files = tuple(
        ModelFile(row["path"], row["sha256"], row["size_bytes"])
        for row in validated["files"]
    )
    return ModelLock(spec, cache_dir.resolve(), files)


def load_frozen_documents(
    corpus_export: Mapping[str, Any], manifest: Mapping[str, Any], channel: str
) -> list[IndexDocument]:
    """Validate exported authoritative text against the manifest and render one channel."""
    if channel not in {"entity", "chunk"}:
        raise RetrievalBakeoffError("dense channel must be entity or chunk")
    rows = corpus_export.get("entities")
    if not isinstance(rows, list):
        raise RetrievalBakeoffError("corpus export lacks entities")
    by_id = {row.get("entity_id"): row for row in rows if isinstance(row, dict)}
    if set(by_id) != set(manifest["eligible_ids"]):
        raise RetrievalBakeoffError("corpus export eligible set differs from signed manifest")
    manifest_rows = {row["entity_id"]: row for row in manifest["entities"]}
    documents: list[IndexDocument] = []
    for entity_id in manifest["eligible_ids"]:
        row = by_id[entity_id]
        signed = manifest_rows[entity_id]
        if row.get("source_hash") != signed["source_hash"]:
            raise RetrievalBakeoffError(f"source hash mismatch for {entity_id}")
        title = str(row.get("title", ""))
        body = str(row.get("body", ""))
        if _text_hash(title) != signed["title_hash"]:
            raise RetrievalBakeoffError(f"title hash mismatch for {entity_id}")
        if _text_hash(body) != signed["body_hash"]:
            raise RetrievalBakeoffError(f"body hash mismatch for {entity_id}")
        chunks = row.get("chunks")
        if not isinstance(chunks, list) or not all(isinstance(item, str) for item in chunks):
            raise RetrievalBakeoffError(f"chunks are malformed for {entity_id}")
        if len(chunks) != len(signed["chunk_hashes"]):
            raise RetrievalBakeoffError(f"chunk count mismatch for {entity_id}")
        if [_text_hash(item) for item in chunks] != signed["chunk_hashes"]:
            raise RetrievalBakeoffError(f"chunk hash mismatch for {entity_id}")
        rendered = authoritative_documents(
            entity_id,
            title,
            body,
            chunks,
            signed["source_hash"],
            manifest["corpus_root_hash"],
        )
        documents.extend(item for item in rendered if item.channel == channel)
    return documents


def _hits_payload(hits: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "entity_id": hit.entity_id,
            "item_id": hit.item_id,
            "score": hit.score,
            "rank": hit.rank,
        }
        for hit in hits
    ]


def execute_dense_cell(
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
    documents: Sequence[IndexDocument],
    lock: ModelLock,
    sidecar_path: Path,
    channel: str,
    backend_factory: Any = fastembed_dense_factory,
) -> dict[str, Any]:
    adapter = DenseEmbeddingAdapter(lock.spec, lock, backend_factory)
    with DenseIndexRunner(
        sidecar_path, adapter, representation_root=manifest["corpus_root_hash"]
    ) as index:
        index_receipt = index.build(documents)
        results = []
        for query in queries:
            hits, latency_ms, error = timed_search(index.search, query["query"], channel, limit=20)
            results.append(
                {
                    "query_id": query["id"],
                    "top20": _hits_payload(hits),
                    "latency_ms": latency_ms,
                    "failure": error,
                }
            )
    failures = [row for row in results if row["failure"] is not None]
    return sign_artifact(
        "RetrievalRunBundle",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "cell": {
                "model_id": lock.spec.model_id,
                "revision": lock.spec.revision,
                "kind": "dense",
                "channel": channel,
                "compatibility_key": lock.spec.compatibility_key(),
                "representation_root": manifest["corpus_root_hash"],
            },
            "index_receipt": index_receipt,
            "complete_query_count": len(results) - len(failures),
            "failures": failures,
            "results": results,
        },
    )


def execute_late_cell(
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
    documents: Sequence[IndexDocument],
    lock: ModelLock,
    sidecar_path: Path,
    backend_factory: Any = fastembed_late_interaction_factory,
) -> dict[str, Any]:
    adapter = LateInteractionEmbeddingAdapter(lock.spec, lock, backend_factory)
    with LateInteractionIndexRunner(
        sidecar_path, adapter, representation_root=manifest["corpus_root_hash"]
    ) as index:
        index_receipt = index.build(documents)
        results = []
        for query in queries:
            hits, latency_ms, error = timed_search(index.search, query["query"], limit=20)
            results.append(
                {
                    "query_id": query["id"],
                    "top20": _hits_payload(hits),
                    "latency_ms": latency_ms,
                    "failure": error,
                }
            )
    failures = [row for row in results if row["failure"] is not None]
    return sign_artifact(
        "RetrievalRunBundle",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "cell": {
                "model_id": lock.spec.model_id,
                "revision": lock.spec.revision,
                "kind": "late_interaction",
                "channel": "entity",
                "compatibility_key": lock.spec.compatibility_key(),
                "representation_root": manifest["corpus_root_hash"],
            },
            "index_receipt": index_receipt,
            "complete_query_count": len(results) - len(failures),
            "failures": failures,
            "results": results,
        },
    )


def execute_lexical_cell(
    *,
    spec: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
    db_path: Path,
) -> dict[str, Any]:
    conn = get_connection(str(db_path))
    try:
        results = []
        for query in queries:
            hits, latency_ms, error = timed_search(bm25_search, conn, query["query"], limit=20)
            ids = [hit.entity_id for hit in hits]
            ids = include_current_heads(conn, ids, limit=20)
            results.append(
                {
                    "query_id": query["id"],
                    "top20": [
                        {
                            "entity_id": entity_id,
                            "rank": rank,
                            "raw_bm25_score": next(
                                (hit.raw_bm25_score for hit in hits if hit.entity_id == entity_id),
                                None,
                            ),
                            "lifecycle_included": entity_id not in {hit.entity_id for hit in hits},
                        }
                        for rank, entity_id in enumerate(ids, 1)
                    ],
                    "latency_ms": latency_ms,
                    "failure": error,
                }
            )
    finally:
        close_connection(conn)
    failures = [row for row in results if row["failure"] is not None]
    return sign_artifact(
        "RetrievalRunBundle",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "cell": {"kind": "lexical", "channel": "bm25_plus_current_head"},
            "complete_query_count": len(results) - len(failures),
            "failures": failures,
            "results": results,
        },
    )


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--queries-dev", type=Path, required=True)
    parser.add_argument("--kind", choices=("dense", "late_interaction", "lexical"), required=True)
    parser.add_argument("--channel", choices=("entity", "chunk"), default="entity")
    parser.add_argument("--corpus-export", type=Path)
    parser.add_argument("--model-lock", type=Path)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    spec = validate_bakeoff_spec(_load_json(args.spec))
    manifest = validate_corpus_manifest(_load_json(args.corpus_manifest))
    query_manifest = load_manifest(args.queries_dev, expected_split="dev", require_provenance=True)
    queries = query_manifest["queries"]
    if len(queries) != 400:
        raise RetrievalBakeoffError("development runner requires exactly 400 queries")
    if args.kind == "lexical":
        if args.db_path is None:
            parser.error("lexical cell requires --db-path")
        result = execute_lexical_cell(spec=spec, queries=queries, db_path=args.db_path)
    else:
        required = (args.corpus_export, args.model_lock, args.model_cache, args.sidecar)
        if any(path is None for path in required):
            parser.error("model cell requires corpus export, model lock, model cache, and sidecar")
        assert args.corpus_export and args.model_lock and args.model_cache and args.sidecar
        lock = adapter_model_lock(_load_json(args.model_lock), args.model_cache, kind=args.kind)
        channel = "entity" if args.kind == "late_interaction" else args.channel
        documents = load_frozen_documents(
            _load_json(args.corpus_export), manifest, channel
        )
        if args.kind == "dense":
            result = execute_dense_cell(
                spec=spec,
                manifest=manifest,
                queries=queries,
                documents=documents,
                lock=lock,
                sidecar_path=args.sidecar,
                channel=channel,
            )
        else:
            result = execute_late_cell(
                spec=spec,
                manifest=manifest,
                queries=queries,
                documents=documents,
                lock=lock,
                sidecar_path=args.sidecar,
            )
    _atomic_write(args.out, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BakeoffContractError, RetrievalBakeoffError) as exc:
        raise SystemExit(str(exc)) from exc
