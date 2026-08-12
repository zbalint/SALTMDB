"""Canonical paths and integrity helpers for one frozen evaluation run.

Full matrices, labels, packets, and checkpoints belong below the ignored
``scratch/eval_results/<run-id>/`` tree.  Keeping the names in one place prevents a dev and
blind stage from accidentally sharing an input or writing a supposedly private mapping into a
public packet directory.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Stage-1 artifacts are intentionally content-addressed.  Keeping the provenance contract in
# this small, dependency-free module means the corpus/query/matrix/judging stages cannot drift
# into slightly different ad-hoc fingerprint formats.  The values are metadata only; no runtime
# search behavior is selected here.
PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_KEYS = (
    "commit_fingerprint",
    "corpus_fingerprint",
    "query_manifest_fingerprint",
    "random_seed",
    "config_fingerprint",
    "judge_version_fingerprint",
    # These fields make the evaluation envelope useful outside the original Stage-1
    # matrix runner.  ``judge_version_fingerprint`` is retained as a compatibility
    # alias for the rubric identity; both are deliberately signed.
    "rubric_fingerprint",
    "model_fingerprint",
    "machine_fingerprint",
)

PROVENANCE_SIGNATURE_ALGORITHM = "sha256-canonical-json-v1"
RETROSPECTIVE_ONLY_MARKER = "retrospective_only"
TRACE_SCHEMA_VERSION = 1
TRACE_COMPONENTS = (
    "bm25",
    "dense_entity",
    "dense_chunk",
    "late_interaction",
    "retrieval_text",
    "lifecycle",
    "reranker",
)


class StaleArtifactError(ValueError):
    """Raised when an artifact was produced from a different evaluation input set."""


def git_commit_fingerprint(repo_root: Path | None = None) -> str:
    """Return the full repository commit hash, or ``"unknown"`` outside a checkout.

    The fallback is explicit rather than silently using a mutable branch name.  Callers that
    need promotion-grade provenance should reject ``unknown`` via :func:`validate_provenance`.
    """
    root = Path(repo_root or Path(__file__).resolve().parents[2])
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def file_fingerprint(path: Path) -> str:
    """SHA-256 fingerprint for a corpus/config/query source file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def machine_fingerprint() -> str:
    """Stable-enough host marker for diagnostic latency artifacts.

    It is deliberately not used in ranking/promotion decisions.  CI can pass an explicit machine
    identifier to :func:`build_provenance` when a stronger host lock is required.
    """
    value = platform.node().strip()
    return hashlib.sha256(value.encode()).hexdigest() if value else "unknown"


def build_provenance(
    *,
    commit_fingerprint: str | None = None,
    corpus_fingerprint: str | None = None,
    query_manifest_fingerprint: str | None = None,
    random_seed: int | None = None,
    config_fingerprint: str | None = None,
    judge_version_fingerprint: str | None = None,
    machine_fingerprint_value: str | None = None,
    artifact_kind: str | None = None,
    rubric_fingerprint: str | None = None,
    model_fingerprint: str | None = None,
    # ``seed``, ``machine_fingerprint`` and ``model`` are friendly aliases used by
    # newer callers.  Keep the original names above so existing benchmark scripts
    # and signed artifacts remain readable.
    seed: int | None = None,
    machine_fingerprint_marker: str | None = None,
    model: str | None = None,
    # Natural-name aliases make the manifest contract convenient for small callers and
    # fixtures while preserving the explicit *_fingerprint names used by existing scripts.
    commit: str | None = None,
    corpus: str | None = None,
    query: str | None = None,
    config: str | None = None,
    rubric: str | None = None,
    machine: str | None = None,
    machine_fingerprint: str | None = None,
) -> dict:
    """Build the required reproducibility envelope for a Stage-1 artifact.

    ``random_seed`` is kept as an integer (rather than stringifying it) so two producers cannot
    accidentally sign different JSON representations of the same run.  The optional machine and
    artifact-kind fields are diagnostic labels and do not alter the required input identity.
    """
    commit_fingerprint = commit_fingerprint or commit
    corpus_fingerprint = corpus_fingerprint or corpus
    query_manifest_fingerprint = query_manifest_fingerprint or query
    config_fingerprint = config_fingerprint or config
    rubric_fingerprint = rubric_fingerprint or rubric
    machine_marker_alias = machine_fingerprint or machine
    if not commit_fingerprint or not corpus_fingerprint or not query_manifest_fingerprint:
        raise ValueError("provenance commit, corpus, and query fingerprints are required")
    if random_seed is None:
        random_seed = seed
    if random_seed is None or isinstance(random_seed, bool):
        raise ValueError("provenance random_seed/seed must be an integer")
    if config_fingerprint is None or not str(config_fingerprint).strip():
        raise ValueError("provenance config_fingerprint is required")
    # Older callers only knew the judge-version identity.  Treat it as the rubric
    # identity as well; callers that distinguish rubric from judge/provider can pass
    # ``rubric_fingerprint`` explicitly.
    rubric_fingerprint = rubric_fingerprint or judge_version_fingerprint
    judge_version_fingerprint = judge_version_fingerprint or rubric_fingerprint
    if not rubric_fingerprint or not judge_version_fingerprint:
        raise ValueError("provenance rubric/judge version fingerprint is required")
    machine_marker = (
        machine_fingerprint_value
        or machine_fingerprint_marker
        or machine_marker_alias
        or globals()["machine_fingerprint"]()
    )
    model_marker = model_fingerprint or model or "unbound-model"
    values = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "commit_fingerprint": commit_fingerprint,
        "corpus_fingerprint": corpus_fingerprint,
        "query_manifest_fingerprint": query_manifest_fingerprint,
        "random_seed": random_seed,
        "config_fingerprint": config_fingerprint,
        "judge_version_fingerprint": judge_version_fingerprint,
        "rubric_fingerprint": rubric_fingerprint,
        "model_fingerprint": model_marker,
        "machine_fingerprint": machine_marker,
        "signed": True,
        "signature_algorithm": PROVENANCE_SIGNATURE_ALGORITHM,
    }
    if artifact_kind is not None:
        values["artifact_kind"] = artifact_kind
    _validate_provenance_shape(values)
    values["fingerprint"] = artifact_fingerprint(values)
    return values


def _validate_provenance_shape(provenance: object) -> None:
    if not isinstance(provenance, dict):
        raise StaleArtifactError("artifact lacks provenance metadata")
    if provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise StaleArtifactError("unsupported or stale provenance schema")
    missing = [key for key in PROVENANCE_KEYS if key not in provenance]
    if missing:
        raise StaleArtifactError(f"provenance missing required fields: {', '.join(missing)}")
    if any(provenance[key] in (None, "") for key in PROVENANCE_KEYS if key != "random_seed"):
        raise StaleArtifactError("provenance contains an empty identity field")
    if not isinstance(provenance["random_seed"], int) or isinstance(
        provenance["random_seed"], bool
    ):
        raise StaleArtifactError("provenance random_seed must be an integer")
    if provenance.get("signed") is not True:
        raise StaleArtifactError("provenance is not signed")
    if provenance.get("signature_algorithm") != PROVENANCE_SIGNATURE_ALGORITHM:
        raise StaleArtifactError("unsupported provenance signature algorithm")


def validate_provenance(
    artifact: object,
    expected: dict | None = None,
    *,
    artifact_label: str = "artifact",
) -> dict:
    """Validate a provenance envelope and optionally compare it with current run inputs.

    Missing provenance is rejected as stale instead of being silently treated as a compatible
    legacy artifact.  This lets callers explicitly invalidate old artifacts while retaining the
    useful, human-readable failure reason.
    """
    if not isinstance(artifact, dict):
        raise StaleArtifactError(f"{artifact_label} is not an object")
    provenance = artifact.get("provenance")
    _validate_provenance_shape(provenance)
    assert isinstance(provenance, dict)
    unsigned = dict(provenance)
    stored = unsigned.pop("fingerprint", None)
    if not isinstance(stored, str) or stored != artifact_fingerprint(unsigned):
        raise StaleArtifactError(f"{artifact_label} provenance fingerprint mismatch")
    if expected is not None:
        for key, expected_value in expected.items():
            if provenance.get(key) != expected_value:
                raise StaleArtifactError(
                    f"{artifact_label} stale provenance: {key} does not match current run"
                )
    return provenance


def with_provenance(artifact: dict, provenance: dict) -> dict:
    """Return a copy of ``artifact`` signed with a validated provenance envelope."""
    _validate_provenance_shape(provenance)
    result = dict(artifact)
    result["provenance"] = dict(provenance)
    result["provenance_fingerprint"] = provenance["fingerprint"]
    return result


def sign_artifact(
    artifact: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
    *,
    fingerprint_field: str = "artifact_fingerprint",
) -> dict[str, Any]:
    """Return an immutable-by-convention, content-addressed evaluation artifact.

    JSON is mutable at runtime, so immutability here means every consumer verifies the
    signed content before use.  The helper intentionally returns a detached dictionary and
    signs *after* provenance is attached.  ``validate_signed_artifact`` is the corresponding
    fail-closed reader.
    """
    result = dict(artifact)
    if provenance is not None:
        _validate_provenance_shape(provenance)
        result["provenance"] = dict(provenance)
        result["provenance_fingerprint"] = provenance["fingerprint"]
    result.pop(fingerprint_field, None)
    result[fingerprint_field] = artifact_fingerprint(result)
    return result


def validate_signed_artifact(
    artifact: object,
    expected_provenance: Mapping[str, Any] | None = None,
    *,
    fingerprint_field: str = "artifact_fingerprint",
    artifact_label: str = "artifact",
) -> dict[str, Any]:
    """Verify a frozen artifact and its provenance, rejecting stale input by default."""
    if not isinstance(artifact, dict):
        raise StaleArtifactError(f"{artifact_label} is not an object")
    try:
        verify_artifact_fingerprint(artifact, field=fingerprint_field)
    except ValueError as exc:
        raise StaleArtifactError(str(exc)) from exc
    validate_provenance(
        artifact,
        dict(expected_provenance) if expected_provenance else None,
        artifact_label=artifact_label,
    )
    return artifact


def mark_retrospective(
    artifact: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any] | None = None,
    source_manifest_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Mark historical replay output as retrospective-only and never promotion eligible."""
    result = dict(artifact)
    result["evaluation_purpose"] = RETROSPECTIVE_ONLY_MARKER
    result["promotion_eligible"] = False
    result["retrospective_only"] = True
    if source_manifest_fingerprint is not None:
        result["source_manifest_fingerprint"] = source_manifest_fingerprint
    return sign_artifact(result, provenance)


def validate_retrospective_artifact(artifact: object) -> dict[str, Any]:
    """Validate the explicit historical-replay marker and signed content."""
    if not isinstance(artifact, dict):
        raise StaleArtifactError("retrospective artifact is not an object")
    if (
        artifact.get("evaluation_purpose") != RETROSPECTIVE_ONLY_MARKER
        or artifact.get("retrospective_only") is not True
        or artifact.get("promotion_eligible") is not False
    ):
        raise StaleArtifactError("historical replay must be marked retrospective_only")
    return validate_signed_artifact(artifact, artifact_label="retrospective artifact")


def _rows(value: object, *, split: str | None = None) -> list[Mapping[str, Any]]:
    """Normalize a query collection or manifest for split validation."""
    if isinstance(value, Mapping):
        candidate = value.get("queries")
        if not isinstance(candidate, list):
            raise ValueError("manifest must contain a queries list")
        value = candidate
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("query collection must be a list of objects")
    result = [item for item in value]
    if split is not None:
        mismatched = [item.get("id") for item in result if item.get("split") not in (None, split)]
        if mismatched:
            raise ValueError(f"query collection contains rows outside {split}: {mismatched[:3]}")
    return result


def _split_row_id(row: Mapping[str, Any], ids: set[str], label: str, id_key: str) -> str:
    row_id = row.get(id_key)
    if not isinstance(row_id, str) or not row_id.strip() or row_id in ids:
        raise ValueError(f"{label} contains duplicate or invalid {id_key}")
    ids.add(row_id)
    return row_id


def _split_row_text(row: Mapping[str, Any], texts: set[str], label: str, text_key: str) -> None:
    text = row.get(text_key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{label} contains an empty query")
    normalized = " ".join(text.casefold().split())
    if normalized in texts:
        raise ValueError(f"{label} contains duplicate query text")
    texts.add(normalized)


def _split_row_family(
    row: Mapping[str, Any], families: set[str], label: str, family_key: str
) -> None:
    family = row.get(family_key)
    if family is None:
        return
    if not isinstance(family, str) or not family.strip():
        raise ValueError(f"{label} contains an invalid topic family")
    families.add(family)


def _split_row_sources(
    row: Mapping[str, Any], sources: set[str], label: str, source_ids_key: str
) -> None:
    raw_sources = row.get(source_ids_key, [])
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list) or not all(
        isinstance(source, str) and source.strip() for source in raw_sources
    ):
        raise ValueError(f"{label} source IDs must be a list of strings")
    if len(set(raw_sources)) != len(raw_sources):
        raise ValueError(f"{label} contains duplicate source IDs in one query")
    sources.update(raw_sources)


def _collect_split_sets(
    rows: list[Mapping[str, Any]],
    label: str,
    *,
    family_key: str,
    id_key: str,
    text_key: str,
    source_ids_key: str,
) -> tuple[set[str], set[str], set[str], set[str]]:
    ids: set[str] = set()
    texts: set[str] = set()
    families: set[str] = set()
    sources: set[str] = set()
    for row in rows:
        _split_row_id(row, ids, label, id_key)
        _split_row_text(row, texts, label, text_key)
        _split_row_family(row, families, label, family_key)
        _split_row_sources(row, sources, label, source_ids_key)
    return ids, texts, families, sources


def validate_family_disjoint_split(
    dev: object,
    blind: object,
    *,
    family_key: str = "topic_family_id",
    id_key: str = "id",
    text_key: str = "query",
    source_ids_key: str = "source_entity_ids",
) -> dict[str, int]:
    """Validate duplicate rows and enforce a family/source-disjoint dev/blind split.

    The check is deliberately independent of category quotas so it can be applied to small
    synthetic fixtures as well as the full evaluation manifest.  IDs, normalized query text,
    topic families, and explicitly known source IDs may not cross the split boundary.
    """
    dev_rows, blind_rows = _rows(dev, split="dev"), _rows(blind, split="blind")

    dev_sets = _collect_split_sets(
        dev_rows,
        "dev",
        family_key=family_key,
        id_key=id_key,
        text_key=text_key,
        source_ids_key=source_ids_key,
    )
    blind_sets = _collect_split_sets(
        blind_rows,
        "blind",
        family_key=family_key,
        id_key=id_key,
        text_key=text_key,
        source_ids_key=source_ids_key,
    )
    names = ("query IDs", "query text", "topic families", "source entity IDs")
    for name, left, right in zip(names, dev_sets, blind_sets):
        overlap = left & right
        if overlap:
            raise ValueError(f"dev/blind {name} overlap: {sorted(overlap)[:3]}")
    return {
        "dev_queries": len(dev_rows),
        "blind_queries": len(blind_rows),
        "dev_families": len(dev_sets[2]),
        "blind_families": len(blind_sets[2]),
    }


# Friendly aliases for callers that use the shorter contract name.
validate_split_disjoint = validate_family_disjoint_split
validate_family_disjoint = validate_family_disjoint_split


def _normalize_pool_inputs(
    rankings: object,
    candidates: Mapping[str, Any] | Sequence[str] | None,
    known_source_ids: Iterable[str],
) -> tuple[Mapping[str, Sequence[Any]], Mapping[str, Any] | None, Iterable[str]]:
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        if not known_source_ids and all(isinstance(item, str) for item in candidates):
            known_source_ids = candidates
            candidates = None
        else:
            raise ValueError("candidate metadata must be a mapping")
    if not isinstance(rankings, Mapping):
        raise ValueError("rankings must be a mapping keyed by configuration")
    normalized_rankings = cast(Mapping[str, Sequence[Any]], rankings)
    normalized_candidates = cast(Mapping[str, Any] | None, candidates)
    return normalized_rankings, normalized_candidates, known_source_ids


def _pool_metadata_maps(
    candidates: Mapping[str, Any] | None,
) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    if not isinstance(candidates, Mapping):
        return {}, {}
    is_flat = all(
        isinstance(value, Mapping) and ("title" in value or "snippet" in value or "id" in value)
        for value in candidates.values()
    )
    if is_flat:
        return {str(key): value for key, value in candidates.items()}, {}
    return {}, candidates


def _pool_candidate_id(raw_item: Any) -> tuple[str, Mapping[str, Any]]:
    if isinstance(raw_item, Mapping):
        candidate_id = raw_item.get("id") or raw_item.get("candidate_id")
        metadata = raw_item
    else:
        candidate_id = raw_item
        metadata = {}
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("ranking contains an invalid candidate ID")
    return candidate_id, metadata


def _pool_candidate_metadata(
    candidate_id: str,
    metadata: Mapping[str, Any],
    flat_candidates: Mapping[str, Mapping[str, Any]],
    nested_candidates: Mapping[str, Any],
    config_name: str,
) -> dict[str, Any]:
    if not metadata and isinstance(flat_candidates.get(candidate_id), Mapping):
        metadata = flat_candidates[candidate_id]
    elif not metadata:
        nested = nested_candidates.get(config_name, {})
        if isinstance(nested, Mapping) and isinstance(nested.get(candidate_id), Mapping):
            metadata = nested[candidate_id]
    return {
        key: value
        for key, value in metadata.items()
        if key not in {"id", "candidate_id", "rank", "config", "score"}
    }


def _add_ranked_pool_candidates(
    pool: dict[str, dict[str, Any]],
    config_name: str,
    ranked: Sequence[Any],
    *,
    top_k: int,
    flat_candidates: Mapping[str, Mapping[str, Any]],
    nested_candidates: Mapping[str, Any],
) -> None:
    if not isinstance(ranked, Sequence) or isinstance(ranked, (str, bytes)):
        raise ValueError(f"ranking for {config_name!r} must be a sequence")
    for raw_item in ranked[:top_k]:
        candidate_id, metadata = _pool_candidate_id(raw_item)
        if candidate_id in pool:
            continue
        pool[candidate_id] = _pool_candidate_metadata(
            candidate_id,
            metadata,
            flat_candidates,
            nested_candidates,
            config_name,
        )
        pool[candidate_id].setdefault("ground_truth_forced_include", False)


def _add_known_source_ids(pool: dict[str, dict[str, Any]], source_ids: Iterable[str]) -> None:
    for source_id in source_ids:
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("known source IDs must be non-empty strings")
        item = pool.setdefault(source_id, {"ground_truth_forced_include": True})
        item["ground_truth_forced_include"] = True


def build_contender_union_pool(
    rankings: Mapping[str, Sequence[Any]] | Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Any] | Sequence[str] | None = None,
    known_source_ids: Iterable[str] = (),
    *,
    top_k: int = 20,
) -> dict[str, dict[str, Any]]:
    """Pool each contender's top ``top_k`` results and all known source IDs.

    ``rankings`` accepts either ``{config: [id, ...]}`` or ``{config: [{id: ...}, ...]}``.
    Candidate metadata may be a flat ``{id: item}`` map or nested by configuration.  The
    result preserves first-seen deterministic ordering and marks force-included source IDs.
    """
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    rankings, candidates, known_source_ids = _normalize_pool_inputs(
        rankings, candidates, known_source_ids
    )
    flat_candidates, nested_candidates = _pool_metadata_maps(candidates)
    pool: dict[str, dict[str, Any]] = {}
    for config_name in sorted(rankings):
        _add_ranked_pool_candidates(
            pool,
            config_name,
            rankings[config_name],
            top_k=top_k,
            flat_candidates=flat_candidates,
            nested_candidates=nested_candidates,
        )
    _add_known_source_ids(pool, known_source_ids)
    return pool


contender_union_pool = build_contender_union_pool


def build_query_trace(
    query_id: str,
    *,
    config_name: str | None = None,
    bm25: Mapping[str, Any] | None = None,
    dense_entity: Mapping[str, Any] | None = None,
    dense_chunk: Mapping[str, Any] | None = None,
    late_interaction: Mapping[str, Any] | None = None,
    retrieval_text: Mapping[str, Any] | None = None,
    lifecycle: Mapping[str, Any] | None = None,
    reranker: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable per-query retrieval trace envelope.

    Components are intentionally opaque dictionaries: retrieval implementations can add scores
    or candidate counts without changing the schema, while the required component names make a
    benchmark loss attributable to generation, fusion, lifecycle resolution, or reranking.
    """
    if not isinstance(query_id, str) or not query_id.strip():
        raise ValueError("trace query_id must be a non-empty string")
    components = {
        "bm25": dict(bm25 or {}),
        "dense_entity": dict(dense_entity or {}),
        "dense_chunk": dict(dense_chunk or {}),
        "late_interaction": dict(late_interaction or {}),
        "retrieval_text": dict(retrieval_text or {}),
        "lifecycle": dict(lifecycle or {}),
        "reranker": dict(reranker or {}),
    }
    trace: dict[str, Any] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "query_id": query_id,
        "components": components,
        **components,
    }
    if config_name is not None:
        trace["config_name"] = config_name
    trace["trace_fingerprint"] = artifact_fingerprint(trace)
    return trace


def validate_query_trace(trace: object) -> dict[str, Any]:
    """Validate an immutable per-query trace without imposing runtime-specific fields."""
    if not isinstance(trace, dict) or trace.get("schema_version") != TRACE_SCHEMA_VERSION:
        raise ValueError("query trace has unsupported schema")
    query_id = trace.get("query_id")
    if not isinstance(query_id, str) or not query_id:
        raise ValueError("query trace lacks query_id")
    components = trace.get("components")
    if not isinstance(components, dict) or set(components) != set(TRACE_COMPONENTS):
        raise ValueError("query trace lacks required retrieval components")
    if any(not isinstance(components[name], dict) for name in TRACE_COMPONENTS):
        raise ValueError("query trace component must be an object")
    if trace.get("trace_fingerprint") != artifact_fingerprint(
        {key: value for key, value in trace.items() if key != "trace_fingerprint"}
    ):
        raise ValueError("query trace fingerprint mismatch")
    return trace


build_retrieval_trace = build_query_trace


PUBLIC_FILES = {
    "corpus_manifest": "corpus_manifest.json",
    "source_slots": "source_slots.json",
    "queries_dev": "queries_dev.json",
    "queries_blind": "queries_blind.json",
    "config_manifest": "config_manifest.json",
    "matrix_dev": "matrix_dev.json",
    "matrix_blind": "matrix_blind.json",
    "analysis_dev": "analysis_dev.json",
    "analysis_blind": "analysis_blind.json",
    "decision": "final_decision.json",
    "provenance": "provenance_manifest.json",
}


def artifact_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_artifact_fingerprint(value: object, *, field: str = "fingerprint") -> None:
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise ValueError(f"artifact lacks {field}")
    unsigned = dict(value)
    stored = unsigned.pop(field)
    if stored != artifact_fingerprint(unsigned):
        raise ValueError(f"artifact {field} mismatch")


def run_directory(root: Path, run_id: str) -> Path:
    """Return a validated run directory without allowing path traversal."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must be a simple, non-empty filename-safe identifier")
    root = Path(root).resolve()
    path = (root / run_id).resolve()
    if path.parent != root:
        raise ValueError("run_id escapes the evaluation-results root")
    return path


def canonical_artifact_paths(run_dir: Path) -> dict[str, Path]:
    """Return all canonical public and private artifact locations for a run."""
    run_dir = Path(run_dir)
    paths = {name: run_dir / filename for name, filename in PUBLIC_FILES.items()}
    paths.update(
        {
            "public_packets_dev": run_dir / "public_packets" / "dev",
            "public_packets_blind": run_dir / "public_packets" / "blind",
            "private_mappings_dev": run_dir / "private_mappings" / "dev",
            "private_mappings_blind": run_dir / "private_mappings" / "blind",
            "raw_labels_dev": run_dir / "raw_labels" / "dev",
            "raw_labels_blind": run_dir / "raw_labels" / "blind",
            "merged_labels_dev": run_dir / "merged_labels_dev.json",
            "merged_labels_blind": run_dir / "merged_labels_blind.json",
            "arbitration_dev": run_dir / "arbitration_dev.json",
            "arbitration_blind": run_dir / "arbitration_blind.json",
            "checkpoints_dev": run_dir / "checkpoints" / "dev",
            "checkpoints_blind": run_dir / "checkpoints" / "blind",
        }
    )
    return paths


def initialize_run_directory(root: Path, run_id: str) -> dict[str, Path]:
    """Create only the canonical directory skeleton; no evaluation artifact is fabricated."""
    paths = canonical_artifact_paths(run_directory(root, run_id))
    paths["corpus_manifest"].parent.mkdir(parents=True, exist_ok=True)
    for name, path in paths.items():
        if path.suffix == "":
            path.mkdir(parents=True, exist_ok=True)
    return paths
