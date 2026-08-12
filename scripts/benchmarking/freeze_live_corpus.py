"""Derive immutable bakeoff corpus artifacts from an MCP snapshot response.

This module never opens SALTMDB's database.  Its sole input is the JSON returned by
``export_corpus_snapshot`` and its outputs are run-private, content-addressed artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from bakeoff_state import fingerprint, sign_artifact, validate_corpus_manifest
from retrieval_architecture import (
    FASTEMBED_DENSE_CANDIDATES,
    FASTEMBED_LATE_INTERACTION_CANDIDATES,
)

REPRESENTATION_VERSION = "normalized_whitespace_v1+body_chunks_chars_1200_200_v1"
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200
FLOAT_BYTES = 4


class CorpusFreezeError(ValueError):
    """The exported snapshot is incomplete, inconsistent, or malformed."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        raise CorpusFreezeError("title and body must be strings")
    return " ".join(value.split())


def _chunks(body: str, title: str) -> list[str]:
    if body:
        step = CHUNK_SIZE - CHUNK_OVERLAP
        return [body[start : start + CHUNK_SIZE] for start in range(0, len(body), step)]
    if title:
        return [title]
    raise CorpusFreezeError("an entity cannot have both an empty title and empty body")


def _require_hash(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise CorpusFreezeError(f"{field} must be a lowercase SHA-256")
    return value


def derive(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if snapshot.get("has_more") or snapshot.get("next_cursor") is not None:
        raise CorpusFreezeError("snapshot must contain the complete final page")
    snapshot_hash = _require_hash(snapshot.get("snapshot_hash"), "snapshot_hash")
    rows = snapshot.get("entities")
    if not isinstance(rows, list) or not rows:
        raise CorpusFreezeError("snapshot entities must be a non-empty list")
    if snapshot.get("entity_count") != len(rows):
        raise CorpusFreezeError("snapshot entity_count does not match the exported rows")
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or ids != sorted(ids) or len(ids) != len(set(ids)):
        raise CorpusFreezeError("snapshot entity IDs must be complete, unique, and ordered")

    exported: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    total_utf8_bytes = 0
    for row in rows:
        entity_id = row["id"]
        title = _normalize(row.get("title"))
        body = _normalize(row.get("body"))
        chunks = _chunks(body, title)
        source_hash = _require_hash(row.get("source_hash"), f"source_hash[{entity_id}]")
        total_utf8_bytes += len(title.encode()) + len(body.encode())
        exported.append(
            {
                "entity_id": entity_id,
                "title": title,
                "body": body,
                "chunks": chunks,
                "source_hash": source_hash,
            }
        )
        manifest_rows.append(
            {
                "entity_id": entity_id,
                "title_hash": _sha256_text(title),
                "body_hash": _sha256_text(body),
                "source_hash": source_hash,
                "chunk_hashes": [_sha256_text(chunk) for chunk in chunks],
            }
        )

    payload = {
        "eligible_ids": ids,
        "entities": manifest_rows,
        "representation_version": REPRESENTATION_VERSION,
    }
    payload["corpus_root_hash"] = fingerprint(payload)
    manifest = sign_artifact("CorpusRepresentationManifest", payload)
    validate_corpus_manifest(manifest)

    corpus_export = {
        "schema_version": 1,
        "snapshot_hash": snapshot_hash,
        "snapshot_provenance": snapshot.get("provenance"),
        "schema_hash": snapshot.get("schema_hash"),
        "database_hash": snapshot.get("database_hash"),
        "corpus_hash": snapshot.get("corpus_hash"),
        "relation_root_hash": snapshot.get("relation_root_hash"),
        "supersedes_edges": snapshot.get("supersedes_edges", []),
        "representation_version": REPRESENTATION_VERSION,
        "entities": exported,
    }

    entity_count = len(exported)
    chunk_count = sum(len(row["chunks"]) for row in exported)
    dense = []
    for model in FASTEMBED_DENSE_CANDIDATES:
        entity_bytes = entity_count * model.dimension * FLOAT_BYTES
        chunk_bytes = chunk_count * model.dimension * FLOAT_BYTES
        dense.append(
            {
                "model_id": model.model_id,
                "declared_revision": model.revision,
                "dimension": model.dimension,
                "entity_vector_count": entity_count,
                "chunk_vector_count": chunk_count,
                "entity_payload_bytes": entity_bytes,
                "chunk_payload_bytes": chunk_bytes,
                "combined_payload_bytes": entity_bytes + chunk_bytes,
            }
        )
    token_vectors = []
    for model in FASTEMBED_LATE_INTERACTION_CANDIDATES:
        maximum_tokens = entity_count * model.max_input_tokens
        token_vectors.append(
            {
                "model_id": model.model_id,
                "declared_revision": model.revision,
                "dimension": model.dimension,
                "entity_document_count": entity_count,
                "maximum_tokens_per_document": model.max_input_tokens,
                "maximum_token_vector_count": maximum_tokens,
                "maximum_payload_bytes": maximum_tokens * model.dimension * FLOAT_BYTES,
                "projection_kind": "exact_declared_cap_upper_bound",
            }
        )
    projection_payload = {
        "snapshot_hash": snapshot_hash,
        "corpus_root_hash": manifest["corpus_root_hash"],
        "representation_version": REPRESENTATION_VERSION,
        "entity_count": entity_count,
        "chunk_count": chunk_count,
        "normalized_text_utf8_bytes": total_utf8_bytes,
        "dense_indexes": dense,
        "late_interaction_indexes": token_vectors,
        "allowances_bytes": {
            "immutable_model_cache_minimum": 12 * 1024**3,
            "artifacts_and_indexes_minimum": 30 * 1024**3,
        },
    }
    projection = sign_artifact("IndexStorageProjection", projection_payload)
    return corpus_export, manifest, projection


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    corpus_export, manifest, projection = derive(snapshot)
    _atomic_write(args.out_dir / "corpus_export.json", corpus_export)
    _atomic_write(args.out_dir / "corpus_representation_manifest.json", manifest)
    _atomic_write(args.out_dir / "index_storage_projection.json", projection)
    print(json.dumps({
        "snapshot_hash": corpus_export["snapshot_hash"],
        "corpus_root_hash": manifest["corpus_root_hash"],
        "entity_count": projection["entity_count"],
        "chunk_count": projection["chunk_count"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
