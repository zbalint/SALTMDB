"""Fail-closed compatibility evaluation for the preserved 2026-08-11 blind vault.

This module deliberately does not import the Gate D custody path.  The historical vault used a
different frozen corpus and evidence schema, so manufacturing Gate D receipts after seeing its
blind artifacts would make a comparison appear stronger than it is.  It instead provides a
small, testable contract for a fresh two-system pool: the current runtime broad baseline versus
the frozen ColBERT candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HISTORICAL_BASELINE_ID = "broad_rt0_pdt0_ds0_ce0"
COLBERT_ID = "late_interaction:answerdotai/answerai-colbert-small-v1:entity"
ARTIFACT_TYPE = "HistoricalCompatibilityEvaluation"


class HistoricalCompatibilityError(ValueError):
    """Raised when historical evidence cannot support an honest comparison."""


@dataclass(frozen=True)
class Decision:
    decision: str
    reasons: tuple[str, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blind_manifest_fingerprint(raw: bytes) -> str:
    """Fingerprint the exact bytes supplied by custody, never a re-serialized object."""
    if not isinstance(raw, bytes) or not raw:
        raise HistoricalCompatibilityError("blind manifest bytes are required")
    return sha256_bytes(raw)


def validate_system_descriptors(descriptors: Sequence[Mapping[str, Any]]) -> None:
    if len(descriptors) != 2:
        raise HistoricalCompatibilityError("exactly two pinned system descriptors are required")
    ids = set()
    for d in descriptors:
        required = (
            "system_id",
            "configuration_id",
            "model_id",
            "index_id",
            "model_revision",
            "model_lock_fingerprint",
            "tokenizer_fingerprint",
            "index_fingerprint",
            "configuration_fingerprint",
        )
        if any(not isinstance(d.get(k), str) or not d[k] for k in required):
            raise HistoricalCompatibilityError("system descriptor is not fully pinned")
        ids.add(d["system_id"])
    if ids != {HISTORICAL_BASELINE_ID, COLBERT_ID}:
        raise HistoricalCompatibilityError("unexpected baseline/candidate system descriptors")


def _query_id(row: Mapping[str, Any]) -> str:
    value = row.get("query_id", row.get("id"))
    if not isinstance(value, str) or not value:
        raise HistoricalCompatibilityError("blind manifest contains an invalid query id")
    return value


def validate_blind_manifest(rows: Sequence[Mapping[str, Any]] | Mapping[str, Any]) -> int:
    """Validate the immutable historical blind query identity without selecting a new split."""
    if isinstance(rows, Mapping):
        rows = rows.get("queries")  # type: ignore[assignment]
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise HistoricalCompatibilityError("blind manifest must be a query list")
    if len(rows) != 800:
        raise HistoricalCompatibilityError("blind manifest must contain exactly 800 queries")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("split") != "blind":
            raise HistoricalCompatibilityError("blind manifest contains a non-blind query")
        query_id = _query_id(row)
        if query_id in seen:
            raise HistoricalCompatibilityError("blind manifest query ids must be unique")
        seen.add(query_id)
    return len(seen)


def validate_bundle_identity(
    bundles: Sequence[Mapping[str, Any]], *, query_fp: str, corpus_fp: str
) -> None:
    """Require exactly the pinned candidate and current broad runtime baseline."""
    if len(bundles) != 2:
        raise HistoricalCompatibilityError("exactly two retrieval bundles are required")
    names: set[str] = set()
    for bundle in bundles:
        name = bundle.get("system_id", bundle.get("contender_id"))
        if not isinstance(name, str) or not name:
            raise HistoricalCompatibilityError("retrieval bundle lacks a system identity")
        if (
            bundle.get("query_manifest_fingerprint") != query_fp
            or bundle.get("corpus_fingerprint") != corpus_fp
        ):
            raise HistoricalCompatibilityError(
                "retrieval bundle fingerprint does not match frozen inputs"
            )
        names.add(name)
    expected = {HISTORICAL_BASELINE_ID, COLBERT_ID}
    if names != expected:
        raise HistoricalCompatibilityError(
            "retrieval bundles must be the broad baseline and ColBERT"
        )
    validate_system_descriptors(bundles)


def _top_rows(
    rows: Mapping[str, Sequence[Mapping[str, Any]]], depth: int
) -> Iterable[tuple[str, Mapping[str, Any]]]:
    if depth <= 0:
        raise HistoricalCompatibilityError("pool depth must be positive")
    for query_id, ranking in rows.items():
        if not isinstance(query_id, str) or not query_id or not isinstance(ranking, Sequence):
            raise HistoricalCompatibilityError("ranking has an invalid query or row list")
        ids: set[str] = set()
        if len(ranking) < depth and any(isinstance(r, Mapping) and "rank" in r for r in ranking):
            raise HistoricalCompatibilityError("ranking must contain a complete top-20 list")
        for expected_rank, row in enumerate(ranking[:depth], 1):
            entity_id = row.get("entity_id") if isinstance(row, Mapping) else None
            content_hash = row.get("content_hash") if isinstance(row, Mapping) else None
            rank = row.get("rank") if isinstance(row, Mapping) else None
            if not isinstance(entity_id, str) or not entity_id or entity_id in ids:
                raise HistoricalCompatibilityError(
                    "ranking contains an invalid or duplicate entity id"
                )
            if not isinstance(content_hash, str) or not content_hash:
                raise HistoricalCompatibilityError(
                    "ranking requires a canonical candidate content_hash"
                )
            if rank is not None and rank != expected_rank:
                raise HistoricalCompatibilityError("ranking ranks must be complete and ordered")
            corpus_ids = row.get("corpus_entity_ids")
            if corpus_ids is not None and entity_id not in corpus_ids:
                raise HistoricalCompatibilityError("ranking entity is absent from frozen corpus")
            ids.add(entity_id)
            yield query_id, {"entity_id": entity_id, "content_hash": content_hash}


def build_two_system_pool(
    baseline: Mapping[str, Sequence[Mapping[str, Any]]],
    candidate: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    depth: int = 20,
) -> dict[str, list[dict[str, str]]]:
    """Build a fresh pairwise pool, never a union with stale historical configuration pools."""
    if set(baseline) != set(candidate):
        raise HistoricalCompatibilityError(
            "baseline and ColBERT rankings must cover identical queries"
        )
    result: dict[str, list[dict[str, str]]] = {query_id: [] for query_id in baseline}
    seen: dict[str, set[str]] = {query_id: set() for query_id in baseline}
    for rankings in (baseline, candidate):
        for query_id, row in _top_rows(rankings, depth):
            if row["entity_id"] not in seen[query_id]:
                result[query_id].append(dict(row))
                seen[query_id].add(row["entity_id"])
            elif any(
                x["entity_id"] == row["entity_id"] and x["content_hash"] != row["content_hash"]
                for x in result[query_id]
            ):
                raise HistoricalCompatibilityError("same entity has divergent content hashes")
    return result


def _label_key(query_id: str, entity_id: str, content_hash: str) -> tuple[str, str, str]:
    return (query_id, entity_id, content_hash)


def reuse_exact_labels(
    pool: Mapping[str, Sequence[Mapping[str, Any]]],
    historical_labels: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    """Reuse a label only when the query, entity, and candidate bytes all still match."""
    result: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for query_id, candidates in pool.items():
        for candidate in candidates:
            key = _label_key(query_id, candidate["entity_id"], candidate["content_hash"])
            if key in historical_labels:
                result[key] = historical_labels[key]
    return result


def validate_final_labels(
    expected: set[str] | set[tuple[str, str, str]],
    labels: Mapping[Any, Mapping[str, Any]],
    *,
    origins: Mapping[Any, str] | None = None,
) -> None:
    """Reject missing, stale, extraneous, or unresolved final judgments."""

    def normalize(value: Any) -> str:
        return value if isinstance(value, str) else "|".join(value)

    expected_keys = {normalize(value) for value in expected}
    actual_keys = {normalize(value) for value in labels}
    if actual_keys != expected_keys:
        raise HistoricalCompatibilityError(
            "final label coverage is missing or contains extraneous labels"
        )
    for key, item in labels.items():
        if item.get("grade") not in {0, 1, 2} or item.get("disagreement") is True:
            raise HistoricalCompatibilityError("final label is unresolved or invalid")
        if origins and origins.get(key, origins.get(normalize(key))) == "new":
            judges = item.get("judge_grades")
            if (
                not isinstance(judges, Sequence)
                or len(judges) != 3
                or item.get("arbitration_resolved") is not True
            ):
                raise HistoricalCompatibilityError(
                    "new labels require three judges and resolved arbitration"
                )


def decide(metrics: Mapping[str, Any]) -> Decision:
    """Apply the predeclared closeout gates; it never implies production activation."""
    failures: list[str] = []
    for key, value in metrics.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise HistoricalCompatibilityError(f"metric {key} must be finite numeric")
    if (
        not isinstance(metrics.get("paired_ndcg_improvement"), (int, float))
        or metrics["paired_ndcg_improvement"] <= 0
    ):
        failures.append("paired NDCG did not improve")
    if not isinstance(metrics.get("ndcg_ci_low"), (int, float)) or metrics["ndcg_ci_low"] <= 0:
        failures.append("NDCG confidence interval does not exclude zero")
    if not isinstance(metrics.get("holm_p"), (int, float)) or metrics["holm_p"] >= 0.05:
        failures.append("Holm-adjusted significance gate failed")
    if (
        not isinstance(metrics.get("safety_regression"), (int, float))
        or metrics["safety_regression"] > 0.01
    ):
        failures.append("safety regression exceeds one percentage point")
    if (
        not isinstance(metrics.get("warm_p95_seconds"), (int, float))
        or metrics["warm_p95_seconds"] > 5.0
    ):
        failures.append("warm p95 latency exceeds five seconds")
    return Decision("RETAINED" if failures else "PROMOTED", tuple(failures))


def make_result(
    *, decision: str, metrics: Mapping[str, Any], reasons: Sequence[str] = ()
) -> dict[str, Any]:
    if decision not in {"PROMOTED", "RETAINED", "INCOMPARABLE"}:
        raise HistoricalCompatibilityError("unknown historical compatibility decision")
    if decision != "INCOMPARABLE" and decide(metrics).decision != decision:
        raise HistoricalCompatibilityError("decision does not match computed metric gates")
    return {
        "artifact_type": ARTIFACT_TYPE,
        "decision": decision,
        "metrics": dict(metrics),
        "reasons": list(reasons),
        "production_activation": "not_authorized",
    }


def write_signed_result(
    result: Mapping[str, Any], output_dir: str | os.PathLike[str], *, signing_key: bytes
) -> Path:
    path = Path(output_dir)
    if path.exists() or not isinstance(signing_key, bytes) or not signing_key:
        raise HistoricalCompatibilityError(
            "output directory must be fresh and signing key required"
        )
    path.mkdir(parents=True)
    payload = json.dumps(dict(result), sort_keys=True, separators=(",", ":")).encode()
    signature = hashlib.sha256(signing_key + payload).hexdigest()
    tmp = path / ".result.json.tmp"
    tmp.write_bytes(payload)
    os.replace(tmp, path / "result.json")
    (path / "result.sig").write_text(signature + "\n", encoding="utf-8")
    return path / "result.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--signing-key", required=True)
    args = parser.parse_args(argv)
    source = Path(args.input)
    if not source.is_file():
        raise HistoricalCompatibilityError("explicit JSON input file is required")
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("artifact_type") not in (None, ARTIFACT_TYPE):
        raise HistoricalCompatibilityError("Gate D artifacts are not accepted")
    result = make_result(
        decision=data["decision"], metrics=data["metrics"], reasons=data.get("reasons", ())
    )
    write_signed_result(result, args.output_dir, signing_key=args.signing_key.encode())
    return 0
