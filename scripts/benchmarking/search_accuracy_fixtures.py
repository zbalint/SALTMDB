"""Schemas and builders for the Stage-1 search-accuracy fixtures.

This module is intentionally *not* a query generator.  A fixture can only be frozen from rows
that a caller supplies with a source reference and (for positives/holdout rows) an expected
entity.  When a corpus or source file is unavailable, callers get a validation error rather than
an invented query that would look like judged evidence.

The two signed collections are deliberately small and stable:

* semantic blind: 250 positives (100 paraphrase, 50 lifecycle/related, 50 multilingual, 50
  short/body/typo) and 100 negatives;
* safety holdout: 100 exact sentence/title and 100 keyword queries, each with one expected entity
  and ``top_k=10``.

The manifest carries the same reproducibility envelope used by the matrix/judging stages.  It is
safe to use this module with a synthetic unit-test source, but it never fabricates judged content.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping

try:  # Works both as a sibling script import and as a package-style test import.
    from evaluation_artifacts import (
        StaleArtifactError,
        artifact_fingerprint,
        build_provenance,
        validate_provenance,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by direct file-loader consumers
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from evaluation_artifacts import (  # type: ignore[no-redef]
        StaleArtifactError,
        artifact_fingerprint,
        build_provenance,
        validate_provenance,
    )


FIXTURE_SCHEMA_VERSION = 1
SEMANTIC_POSITIVE_GROUPS = {
    "paraphrase": 100,
    "lifecycle_related": 50,
    "multilingual": 50,
    "short_body_typo": 50,
}
SEMANTIC_NEGATIVE_GROUP = "negative"
SEMANTIC_NEGATIVE_TOTAL = 100
SAFETY_HOLDOUT_GROUPS = {
    "exact_sentence_title": 100,
    "keyword": 100,
}
SAFETY_TOP_K = 10

_SEMANTIC_ALLOWED_KEYS = {
    "id",
    "query",
    "group",
    "source_entity_ids",
    "topic_family_id",
    "source_reference",
    "provenance",
    "metadata",
}
_SAFETY_ALLOWED_KEYS = {
    "id",
    "query",
    "group",
    "expected_entity_id",
    "top_k",
    "source_reference",
    "provenance",
    "metadata",
}


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _source_reference(row: Mapping[str, object], *, positive: bool) -> str:
    value = row.get("source_reference", row.get("provenance"))
    if isinstance(value, Mapping):
        value = value.get("uri") or value.get("path") or value.get("id")
    if not isinstance(value, str) or not value.strip():
        # A positive's entity IDs are a useful local source reference as long as the fixture
        # producer explicitly supplied them.  A negative still needs a source note/file so that
        # a placeholder cannot be mistaken for a judged query.
        if positive:
            ids = row.get("source_entity_ids")
            if (
                isinstance(ids, list)
                and ids
                and all(isinstance(item, str) and item for item in ids)
            ):
                return "entity:" + ",".join(ids)
            expected = row.get("expected_entity_id")
            if isinstance(expected, str) and expected.strip():
                return "entity:" + expected.strip()
        raise ValueError(
            "query row lacks a non-empty source_reference; content may not be fabricated"
        )
    return value.strip()


def _source_ids(row: Mapping[str, object], *, positive: bool) -> list[str]:
    value = row.get("source_entity_ids", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("source_entity_ids must be a list of non-empty strings")
    if positive and not value:
        raise ValueError("positive semantic rows require source_entity_ids")
    return list(dict.fromkeys(item.strip() for item in value))


def _normalize_semantic_row(row: Mapping[str, object]) -> dict:
    if not isinstance(row, Mapping):
        raise ValueError("semantic fixture row must be an object")
    unknown = set(row) - _SEMANTIC_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"semantic fixture row has unknown fields: {sorted(unknown)}")
    row_id = _text(row.get("id"), "id")
    query = _text(row.get("query"), "query")
    group = _text(row.get("group"), "group")
    positive = group != SEMANTIC_NEGATIVE_GROUP
    source_ids = _source_ids(row, positive=positive)
    reference = _source_reference(row, positive=positive)
    family = row.get("topic_family_id")
    if positive:
        family = _text(family, "topic_family_id")
    elif family is not None:
        family = _text(family, "topic_family_id")
    metadata = row.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    result = {
        "id": row_id,
        "query": query,
        "group": group,
        "source_entity_ids": source_ids,
        "source_reference": reference,
        "metadata": dict(metadata),
    }
    if family is not None:
        result["topic_family_id"] = family
    return result


def _normalize_safety_row(row: Mapping[str, object]) -> dict:
    if not isinstance(row, Mapping):
        raise ValueError("safety holdout row must be an object")
    unknown = set(row) - _SAFETY_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"safety holdout row has unknown fields: {sorted(unknown)}")
    row_id = _text(row.get("id"), "id")
    query = _text(row.get("query"), "query")
    group = _text(row.get("group"), "group")
    if group not in SAFETY_HOLDOUT_GROUPS:
        raise ValueError(f"unknown safety holdout group: {group!r}")
    expected = _text(row.get("expected_entity_id"), "expected_entity_id")
    top_k = row.get("top_k")
    if top_k != SAFETY_TOP_K:
        raise ValueError("safety holdout rows must use top_k=10")
    reference = _source_reference(row, positive=True)
    metadata = row.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    return {
        "id": row_id,
        "query": query,
        "group": group,
        "expected_entity_id": expected,
        "top_k": SAFETY_TOP_K,
        "source_reference": reference,
        "metadata": dict(metadata),
    }


def validate_semantic_queries(queries: Iterable[Mapping[str, object]]) -> list[dict]:
    """Validate and return canonical semantic blind rows with exact pre-registered quotas."""
    normalized = [_normalize_semantic_row(row) for row in queries]
    ids = [row["id"] for row in normalized]
    texts = [row["query"].casefold() for row in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("semantic fixture has duplicate IDs")
    if len(texts) != len(set(texts)):
        raise ValueError("semantic fixture has duplicate query text")
    counts = Counter(row["group"] for row in normalized)
    expected = {**SEMANTIC_POSITIVE_GROUPS, SEMANTIC_NEGATIVE_GROUP: SEMANTIC_NEGATIVE_TOTAL}
    if dict(counts) != expected:
        raise ValueError(f"semantic fixture quotas do not match {expected}: got {dict(counts)}")
    families: dict[str, str] = {}
    for row in normalized:
        family = row.get("topic_family_id")
        if family is None:
            continue
        previous = families.setdefault(family, row["group"])
        if previous != row["group"]:
            raise ValueError("topic_family_id cannot span semantic fixture groups")
    return normalized


def validate_safety_holdout_queries(queries: Iterable[Mapping[str, object]]) -> list[dict]:
    """Validate exact 100/100 safety holdout coverage and expected-entity metadata."""
    normalized = [_normalize_safety_row(row) for row in queries]
    ids = [row["id"] for row in normalized]
    texts = [row["query"].casefold() for row in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("safety holdout has duplicate IDs")
    if len(texts) != len(set(texts)):
        raise ValueError("safety holdout has duplicate query text")
    counts = Counter(row["group"] for row in normalized)
    if dict(counts) != SAFETY_HOLDOUT_GROUPS:
        raise ValueError(
            f"safety holdout quotas do not match {SAFETY_HOLDOUT_GROUPS}: got {dict(counts)}"
        )
    return normalized


def _manifest(
    *,
    kind: str,
    queries: list[dict],
    commit_fingerprint: str,
    corpus_fingerprint: str,
    random_seed: int,
    config_fingerprint: str,
    judge_version_fingerprint: str,
    machine_fingerprint: str | None = None,
) -> dict:
    query_hash = artifact_fingerprint(queries)
    provenance = build_provenance(
        commit_fingerprint=commit_fingerprint,
        corpus_fingerprint=corpus_fingerprint,
        query_manifest_fingerprint=query_hash,
        random_seed=random_seed,
        config_fingerprint=config_fingerprint,
        judge_version_fingerprint=judge_version_fingerprint,
        machine_fingerprint_value=machine_fingerprint,
        artifact_kind=kind,
    )
    result = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "kind": kind,
        "queries": queries,
        "query_manifest_fingerprint": query_hash,
        "provenance": provenance,
    }
    result["manifest_fingerprint"] = artifact_fingerprint(result)
    return result


def build_semantic_blind_manifest(
    queries: Iterable[Mapping[str, object]],
    *,
    commit_fingerprint: str,
    corpus_fingerprint: str,
    random_seed: int,
    config_fingerprint: str,
    judge_version_fingerprint: str,
    machine_fingerprint: str | None = None,
) -> dict:
    """Build a signed semantic blind manifest from caller-provided, sourced query rows."""
    rows = validate_semantic_queries(queries)
    return _manifest(
        kind="semantic_blind",
        queries=rows,
        commit_fingerprint=commit_fingerprint,
        corpus_fingerprint=corpus_fingerprint,
        random_seed=random_seed,
        config_fingerprint=config_fingerprint,
        judge_version_fingerprint=judge_version_fingerprint,
        machine_fingerprint=machine_fingerprint,
    )


def build_safety_holdout_manifest(
    queries: Iterable[Mapping[str, object]],
    *,
    commit_fingerprint: str,
    corpus_fingerprint: str,
    random_seed: int,
    config_fingerprint: str,
    judge_version_fingerprint: str,
    machine_fingerprint: str | None = None,
) -> dict:
    """Build a signed exact/keyword safety holdout from caller-provided rows."""
    rows = validate_safety_holdout_queries(queries)
    return _manifest(
        kind="safety_holdout",
        queries=rows,
        commit_fingerprint=commit_fingerprint,
        corpus_fingerprint=corpus_fingerprint,
        random_seed=random_seed,
        config_fingerprint=config_fingerprint,
        judge_version_fingerprint=judge_version_fingerprint,
        machine_fingerprint=machine_fingerprint,
    )


def build_stage1_fixture_manifest(
    semantic_queries: Iterable[Mapping[str, object]],
    safety_queries: Iterable[Mapping[str, object]],
    *,
    commit_fingerprint: str,
    corpus_fingerprint: str,
    random_seed: int,
    config_fingerprint: str,
    judge_version_fingerprint: str,
    machine_fingerprint: str | None = None,
) -> dict:
    """Build one signed envelope containing both frozen Stage-1 fixture collections."""
    semantic = validate_semantic_queries(semantic_queries)
    safety = validate_safety_holdout_queries(safety_queries)
    query_payload = {"semantic_blind": semantic, "safety_holdout": safety}
    query_hash = artifact_fingerprint(query_payload)
    provenance = build_provenance(
        commit_fingerprint=commit_fingerprint,
        corpus_fingerprint=corpus_fingerprint,
        query_manifest_fingerprint=query_hash,
        random_seed=random_seed,
        config_fingerprint=config_fingerprint,
        judge_version_fingerprint=judge_version_fingerprint,
        machine_fingerprint_value=machine_fingerprint,
        artifact_kind="stage1_fixtures",
    )
    result = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "kind": "stage1_fixtures",
        "semantic_blind": semantic,
        "safety_holdout": safety,
        "query_manifest_fingerprint": query_hash,
        "provenance": provenance,
    }
    result["manifest_fingerprint"] = artifact_fingerprint(result)
    return result


# Friendly aliases for callers that use the plan's terminology.
build_frozen_semantic_manifest = build_semantic_blind_manifest
build_frozen_safety_holdout = build_safety_holdout_manifest
build_frozen_fixture_manifest = build_stage1_fixture_manifest


def _verify_manifest_fingerprint(value: dict) -> None:
    stored = value.get("manifest_fingerprint")
    if not isinstance(stored, str):
        raise StaleArtifactError("fixture manifest lacks manifest_fingerprint")
    unsigned = dict(value)
    unsigned.pop("manifest_fingerprint", None)
    if stored != artifact_fingerprint(unsigned):
        raise StaleArtifactError("fixture manifest fingerprint mismatch")


def validate_fixture_manifest(
    value: Mapping[str, object], *, expected_provenance: dict | None = None
) -> dict:
    """Reject tampered, stale, or quota-incomplete fixture manifests."""
    if not isinstance(value, Mapping) or value.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported fixture manifest schema")
    value = dict(value)
    _verify_manifest_fingerprint(value)
    provenance = validate_provenance(value, expected_provenance, artifact_label="fixture manifest")
    kind = value.get("kind")
    if kind == "semantic_blind":
        rows = validate_semantic_queries(value.get("queries", []))
        expected_hash = artifact_fingerprint(rows)
    elif kind == "safety_holdout":
        rows = validate_safety_holdout_queries(value.get("queries", []))
        expected_hash = artifact_fingerprint(rows)
    elif kind == "stage1_fixtures":
        semantic = validate_semantic_queries(value.get("semantic_blind", []))
        safety = validate_safety_holdout_queries(value.get("safety_holdout", []))
        expected_hash = artifact_fingerprint({"semantic_blind": semantic, "safety_holdout": safety})
    else:
        raise ValueError("fixture manifest kind is invalid")
    if value.get("query_manifest_fingerprint") != expected_hash:
        raise StaleArtifactError("fixture query-manifest fingerprint mismatch")
    if provenance.get("query_manifest_fingerprint") != expected_hash:
        raise StaleArtifactError("fixture provenance query fingerprint mismatch")
    return value


def load_fixture_manifest(path: Path, *, expected_provenance: dict | None = None) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_fixture_manifest(value, expected_provenance=expected_provenance)


def write_fixture_manifest(value: Mapping[str, object], path: Path) -> dict:
    validated = validate_fixture_manifest(value)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(validated, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)
    return validated
