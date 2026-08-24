"""Run the public, fresh-development retrieval boundary.

This module is deliberately a narrow adapter around the committed retrieval helpers.  It reads
only caller-supplied public manifests, a validated lexical snapshot, a pinned local BGE-small
ModelLock/cache, and a run-private dense sidecar.  It never opens a live SALTMDB database or any
blind/vault/source-slot path.  Retrieval backends are injectable so contract tests do not need a
database or model runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "src"))

from bakeoff_state import (  # noqa: E402
    BakeoffContractError,
    sign_artifact,
    validate_corpus_manifest,
    validate_model_lock,
)
from fresh_development_protocol import (  # noqa: E402
    ARMS,
    BASELINE_ARM,
    CANDIDATE_ARM,
    FreshDevelopmentError,
    _derive_rankings_from_retrieval,
    validate_fresh_development_spec,
    validate_fresh_query_manifest,
)
from lexical_adapter import bm25_search  # noqa: E402
from retrieval_index_runner import (  # noqa: E402
    DenseIndexRunner,
    IndexDocument,
)
from retrieval_adapters import DenseEmbeddingAdapter, fastembed_dense_factory  # noqa: E402
from run_retrieval_bakeoff import (  # noqa: E402
    adapter_model_lock,
    load_frozen_documents,
    validate_lexical_snapshot_receipt,
)


class FreshRetrievalError(ValueError):
    """A public fresh-retrieval binding or backend contract is invalid."""


PROTECTED_PATH_TERMS = frozenset(
    {"blind", "blindunlock", "blind-result", "source-slot", "source_slots", "vault"}
)
LIVE_DB_NAMES = frozenset({"saltmdb.db", "saltmdb.sqlite", "production.db", "live.db"})
PINNED_BGE_SOURCE = "Qdrant/bge-small-en-v1.5-onnx-Q"
PINNED_BGE_REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _safe_public_path(path: Path, role: str, *, must_exist: bool = False) -> Path:
    """Reject protected/live-looking paths before any caller-owned path is opened."""
    raw = os.fspath(path)
    lowered = raw.replace("\\", "/").lower()
    tokens = {part for part in lowered.split("/") if part}
    if "saltmdb.db" in lowered or ".saltmdb" in tokens or tokens & PROTECTED_PATH_TERMS:
        raise FreshRetrievalError(f"{role} path names protected or live material")
    candidate = Path(raw).expanduser()
    # Check the supplied path and each existing parent before resolving it.  Checking only the
    # resolved result silently dereferences a symlink and defeats the public-boundary check.
    if candidate.is_symlink():
        raise FreshRetrievalError(f"{role} path must not be a symlink")
    parent = candidate.parent
    while parent != parent.parent:
        if parent.exists() and parent.is_symlink():
            raise FreshRetrievalError(f"{role} path has a symlinked parent")
        parent = parent.parent
    resolved = candidate.resolve(strict=False)
    if role == "lexical snapshot database" and resolved.name.lower() in LIVE_DB_NAMES:
        raise FreshRetrievalError("live SALTMDB database paths are forbidden")
    if must_exist and not resolved.exists():
        raise FreshRetrievalError(f"{role} path does not exist")
    return resolved


def _load_json(path: Path, role: str) -> dict[str, Any]:
    safe = _safe_public_path(path, role, must_exist=True)
    try:
        value = json.loads(safe.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreshRetrievalError(f"{role} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FreshRetrievalError(f"{role} must contain a JSON object")
    return value


def _runtime_machine_fingerprint() -> str:
    """Fingerprint the execution machine using a small, documented stable identity tuple."""
    payload = {
        "platform": platform.platform(aliased=True),
        "python": platform.python_version(),
        "machine": platform.machine(),
    }
    return _hash(payload)


def _read_runtime_identity(repo_root: Path) -> dict[str, Any]:
    """Read the immutable Git identity and require no tracked worktree changes."""

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FreshRetrievalError("unable to verify the production Git checkout") from exc
        return result.stdout.strip()

    status = git("status", "--porcelain", "--untracked-files=no")
    if status:
        raise FreshRetrievalError("production checkout has tracked worktree changes")
    return {
        "git_commit": git("rev-parse", "--verify", "HEAD"),
        "git_object_format": git("rev-parse", "--show-object-format"),
        "machine_fingerprint": _runtime_machine_fingerprint(),
    }


def _validate_runtime_identity(
    spec: Mapping[str, Any],
    environment_fingerprint: str,
    probe: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, str]:
    raw_runtime = probe() if probe is not None else _read_runtime_identity(_ROOT)
    if not isinstance(raw_runtime, Mapping):
        raise FreshRetrievalError("runtime identity probe returned a non-object")
    runtime = dict(raw_runtime)
    if any(
        not isinstance(runtime.get(field), str) or not runtime[field]
        for field in ("git_commit", "git_object_format", "machine_fingerprint")
    ):
        raise FreshRetrievalError("runtime identity probe is incomplete")
    if (
        runtime.get("git_commit") != spec["production"]["git_commit"]
        or runtime.get("git_object_format") != spec["production"]["git_object_format"]
    ):
        raise FreshRetrievalError("running checkout does not match frozen production Git identity")
    if runtime.get("machine_fingerprint") != spec["machine_fingerprint"]:
        raise FreshRetrievalError("running machine does not match frozen machine fingerprint")
    if (
        not isinstance(environment_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", environment_fingerprint) is None
        or environment_fingerprint != runtime["machine_fingerprint"]
    ):
        raise FreshRetrievalError("environment fingerprint is not the verified machine SHA-256")
    return {
        "git_commit": runtime["git_commit"],
        "git_object_format": runtime["git_object_format"],
        "machine_fingerprint": runtime["machine_fingerprint"],
    }


def _validate_model_lock_for_bge(lock: Mapping[str, Any], model_cache: Path) -> dict[str, Any]:
    try:
        value = validate_model_lock(lock)
    except (BakeoffContractError, ValueError) as exc:
        raise FreshRetrievalError("invalid BGE ModelLock") from exc
    if (
        value["source_repository"] != PINNED_BGE_SOURCE
        or value["resolved_revision"] != PINNED_BGE_REVISION
    ):
        raise FreshRetrievalError("ModelLock is not the pinned BAAI/bge-small-en-v1.5 model")
    cache = _safe_public_path(model_cache, "BGE model cache", must_exist=True)
    if not cache.is_dir():
        raise FreshRetrievalError("BGE model cache must be a directory")
    return value


def _hit_fields(hit: Any, *, dense: bool) -> tuple[str, float]:
    entity_id = (
        hit.get("entity_id") if isinstance(hit, Mapping) else getattr(hit, "entity_id", None)
    )
    field = "score" if dense else "raw_bm25_score"
    score = hit.get(field) if isinstance(hit, Mapping) else getattr(hit, field, None)
    if not isinstance(entity_id, str) or not entity_id:
        raise FreshRetrievalError("retrieval hit has an invalid entity ID")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        raise FreshRetrievalError("retrieval hit has a non-finite score")
    return entity_id, float(score)


def _normalize_hits(hits: Sequence[Any], *, dense: bool) -> tuple[list[str], list[float]]:
    if isinstance(hits, (str, bytes)) or not isinstance(hits, Sequence):
        raise FreshRetrievalError("retrieval backend must return a finite hit sequence")
    if len(hits) > 20:
        raise FreshRetrievalError("retrieval backend returned more than top-20 hits")
    ids: list[str] = []
    scores: list[float] = []
    for hit in hits:
        entity_id, score = _hit_fields(hit, dense=dense)
        if entity_id in ids:
            raise FreshRetrievalError("retrieval backend returned duplicate entity IDs")
        ids.append(entity_id)
        scores.append(score)
    return ids, scores


def _diagnostic(
    query: Mapping[str, Any], title_index: Mapping[str, Sequence[str]]
) -> dict[str, Any]:
    empty = {
        "triggered": False,
        "match_ids": [],
        "output_id": None,
        "output_rank": None,
        "unique_corpus_match": False,
    }
    if query.get("category") != "exact_title":
        return empty
    # The index stores at most two IDs per title: enough to distinguish a unique match from
    # collision/non-match while keeping the timed fast path equivalent to production LIMIT 2.
    matches = list(title_index.get(query["query"], ()))
    if query.get("subtype") == "unique_byte_exact_singleton":
        if len(matches) != 1 or query.get("source_entity_ids") != matches:
            raise FreshRetrievalError(
                "exact-title singleton does not match one corpus/source identity"
            )
        return {
            "triggered": True,
            "match_ids": matches,
            "output_id": matches[0],
            "output_rank": 1,
            "unique_corpus_match": True,
        }
    if matches:
        raise FreshRetrievalError("byte-mismatch exact-title control has a corpus title match")
    return empty


def _candidate_texts(
    rankings: Mapping[str, Mapping[str, Sequence[str]]], export: Mapping[str, Any]
) -> dict[str, dict[str, dict[str, str]]]:
    rows = {row["entity_id"]: row for row in export.get("entities", []) if isinstance(row, dict)}
    result: dict[str, dict[str, dict[str, str]]] = {}
    query_ids = set(rankings[BASELINE_ARM])
    for query_id in query_ids:
        union = list(
            dict.fromkeys(rankings[BASELINE_ARM][query_id] + rankings[CANDIDATE_ARM][query_id])
        )
        texts: dict[str, dict[str, str]] = {}
        for entity_id in union:
            row = rows.get(entity_id)
            if row is None:
                raise FreshRetrievalError("derived ranking references an absent corpus entity")
            texts[entity_id] = {
                "title": str(row.get("title", "")),
                "full_content": str(row.get("body", "")),
            }
        result[query_id] = texts
    return result


class PublicTimingExecutor:
    """Context-managed production retrieval resources for later end-to-end timing."""

    def __init__(
        self,
        *,
        spec: Mapping[str, Any],
        manifest: Mapping[str, Any],
        documents: Sequence[IndexDocument],
        title_index: Mapping[str, Sequence[str]],
        lexical_search: Callable[[str], Sequence[Any]],
        dense_search: Callable[[str, Sequence[IndexDocument]], Sequence[Any]],
        lexical_conn: Any = None,
        dense_stack: ExitStack | None = None,
    ) -> None:
        self.spec = spec
        self.manifest = manifest
        self.documents = documents
        self.title_index = title_index
        self.lexical_search = lexical_search
        self.dense_search = dense_search
        self._lexical_conn = lexical_conn
        self._dense_stack = dense_stack
        self._closed = False

    def __enter__(self) -> "PublicTimingExecutor":
        if self._closed:
            raise FreshRetrievalError("timing executor has already been closed")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._lexical_conn is not None:
                from saltmdb.db.connection import close_connection

                close_connection(self._lexical_conn)
        finally:
            self._lexical_conn = None
            try:
                if self._dense_stack is not None:
                    self._dense_stack.close()
            finally:
                self._dense_stack = None
                self._closed = True

    def _cell(self, query: Mapping[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise FreshRetrievalError("timing executor is closed")
        exact_title = _diagnostic(query, self.title_index)
        if exact_title["triggered"]:
            return {
                "lexical": {"ids": [], "raw_bm25_scores": []},
                "dense": {"ids": [], "scores": []},
                "exact_title": exact_title,
            }
        try:
            lexical = _normalize_hits(self.lexical_search(query["query"]), dense=False)
            dense = _normalize_hits(self.dense_search(query["query"], self.documents), dense=True)
        except Exception as exc:
            raise FreshRetrievalError("end-to-end retrieval backend failed") from exc
        return {
            "lexical": {"ids": lexical[0], "raw_bm25_scores": lexical[1]},
            "dense": {"ids": dense[0], "scores": dense[1]},
            "exact_title": exact_title,
        }

    def execute_query(self, arm: str, query: Mapping[str, Any]) -> Sequence[str]:
        """Execute one complete fixed arm, including exact-title parity and fusion."""
        if arm not in ARMS:
            raise FreshRetrievalError("unknown timing arm")
        cell = self._cell(query)
        one_manifest = dict(self.manifest)
        one_manifest["queries"] = [dict(query)]
        retrieval = {"cells": {query["id"]: cell}, "fingerprint": _hash({query["id"]: cell})}
        rankings = _derive_rankings_from_retrieval(retrieval, spec=self.spec, manifest=one_manifest)
        return list(rankings[arm][query["id"]])

    def retrieve_cell(self, query: Mapping[str, Any]) -> tuple[dict[str, Any], int, int]:
        """Return one raw cell plus channel-failure counters for the initial evidence pass."""
        if self._closed:
            raise FreshRetrievalError("timing executor is closed")
        exact_title = _diagnostic(query, self.title_index)
        if exact_title["triggered"]:
            return (
                {
                    "lexical": {"ids": [], "raw_bm25_scores": []},
                    "dense": {"ids": [], "scores": []},
                    "exact_title": exact_title,
                },
                0,
                0,
            )
        lexical_failures = dense_failures = 0
        try:
            lexical = _normalize_hits(self.lexical_search(query["query"]), dense=False)
        except FreshRetrievalError:
            raise
        except Exception:
            lexical = ([], [])
            lexical_failures = 1
        try:
            dense = _normalize_hits(self.dense_search(query["query"], self.documents), dense=True)
        except FreshRetrievalError:
            raise
        except Exception:
            dense = ([], [])
            dense_failures = 1
        return (
            {
                "lexical": {"ids": lexical[0], "raw_bm25_scores": lexical[1]},
                "dense": {"ids": dense[0], "scores": dense[1]},
                "exact_title": exact_title,
            },
            lexical_failures,
            dense_failures,
        )


def _public_production_receipt(
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    corpus_manifest: Mapping[str, Any],
    lexical_receipt: Mapping[str, Any],
    model_lock: Mapping[str, Any],
    sidecar_fingerprint: str,
    environment_fingerprint: str,
    runtime_identity: Mapping[str, str],
) -> dict[str, Any]:
    payload = {
        "production": spec["production"],
        "candidate": spec["candidate"],
        "environment_fingerprint": environment_fingerprint,
        "manifest_fingerprint": manifest["artifact_fingerprint"],
        "corpus_manifest_fingerprint": corpus_manifest["artifact_fingerprint"],
        "lexical_snapshot_receipt_fingerprint": lexical_receipt["artifact_fingerprint"],
        "model_lock_fingerprint": model_lock["artifact_fingerprint"],
        "sidecar_fingerprint": sidecar_fingerprint,
        "include_current_heads": False,
        "runtime_identity": dict(runtime_identity),
        "dense_resolved_revision": spec["production"]["dense"]["resolved_revision"],
    }
    return sign_artifact("ProductionConfigReceipt", payload)


def run_public_fresh_retrieval(  # noqa: C901, PLR0912, PLR0915
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    corpus_manifest: Mapping[str, Any],
    corpus_export: Mapping[str, Any],
    lexical_snapshot_receipt: Mapping[str, Any],
    db_path: Path,
    model_lock: Mapping[str, Any],
    model_cache: Path,
    sidecar_path: Path,
    environment_fingerprint: str,
    lexical_search: Callable[[str], Sequence[Any]] | None = None,
    dense_search: Callable[[str, Sequence[IndexDocument]], Sequence[Any]] | None = None,
    dense_backend_factory: Callable[..., Any] | None = None,
    retain_timing_resources: bool = False,
    _runtime_identity_probe: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute public fresh retrieval through injected or production-faithful backends."""
    try:
        frozen_spec = validate_fresh_development_spec(spec)
        frozen_manifest = validate_fresh_query_manifest(manifest, frozen_spec)
        frozen_corpus = validate_corpus_manifest(corpus_manifest)
    except (BakeoffContractError, FreshDevelopmentError, ValueError) as exc:
        raise FreshRetrievalError("fresh retrieval input binding is invalid") from exc
    if frozen_corpus["corpus_root_hash"] != frozen_spec["production"]["corpus_snapshot_hash"]:
        raise FreshRetrievalError("corpus representation root does not match FreshDevelopmentSpec")
    runtime_identity = _validate_runtime_identity(
        frozen_spec, environment_fingerprint, _runtime_identity_probe
    )
    db = _safe_public_path(db_path, "lexical snapshot database", must_exist=True)
    sidecar = _safe_public_path(sidecar_path, "fresh dense sidecar")
    model = _validate_model_lock_for_bge(model_lock, model_cache)
    if (
        model["artifact_fingerprint"]
        != frozen_spec["production"]["dense"]["model_lock_fingerprint"]
    ):
        raise FreshRetrievalError("BGE ModelLock fingerprint does not match the frozen spec")
    if model["resolved_revision"] != frozen_spec["production"]["dense"]["resolved_revision"]:
        raise FreshRetrievalError("ModelLock revision does not match frozen spec")
    try:
        lexical = validate_lexical_snapshot_receipt(
            lexical_snapshot_receipt,
            db_path=db,
            expected_corpus_root=frozen_corpus["corpus_root_hash"],
        )
    except (FreshRetrievalError, ValueError) as exc:
        raise FreshRetrievalError("lexical snapshot binding is invalid") from exc
    try:
        documents = load_frozen_documents(corpus_export, frozen_corpus, "entity")
    except (BakeoffContractError, FreshRetrievalError, ValueError) as exc:
        raise FreshRetrievalError("frozen corpus export does not match its manifest") from exc
    entities = corpus_export.get("entities")
    if not isinstance(entities, list):
        raise FreshRetrievalError("frozen corpus export lacks entities")
    title_index: dict[str, list[str]] = {}
    entity_ids: set[str] = set()
    for row in entities:
        if not isinstance(row, dict):
            continue
        entity_id = row["entity_id"]
        entity_ids.add(entity_id)
        title = str(row.get("title", ""))
        title_index.setdefault(title, [])
        if len(title_index[title]) < 2:
            title_index[title].append(entity_id)
    if entity_ids != set(frozen_corpus["eligible_ids"]):
        raise FreshRetrievalError("frozen corpus export entity IDs are incomplete")
    queries = frozen_manifest["queries"]
    lexical_failures = 0
    dense_failures = 0
    cells: dict[str, dict[str, Any]] = {}
    lexical_conn = None
    dense_stack = ExitStack()
    if lexical_search is None:
        from saltmdb.db.connection import get_connection

        lexical_conn = get_connection(str(db))

        def lexical_search(text: str) -> Sequence[Any]:
            return bm25_search(lexical_conn, text, limit=20)

    if dense_search is None:
        try:
            lock = adapter_model_lock(model, model_cache, kind="dense")
            adapter = DenseEmbeddingAdapter(
                lock.spec,
                lock,
                dense_backend_factory or fastembed_dense_factory,
            )
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            index = dense_stack.enter_context(
                DenseIndexRunner(
                    sidecar,
                    adapter,
                    representation_root=frozen_corpus["corpus_root_hash"],
                )
            )
            index.build(documents)

            def dense_search(text: str, _documents: Sequence[IndexDocument]) -> Sequence[Any]:
                return index.search(text, "entity", limit=20)
        except Exception as exc:
            dense_stack.close()
            if lexical_conn is not None:
                from saltmdb.db.connection import close_connection

                close_connection(lexical_conn)
            raise FreshRetrievalError("pinned BGE dense execution could not start") from exc
    executor = PublicTimingExecutor(
        spec=frozen_spec,
        manifest=frozen_manifest,
        documents=documents,
        title_index=title_index,
        lexical_search=lexical_search,
        dense_search=dense_search,
        lexical_conn=lexical_conn,
        dense_stack=dense_stack,
    )
    try:
        try:
            for query in queries:
                query_id = query["id"]
                cell, lexical_count, dense_count = executor.retrieve_cell(query)
                lexical_failures += lexical_count
                dense_failures += dense_count
                cells[query_id] = cell
        except Exception:
            if retain_timing_resources:
                executor.close()
            raise
    finally:
        if not retain_timing_resources:
            executor.close()
    try:
        retrieval_evidence = {"cells": cells, "fingerprint": _hash(cells)}
        rankings = _derive_rankings_from_retrieval(
            retrieval_evidence, spec=frozen_spec, manifest=frozen_manifest
        )
        sidecar_fingerprint = _sha256_file(sidecar) if sidecar.is_file() else _hash(str(sidecar))
        production_receipt = _public_production_receipt(
            spec=frozen_spec,
            manifest=frozen_manifest,
            corpus_manifest=frozen_corpus,
            lexical_receipt=lexical,
            model_lock=model,
            sidecar_fingerprint=sidecar_fingerprint,
            environment_fingerprint=environment_fingerprint,
            runtime_identity=runtime_identity,
        )
        result = {
            "retrieval_evidence": retrieval_evidence,
            "rankings": rankings,
            "spec_fingerprint": frozen_spec["artifact_fingerprint"],
            "manifest_fingerprint": frozen_manifest["artifact_fingerprint"],
            "candidate_texts": _candidate_texts(rankings, corpus_export),
            "retrieval_channel_failures": {"lexical": lexical_failures, "dense": dense_failures},
            "channel_failures": {arm: lexical_failures + dense_failures for arm in ARMS},
            "production_receipt": production_receipt,
            "configuration_receipt": production_receipt,
            "model_lock_fingerprint": model["artifact_fingerprint"],
            "corpus_manifest_fingerprint": frozen_corpus["artifact_fingerprint"],
        }
        if retain_timing_resources:
            result["timing_executor"] = executor
        return result
    except FreshDevelopmentError as exc:
        if retain_timing_resources:
            executor.close()
        raise FreshRetrievalError("raw retrieval evidence cannot derive protocol rankings") from exc
    except Exception:
        if retain_timing_resources:
            executor.close()
        raise


def run_public_fresh_retrieval_from_paths(
    *,
    spec_path: Path,
    manifest_path: Path,
    corpus_manifest_path: Path,
    corpus_export_path: Path,
    lexical_receipt_path: Path,
    db_path: Path,
    model_lock_path: Path,
    model_cache: Path,
    sidecar_path: Path,
    environment_fingerprint: str,
    lexical_search: Callable[[str], Sequence[Any]] | None = None,
    dense_search: Callable[[str, Sequence[IndexDocument]], Sequence[Any]] | None = None,
    dense_backend_factory: Callable[..., Any] | None = None,
    retain_timing_resources: bool = False,
) -> dict[str, Any]:
    """Load public artifacts only after every supplied path passes the safety boundary."""
    for path, role, must_exist in (
        (spec_path, "FreshDevelopmentSpec", True),
        (manifest_path, "FreshDevelopmentQueryManifest", True),
        (corpus_manifest_path, "corpus representation manifest", True),
        (corpus_export_path, "frozen corpus export", True),
        (lexical_receipt_path, "lexical snapshot receipt", True),
        (db_path, "lexical snapshot database", True),
        (model_lock_path, "BGE ModelLock", True),
        (model_cache, "BGE model cache", True),
        (sidecar_path, "fresh dense sidecar", False),
    ):
        _safe_public_path(path, role, must_exist=must_exist)
    return run_public_fresh_retrieval(
        spec=_load_json(spec_path, "FreshDevelopmentSpec"),
        manifest=_load_json(manifest_path, "FreshDevelopmentQueryManifest"),
        corpus_manifest=_load_json(corpus_manifest_path, "corpus representation manifest"),
        corpus_export=_load_json(corpus_export_path, "frozen corpus export"),
        lexical_snapshot_receipt=_load_json(lexical_receipt_path, "lexical snapshot receipt"),
        db_path=db_path,
        model_lock=_load_json(model_lock_path, "BGE ModelLock"),
        model_cache=model_cache,
        sidecar_path=sidecar_path,
        environment_fingerprint=environment_fingerprint,
        lexical_search=lexical_search,
        dense_search=dense_search,
        dense_backend_factory=dense_backend_factory,
        retain_timing_resources=retain_timing_resources,
    )


def persist_public_retrieval_bundle(
    retrieval_result: Mapping[str, Any], output_path: Path
) -> dict[str, Any]:
    """Persist raw public retrieval evidence only; labels and evaluation are out of scope."""
    if "timing_executor" in retrieval_result:
        raise FreshRetrievalError("close timing resources before persisting a retrieval bundle")
    required = {
        "retrieval_evidence",
        "rankings",
        "candidate_texts",
        "production_receipt",
        "spec_fingerprint",
        "manifest_fingerprint",
    }
    if not required.issubset(retrieval_result):
        raise FreshRetrievalError("retrieval result is incomplete for persistence")
    safe = _safe_public_path(output_path, "public retrieval bundle")
    artifact = sign_artifact(
        "FreshPublicRetrievalBundle",
        {
            key: retrieval_result[key]
            for key in (
                "retrieval_evidence",
                "rankings",
                "candidate_texts",
                "retrieval_channel_failures",
                "channel_failures",
                "production_receipt",
                "spec_fingerprint",
                "manifest_fingerprint",
            )
            if key in retrieval_result
        },
    )
    serialized = (json.dumps(artifact, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    safe.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        # Exclusive creation is the race-safe boundary: an existing first evidence bundle is
        # never replaced, and a failed partial write is removed only when this call created it.
        with safe.open("xb") as handle:
            created = True
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise FreshRetrievalError("public retrieval bundle output already exists") from exc
    except Exception as exc:
        if created:
            try:
                safe.unlink()
            except OSError:
                pass
        raise FreshRetrievalError("public retrieval bundle write failed") from exc
    return artifact


def run_and_persist_public_fresh_retrieval(
    *, output_path: Path, **public_paths: Any
) -> dict[str, Any]:
    """Execute the non-injected public path runner and persist its raw bundle."""
    forbidden = {
        "lexical_search",
        "dense_search",
        "dense_backend_factory",
        "retain_timing_resources",
    }
    if forbidden & set(public_paths):
        raise FreshRetrievalError(
            "persisted public runs cannot use injected backends or retained resources"
        )
    result = run_public_fresh_retrieval_from_paths(**public_paths)
    return persist_public_retrieval_bundle(result, output_path)


def _build_timing_runner(
    retrieval_result: Mapping[str, Any],
    *,
    execute_query: Callable[[str, Mapping[str, Any]], Sequence[str]] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Callable[[str, Mapping[str, Any], str], tuple[Sequence[str], Sequence[float]]]:
    """Build an ``execute_two_arm_development`` runner from public retrieval rankings.

    ``execute_query`` must be backed by persistent, public production resources and must execute
    the complete arm for each invocation (including the corpus-wide exact-title fast path).  A
    ranking dictionary is intentionally insufficient: this boundary must measure end-to-end
    retrieval/fusion work, without labels, blind inputs, or a live database.
    """
    rankings = retrieval_result.get("rankings")
    if not isinstance(rankings, Mapping) or set(rankings) != set(ARMS):
        raise FreshRetrievalError("timing runner requires exactly the two derived arms")
    if not callable(execute_query):
        raise FreshRetrievalError("timing runner requires a persistent end-to-end query callback")

    def run(
        arm: str, query: Mapping[str, Any], phase: str
    ) -> tuple[Sequence[str], Sequence[float]]:
        if arm not in ARMS or phase not in {"warmup", "measure"}:
            raise FreshRetrievalError("timing runner received an unknown arm or phase")
        query_id = query.get("id")
        if not isinstance(query_id, str) or query_id not in rankings[arm]:
            raise FreshRetrievalError("timing runner query is absent from derived rankings")
        sample_count = 1 if phase == "warmup" else 2
        samples: list[float] = []
        ranking: Sequence[str] = ()
        for _ in range(sample_count):
            started = clock()
            ranking = execute_query(arm, query)
            elapsed_ms = (clock() - started) * 1000.0
            if not isinstance(ranking, Sequence) or isinstance(ranking, (str, bytes)):
                raise FreshRetrievalError("end-to-end query callback returned an invalid ranking")
            if list(ranking) != list(rankings[arm][query_id]):
                raise FreshRetrievalError(
                    "end-to-end ranking drifted from frozen retrieval evidence"
                )
            if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
                raise FreshRetrievalError("end-to-end query callback returned invalid timing")
            samples.append(elapsed_ms)
        return list(ranking), samples

    return run


def build_timing_runner(
    retrieval_result: Mapping[str, Any],
    *,
    executor: PublicTimingExecutor | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Callable[[str, Mapping[str, Any], str], tuple[Sequence[str], Sequence[float]]]:
    """Build production timing only from the retained, bound executor itself."""
    if not isinstance(executor, PublicTimingExecutor) or executor._closed:
        raise FreshRetrievalError("production timing requires an open PublicTimingExecutor")
    if (
        retrieval_result.get("spec_fingerprint") != executor.spec["artifact_fingerprint"]
        or retrieval_result.get("manifest_fingerprint") != executor.manifest["artifact_fingerprint"]
    ):
        raise FreshRetrievalError("timing executor is bound to different retrieval inputs")
    return _build_timing_runner(
        retrieval_result,
        execute_query=executor.execute_query,
        clock=clock,
    )


def _build_test_timing_runner(
    retrieval_result: Mapping[str, Any],
    *,
    execute_query: Callable[[str, Mapping[str, Any]], Sequence[str]],
    clock: Callable[[], float] = time.perf_counter,
) -> Callable[[str, Mapping[str, Any], str], tuple[Sequence[str], Sequence[float]]]:
    """Test-only injection seam; production callers must bind PublicTimingExecutor."""
    return _build_timing_runner(retrieval_result, execute_query=execute_query, clock=clock)


__all__ = [
    "FreshRetrievalError",
    "PublicTimingExecutor",
    "build_timing_runner",
    "persist_public_retrieval_bundle",
    "run_and_persist_public_fresh_retrieval",
    "run_public_fresh_retrieval",
    "run_public_fresh_retrieval_from_paths",
]
