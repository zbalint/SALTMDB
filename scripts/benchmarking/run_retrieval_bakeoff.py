"""Execute one frozen, local-only Gate-D retrieval cell.

The command consumes a signed BakeoffSpec, CorpusRepresentationManifest, a frozen query manifest,
frozen corpus text export, and verified local ModelLock.  It never downloads a model.  Development
queries are loaded from ``--queries-dev`` as before.  Blind queries must be supplied with
``--queries-blind`` and are authorized through the winner-specific BlindUnlock and
BlindQueryManifestReceipt before the blind path is read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # POSIX-only hard memory ceiling; see _apply_memory_ceiling below.
    import resource
except ImportError:  # pragma: no cover - Windows fallback has no enforced ceiling.
    resource = None  # type: ignore[assignment]

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "src"))

from bakeoff_state import (  # noqa: E402
    BakeoffContractError,
    authorize_blind_file,
    sha256_bytes,
    sign_artifact,
    validate_bakeoff_spec,
    validate_blind_manifest_receipt,
    validate_blind_unlock,
    validate_corpus_manifest,
    validate_development_winner,
    validate_model_lock as validate_model_lock_artifact,
    validate_signed_artifact,
)
from build_evaluation_queries import (  # noqa: E402
    artifact_fingerprint,
    load_manifest,  # noqa: F401
    validate_queries,
    verify_artifact_fingerprint,
)
from evaluation_artifacts import validate_provenance  # noqa: E402
from lexical_adapter import bm25_search, include_current_heads  # noqa: E402
from materialize_model_locks import PINNED_MODELS  # noqa: E402
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_lexical_snapshot_receipt(
    receipt: Mapping[str, Any], *, db_path: Path, expected_corpus_root: str
) -> dict[str, Any]:
    """Validate the immutable lexical SQLite input before raw-production execution."""
    try:
        value = validate_signed_artifact(receipt, kind="LexicalSnapshotReceipt")
    except (BakeoffContractError, ValueError) as exc:
        raise RetrievalBakeoffError(str(exc)) from exc
    if value.get("corpus_root_hash") != expected_corpus_root:
        raise RetrievalBakeoffError("lexical snapshot receipt corpus root does not match manifest")
    receipt_path = Path(str(value.get("db_path", ""))).expanduser()
    if not receipt_path.is_absolute():
        receipt_path = (Path.cwd() / receipt_path).resolve()
    if receipt_path != db_path.expanduser().resolve():
        raise RetrievalBakeoffError("lexical snapshot receipt db_path does not match --db-path")
    expected_sha = value.get("db_sha256_informational")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise RetrievalBakeoffError("lexical snapshot receipt lacks db_sha256_informational")
    actual_sha = _sha256_file(db_path)
    if actual_sha != expected_sha:
        raise RetrievalBakeoffError("lexical snapshot database SHA-256 does not match receipt")
    return value


BLIND_WINNER_ID = "late_interaction:answerdotai/answerai-colbert-small-v1:entity"
BLIND_BASELINE_ID = "lexical:bm25"
BLIND_BINDING_FIELDS = (
    "authorized_query_manifest_fingerprint",
    "blind_manifest_receipt_fingerprint",
    "blind_manifest_file_sha256",
)


def _load_authorized_blind_manifest(
    payload: bytes, *, expected_count: int = 800, expected_corpus_fingerprint: str | None = None
) -> dict[str, Any]:
    """Validate an already-authorized blind manifest without reopening its path.

    ``authorize_blind_file`` deliberately returns bytes only after validating the unlock and
    receipt.  Keeping parsing here byte-based prevents a future caller from accidentally falling
    back to ``load_manifest(path)`` and reading blind material before authorization.
    """
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalBakeoffError("authorized blind manifest is not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("queries"), list):
        raise RetrievalBakeoffError("authorized blind manifest must contain a queries list")
    try:
        verify_artifact_fingerprint(value, field="manifest_fingerprint")
        if value.get("queries_fingerprint") != artifact_fingerprint(value["queries"]):
            raise ValueError("query manifest queries_fingerprint mismatch")
        validate_provenance(value, artifact_label="blind query manifest")
        validate_queries(value["queries"])
    except ValueError as exc:
        raise RetrievalBakeoffError(str(exc)) from exc
    queries = value["queries"]
    if (
        expected_corpus_fingerprint is not None
        and value.get("corpus_fingerprint") != expected_corpus_fingerprint
    ):
        raise RetrievalBakeoffError("blind query manifest corpus_fingerprint does not match spec")
    if len(queries) != expected_count:
        raise RetrievalBakeoffError(
            f"blind query manifest must contain exactly {expected_count} queries"
        )
    if any(query.get("split") != "blind" for query in queries):
        raise RetrievalBakeoffError("blind query manifest contains a non-blind query")
    return value


def load_query_manifest(
    path: Path,
    *,
    split: str,
    vault_dir: Path | None = None,
    spec_path: Path | None = None,
    winner_path: Path | None = None,
    unlock_path: Path | None = None,
    manifest_receipt_path: Path | None = None,
    expected_corpus_fingerprint: str | None = None,
) -> dict[str, Any]:
    return load_query_manifest_with_hash(
        path,
        split=split,
        vault_dir=vault_dir,
        spec_path=spec_path,
        winner_path=winner_path,
        unlock_path=unlock_path,
        manifest_receipt_path=manifest_receipt_path,
        expected_corpus_fingerprint=expected_corpus_fingerprint,
    )[0]


def load_query_manifest_with_hash(
    path: Path,
    *,
    split: str,
    vault_dir: Path | None = None,
    spec_path: Path | None = None,
    winner_path: Path | None = None,
    unlock_path: Path | None = None,
    manifest_receipt_path: Path | None = None,
    expected_corpus_fingerprint: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Load a dev manifest or authorize-then-load a blind manifest.

    The blind branch has no fallback: all five custody paths are required and the target path is
    opened only inside ``authorize_blind_file`` after the signed controls validate.
    """
    if split == "dev":
        try:
            payload = path.read_bytes()
            value = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetrievalBakeoffError("development query manifest is not valid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("queries"), list):
            raise RetrievalBakeoffError("development query manifest must contain a queries list")
        try:
            verify_artifact_fingerprint(value, field="manifest_fingerprint")
            if value.get("queries_fingerprint") != artifact_fingerprint(value["queries"]):
                raise ValueError("query manifest queries_fingerprint mismatch")
            validate_provenance(value, artifact_label="query manifest")
            validate_queries(value["queries"])
        except ValueError as exc:
            raise RetrievalBakeoffError(str(exc)) from exc
        if any(query.get("split") != "dev" for query in value["queries"]):
            raise RetrievalBakeoffError("query manifest contains a different split")
        if len(value["queries"]) != 400:
            raise RetrievalBakeoffError(
                "development query manifest must contain exactly 400 queries"
            )
        return value, sha256_bytes(payload)
    if split != "blind":
        raise RetrievalBakeoffError(f"unsupported query split {split!r}")
    controls = (vault_dir, spec_path, winner_path, unlock_path, manifest_receipt_path)
    if any(control is None for control in controls):
        raise RetrievalBakeoffError(
            "blind retrieval requires vault, spec, development winner, unlock, and manifest receipt"
        )
    payload = authorize_blind_file(
        path,
        vault_dir,
        spec_path,
        winner_path,
        unlock_path,
        manifest_receipt_path,
    )
    return (
        _load_authorized_blind_manifest(
            payload, expected_corpus_fingerprint=expected_corpus_fingerprint
        ),
        sha256_bytes(payload),
    )


def _load_blind_binding(
    *,
    query_manifest: Mapping[str, Any],
    spec: Mapping[str, Any],
    winner_path: Path,
    unlock_path: Path,
    receipt_path: Path,
    authorized_payload_sha256: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Validate the already-used controls and return the binding copied into each blind bundle."""
    winner = validate_development_winner(_load_json(winner_path), spec)
    unlock = validate_blind_unlock(_load_json(unlock_path), spec, winner)
    receipt = validate_blind_manifest_receipt(_load_json(receipt_path), spec, winner, unlock)
    if receipt["file_sha256"] != authorized_payload_sha256:
        raise RetrievalBakeoffError(
            "blind manifest receipt hash does not match authorized payload bytes"
        )
    manifest_fingerprint = query_manifest.get("manifest_fingerprint")
    if not isinstance(manifest_fingerprint, str):
        raise RetrievalBakeoffError("blind query manifest lacks a signed manifest_fingerprint")
    return (
        {
            "authorized_query_manifest_fingerprint": manifest_fingerprint,
            "blind_manifest_receipt_fingerprint": receipt["artifact_fingerprint"],
            "blind_manifest_file_sha256": receipt["file_sha256"],
        },
        winner,
    )


def _validate_blind_execution(contender_id: str, binding: Mapping[str, str] | None) -> None:
    """Reject unapproved blind cells and ensure every blind bundle carries custody binding."""
    if binding is None:
        return
    if contender_id not in {BLIND_WINNER_ID, BLIND_BASELINE_ID}:
        raise RetrievalBakeoffError(
            "blind retrieval permits only the selected development winner and lexical:bm25"
        )
    missing = [field for field in BLIND_BINDING_FIELDS if not binding.get(field)]
    if missing:
        raise RetrievalBakeoffError(f"blind retrieval binding is missing: {', '.join(missing)}")


def _blind_bundle_binding(binding: Mapping[str, str] | None) -> dict[str, str]:
    return {field: str(binding[field]) for field in BLIND_BINDING_FIELDS} if binding else {}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RetrievalBakeoffError(f"{path.name} must contain a JSON object")
    return value


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_logical_model_id(source_repository: str) -> str:
    """Reverse-lookup the fastembed-canonical alias for a Gate-A ``source_repository``.

    ``ModelLock.source_repository`` is the literal HF onnx-mirror repo id used at download time
    (e.g. ``"Qdrant/bge-small-en-v1.5-onnx-Q"``).  FastEmbed's ``TextEmbedding``/
    ``LateInteractionTextEmbedding`` only accept their own canonical alias strings (e.g.
    ``"BAAI/bge-small-en-v1.5"``) for ``model_name`` -- passing the raw download repo id fails
    registry validation before any file I/O.  Fails closed rather than silently falling back to
    ``source_repository`` so an unpinned model can never slip into a bakeoff run.
    """
    for pinned in PINNED_MODELS:
        if pinned.source_repository == source_repository:
            return pinned.logical_model_id
    raise RetrievalBakeoffError(
        f"no pinned model in the Gate-A inventory declares source_repository {source_repository!r}"
    )


def adapter_model_lock(artifact: Mapping[str, Any], cache_dir: Path, *, kind: str) -> ModelLock:
    """Translate the signed cross-stage ModelLock into the executable local adapter lock."""
    validated = validate_model_lock_artifact(dict(artifact))
    if kind not in {"dense", "late_interaction"}:
        raise RetrievalBakeoffError("retrieval kind must be dense or late_interaction")
    logical_model_id = _resolve_logical_model_id(validated["source_repository"])
    spec = EmbeddingSpec(
        model_id=logical_model_id,
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
        ModelFile(row["path"], row["sha256"], row["size_bytes"]) for row in validated["files"]
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
    blind_binding: Mapping[str, str] | None = None,
    backend_factory: Any = fastembed_dense_factory,
    batch_size: int | None = None,
    query_binding: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    _validate_blind_execution(f"dense:{lock.spec.model_id}:{channel}", blind_binding)
    adapter = DenseEmbeddingAdapter(lock.spec, lock, backend_factory)
    build_kwargs: dict[str, Any] = {} if batch_size is None else {"batch_size": batch_size}
    with DenseIndexRunner(
        sidecar_path, adapter, representation_root=manifest["corpus_root_hash"]
    ) as index:
        index_receipt = index.build(documents, **build_kwargs)
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
    payload = {
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
    }
    payload.update(_blind_bundle_binding(blind_binding))
    if query_binding:
        payload.update(query_binding)
    return sign_artifact("RetrievalRunBundle", payload)


def execute_late_cell(
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
    documents: Sequence[IndexDocument],
    lock: ModelLock,
    sidecar_path: Path,
    blind_binding: Mapping[str, str] | None = None,
    backend_factory: Any = fastembed_late_interaction_factory,
    batch_size: int | None = None,
    query_binding: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    _validate_blind_execution(f"late_interaction:{lock.spec.model_id}:entity", blind_binding)
    adapter = LateInteractionEmbeddingAdapter(lock.spec, lock, backend_factory)
    build_kwargs: dict[str, Any] = {} if batch_size is None else {"batch_size": batch_size}
    with LateInteractionIndexRunner(
        sidecar_path, adapter, representation_root=manifest["corpus_root_hash"]
    ) as index:
        index_receipt = index.build(documents, **build_kwargs)
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
    payload = {
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
    }
    payload.update(_blind_bundle_binding(blind_binding))
    if query_binding:
        payload.update(query_binding)
    return sign_artifact("RetrievalRunBundle", payload)


def execute_lexical_cell(
    *,
    spec: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
    db_path: Path,
    blind_binding: Mapping[str, str] | None = None,
    lexical_policy: str = "include_current_heads",
    query_binding: Mapping[str, str] | None = None,
    representation_root: str | None = None,
    lexical_snapshot_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if lexical_policy not in {"include_current_heads", "raw_production"}:
        raise RetrievalBakeoffError(f"unsupported lexical policy: {lexical_policy}")
    if lexical_policy == "raw_production" and (
        lexical_snapshot_receipt is None or representation_root is None
    ):
        raise RetrievalBakeoffError(
            "raw_production lexical execution requires a validated snapshot receipt and "
            "representation root"
        )
    _validate_blind_execution(BLIND_BASELINE_ID, blind_binding)
    conn = get_connection(str(db_path))
    try:
        results = []
        for query in queries:
            hits, latency_ms, error = timed_search(bm25_search, conn, query["query"], limit=20)
            ids = [hit.entity_id for hit in hits]
            if lexical_policy == "include_current_heads":
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
    payload = {
        "run_id": spec["run_id"],
        "spec_fingerprint": spec["artifact_fingerprint"],
        "cell": {
            "kind": "lexical",
            "channel": (
                "bm25_plus_current_head"
                if lexical_policy == "include_current_heads"
                else "bm25_raw_production"
            ),
            "lexical_policy": lexical_policy,
            "production_faithful": lexical_policy == "raw_production",
            **(
                {
                    "lexical_snapshot_receipt_fingerprint": lexical_snapshot_receipt[
                        "artifact_fingerprint"
                    ],
                    "lexical_snapshot_db_sha256": lexical_snapshot_receipt[
                        "db_sha256_informational"
                    ],
                }
                if lexical_policy == "raw_production"
                else {}
            ),
            **(
                {"representation_root": representation_root}
                if representation_root is not None
                else (
                    {"representation_root": blind_binding["representation_root"]}
                    if blind_binding is not None and "representation_root" in blind_binding
                    else {}
                )
            ),
        },
        "complete_query_count": len(results) - len(failures),
        "failures": failures,
        "results": results,
    }
    payload.update(_blind_bundle_binding(blind_binding))
    if query_binding:
        payload.update(query_binding)
    return sign_artifact("RetrievalRunBundle", payload)


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    os.replace(temporary, path)


# A single-process hard virtual-memory ceiling.  This is a real operational-safety fix, not a
# tuning knob: a runaway allocation inside this process (e.g. an oversized attention buffer for
# a long entity document, confirmed to reach ~16.6GB at default batch settings on one contender)
# must fail with a clean Python/onnxruntime MemoryError inside this process, not be silently
# accepted by the kernel and then satisfied through swap thrashing that can degrade the entire
# host.  Empirically verified: a deliberate oversized numpy allocation under a 2GB ceiling raises
# MemoryError instantly, with zero measurable host memory pressure.  5500MB leaves headroom above
# this bakeoff's largest known-successful model while staying safely below this host's ~7.7GB
# total RAM once other resident processes (SALTMDB daemon, MCP servers, etc.) are accounted for.
DEFAULT_MEMORY_LIMIT_MB = 5500


def _apply_memory_ceiling(limit_mb: int | None) -> None:
    """Set a hard RLIMIT_AS ceiling on this process before any model/index work begins.

    Enforced unconditionally (every ``--kind``, including the lightweight lexical cell) so the
    protection can never be silently skipped.  ``limit_mb=None`` (Windows, or an explicit opt-out
    via ``--memory-limit-mb 0``) leaves the process unbounded -- a caller must ask for that
    explicitly, it is never the default.
    """
    if limit_mb is None or limit_mb <= 0:
        return
    if resource is None:  # pragma: no cover - non-POSIX platforms have no RLIMIT_AS.
        return
    limit_bytes = limit_mb * 1024 * 1024
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    new_hard = limit_bytes if hard in (resource.RLIM_INFINITY, None) else min(hard, limit_bytes)
    resource.setrlimit(resource.RLIMIT_AS, (min(limit_bytes, new_hard), new_hard))


def _load_cli_queries(
    args: argparse.Namespace, spec: Mapping[str, Any], parser: argparse.ArgumentParser
) -> tuple[dict[str, Any], dict[str, str] | None, dict[str, Any] | None, str]:
    if args.queries_dev is not None:
        query_manifest, payload_sha256 = load_query_manifest_with_hash(
            args.queries_dev, split="dev"
        )
        assert payload_sha256 is not None
        return query_manifest, None, None, payload_sha256
    blind_controls = (
        args.blind_vault_dir,
        args.development_winner,
        args.blind_unlock,
        args.blind_manifest_receipt,
    )
    if any(control is None for control in blind_controls):
        parser.error(
            "--queries-blind requires --blind-vault-dir, --development-winner, "
            "--blind-unlock, and --blind-manifest-receipt"
        )
    query_manifest, authorized_payload_sha256 = load_query_manifest_with_hash(
        args.queries_blind,
        split="blind",
        vault_dir=args.blind_vault_dir,
        spec_path=args.spec,
        winner_path=args.development_winner,
        unlock_path=args.blind_unlock,
        manifest_receipt_path=args.blind_manifest_receipt,
        expected_corpus_fingerprint=spec["corpus_snapshot_hash"],
    )
    blind_binding, winner = _load_blind_binding(
        query_manifest=query_manifest,
        spec=spec,
        winner_path=args.development_winner,
        unlock_path=args.blind_unlock,
        receipt_path=args.blind_manifest_receipt,
        authorized_payload_sha256=authorized_payload_sha256,
    )
    if winner["pipeline"].get("contender_id") != BLIND_WINNER_ID:
        parser.error("signed development winner is not the permitted Gate-D ColBERT winner")
    return query_manifest, blind_binding, winner, authorized_payload_sha256


def _validate_blind_model_cell(
    *,
    contender_id: str,
    lock: ModelLock,
    channel: str,
    winner: Mapping[str, Any],
) -> None:
    pipeline = winner.get("pipeline") or {}
    if contender_id != BLIND_WINNER_ID:
        raise RetrievalBakeoffError("blind retrieval permits only the signed development winner")
    for field, actual in (
        ("kind", "late_interaction"),
        ("channel", channel),
        ("model_id", lock.spec.model_id),
        ("revision", lock.spec.revision),
        ("compatibility_key", lock.spec.compatibility_key()),
    ):
        if pipeline.get(field) != actual:
            raise RetrievalBakeoffError(
                f"blind model cell {field} does not match the signed development winner"
            )


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0915
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    queries_group = parser.add_mutually_exclusive_group(required=True)
    queries_group.add_argument("--queries-dev", type=Path)
    queries_group.add_argument("--queries-blind", type=Path)
    parser.add_argument(
        "--blind-vault-dir",
        type=Path,
        help="Private vault containing the blind manifest; required with --queries-blind.",
    )
    parser.add_argument(
        "--development-winner",
        type=Path,
        help="Signed development winner; required with --queries-blind.",
    )
    parser.add_argument(
        "--blind-unlock",
        type=Path,
        help="Signed winner-specific BlindUnlock; required with --queries-blind.",
    )
    parser.add_argument(
        "--blind-manifest-receipt",
        type=Path,
        help="Signed BlindQueryManifestReceipt; required with --queries-blind.",
    )
    parser.add_argument("--kind", choices=("dense", "late_interaction", "lexical"), required=True)
    parser.add_argument(
        "--lexical-policy",
        choices=("include_current_heads", "raw_production"),
        default="include_current_heads",
        help="Lexical candidate policy; default preserves historical Gate-D behavior.",
    )
    parser.add_argument("--channel", choices=("entity", "chunk"), default="entity")
    parser.add_argument("--corpus-export", type=Path)
    parser.add_argument("--model-lock", type=Path)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument(
        "--lexical-snapshot-receipt",
        type=Path,
        help="Signed LexicalSnapshotReceipt required for --lexical-policy raw_production.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=(
            "Optional execution-time embedding batch size passed through to the index "
            "runner's build(). Omit to keep today's default (no kwarg passed)."
        ),
    )
    parser.add_argument(
        "--memory-limit-mb",
        type=int,
        default=DEFAULT_MEMORY_LIMIT_MB,
        help=(
            "Hard RLIMIT_AS ceiling (megabytes) applied to this process before any model/index "
            f"work begins; a runaway allocation fails cleanly instead of thrashing the host via "
            f"swap. Defaults to {DEFAULT_MEMORY_LIMIT_MB}. Pass 0 to run unbounded (not "
            "recommended)."
        ),
    )
    args = parser.parse_args(argv)
    _apply_memory_ceiling(args.memory_limit_mb)

    spec = validate_bakeoff_spec(_load_json(args.spec))
    manifest = validate_corpus_manifest(_load_json(args.corpus_manifest))
    if manifest["corpus_root_hash"] != spec["corpus_snapshot_hash"]:
        raise RetrievalBakeoffError("corpus representation root does not match spec snapshot")
    query_manifest, blind_binding, blind_winner, query_payload_sha256 = _load_cli_queries(
        args, spec, parser
    )
    queries = query_manifest["queries"]
    if blind_binding is not None:
        blind_binding = dict(blind_binding)
        blind_binding["representation_root"] = manifest["corpus_root_hash"]
    query_binding = (
        {
            "query_manifest_fingerprint": query_manifest["manifest_fingerprint"],
            "query_manifest_file_sha256": query_payload_sha256,
            "query_split": "dev",
        }
        if args.queries_dev is not None
        else None
    )
    if args.kind == "lexical":
        if args.db_path is None:
            parser.error("lexical cell requires --db-path")
        lexical_snapshot_receipt = None
        if args.lexical_policy == "raw_production":
            if args.lexical_snapshot_receipt is None:
                parser.error("raw_production lexical cell requires --lexical-snapshot-receipt")
            lexical_snapshot_receipt = validate_lexical_snapshot_receipt(
                _load_json(args.lexical_snapshot_receipt),
                db_path=args.db_path,
                expected_corpus_root=manifest["corpus_root_hash"],
            )
        result = execute_lexical_cell(
            spec=spec,
            queries=queries,
            db_path=args.db_path,
            blind_binding=blind_binding,
            lexical_policy=args.lexical_policy,
            query_binding=query_binding,
            representation_root=manifest["corpus_root_hash"],
            lexical_snapshot_receipt=lexical_snapshot_receipt,
        )
    else:
        required = (args.corpus_export, args.model_lock, args.model_cache, args.sidecar)
        if any(path is None for path in required):
            parser.error("model cell requires corpus export, model lock, model cache, and sidecar")
        assert args.corpus_export and args.model_lock and args.model_cache and args.sidecar
        lock = adapter_model_lock(_load_json(args.model_lock), args.model_cache, kind=args.kind)
        channel = "entity" if args.kind == "late_interaction" else args.channel
        if blind_binding is not None:
            contender_id = f"{args.kind}:{lock.spec.model_id}:{channel}"
            _validate_blind_execution(contender_id, blind_binding)
            assert blind_winner is not None
            _validate_blind_model_cell(
                contender_id=contender_id,
                lock=lock,
                channel=channel,
                winner=blind_winner,
            )
        documents = load_frozen_documents(_load_json(args.corpus_export), manifest, channel)
        if args.kind == "dense":
            result = execute_dense_cell(
                spec=spec,
                manifest=manifest,
                queries=queries,
                documents=documents,
                lock=lock,
                sidecar_path=args.sidecar,
                channel=channel,
                blind_binding=blind_binding,
                batch_size=args.batch_size,
                query_binding=query_binding,
            )
        else:
            result = execute_late_cell(
                spec=spec,
                manifest=manifest,
                queries=queries,
                documents=documents,
                lock=lock,
                sidecar_path=args.sidecar,
                blind_binding=blind_binding,
                batch_size=args.batch_size,
                query_binding=query_binding,
            )
    _atomic_write(args.out, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BakeoffContractError, RetrievalBakeoffError) as exc:
        raise SystemExit(str(exc)) from exc
