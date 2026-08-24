"""Build a signed development judging matrix for raw-production pool escapes.

This builder adds only the exact raw lexical (query, entity) pairs absent from the historical
development judging matrix.  It never assigns grades: judges receive the signed matrix and
rendered text, then an external three-judge/adjudication merge may be supplied later.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bakeoff_state import (  # noqa: E402
    sha256_bytes,
    sign_artifact,
    validate_bakeoff_spec,
    validate_corpus_manifest,
    validate_signed_artifact,
)
from build_evaluation_queries import (  # noqa: E402
    artifact_fingerprint,
    validate_queries,
    verify_artifact_fingerprint,
)
from build_judging_matrix import load_frozen_entity_text  # noqa: E402
from evaluation_artifacts import validate_provenance  # noqa: E402


class JudgingAddendumError(ValueError):
    """Inputs cannot prove an exact, text-complete development addendum."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JudgingAddendumError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise JudgingAddendumError(f"expected an object: {path}")
    return value


def _load_queries(
    path: Path, *, expected_spec: Mapping[str, Any]
) -> tuple[list[dict], dict[str, str]]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JudgingAddendumError(f"invalid query manifest JSON: {path}") from exc
    if not isinstance(value, dict):
        raise JudgingAddendumError(f"expected query manifest object: {path}")
    try:
        verify_artifact_fingerprint(value, field="manifest_fingerprint")
        if value.get("queries_fingerprint") != artifact_fingerprint(value["queries"]):
            raise ValueError("query manifest queries_fingerprint mismatch")
        validate_provenance(value, artifact_label="query manifest")
        validate_queries(value["queries"])
    except (KeyError, ValueError) as exc:
        raise JudgingAddendumError(str(exc)) from exc
    queries = value.get("queries")
    if not isinstance(queries, list) or len(queries) != 400:
        raise JudgingAddendumError("development query manifest must contain exactly 400 queries")
    if any(query.get("split") != "dev" for query in queries):
        raise JudgingAddendumError("development query manifest contains a non-dev query")
    if value.get("corpus_fingerprint") != expected_spec["corpus_snapshot_hash"]:
        raise JudgingAddendumError("query manifest corpus fingerprint does not match spec")
    return queries, {
        "query_manifest_fingerprint": value["manifest_fingerprint"],
        "query_manifest_file_sha256": sha256_bytes(payload),
        "query_split": "dev",
    }


def _missing_pairs(  # noqa: C901
    raw_bundle: Mapping[str, Any], old_matrix: Mapping[str, Any], query_ids: set[str]
) -> dict[str, list[str]]:
    old_pools = old_matrix.get("pools")
    if not isinstance(old_pools, dict) or set(old_pools) != query_ids:
        raise JudgingAddendumError(
            "historical judging matrix does not cover exactly the dev queries"
        )
    rows = raw_bundle.get("results")
    if not isinstance(rows, list) or len(rows) != len(query_ids):
        raise JudgingAddendumError("raw bundle result count does not match the query manifest")
    result: dict[str, list[str]] = {}
    seen_queries: set[str] = set()
    for row in rows:
        query_id = row.get("query_id")
        if query_id not in query_ids or query_id in seen_queries:
            raise JudgingAddendumError("raw bundle has unknown or duplicate query IDs")
        seen_queries.add(query_id)
        top20 = row.get("top20")
        if not isinstance(top20, list):
            raise JudgingAddendumError(f"raw bundle lacks top20 for {query_id}")
        existing = old_pools[query_id]
        if not isinstance(existing, dict):
            raise JudgingAddendumError(f"historical pool is malformed for {query_id}")
        missing: list[str] = []
        seen: set[str] = set()
        for hit in top20:
            entity_id = hit.get("entity_id") if isinstance(hit, dict) else None
            if not isinstance(entity_id, str) or not entity_id:
                raise JudgingAddendumError(f"raw bundle has an invalid entity for {query_id}")
            if entity_id in seen:
                raise JudgingAddendumError(
                    f"raw bundle has duplicate entity {entity_id} for {query_id}"
                )
            seen.add(entity_id)
            if entity_id not in existing:
                missing.append(entity_id)
        if missing:
            result[query_id] = missing
    if seen_queries != query_ids:
        raise JudgingAddendumError("raw bundle is missing one or more query rows")
    return result


def build_addendum(  # noqa: C901, PLR0912
    *,
    raw_bundle_path: Path,
    worklist_path: Path,
    judging_matrix_path: Path,
    spec_path: Path,
    query_manifest_path: Path,
    corpus_manifest_path: Path,
    corpus_export_path: Path,
) -> dict[str, Any]:
    spec = validate_bakeoff_spec(_load_json(spec_path))
    old_matrix = validate_signed_artifact(_load_json(judging_matrix_path), kind="JudgingMatrix")
    raw_bundle = validate_signed_artifact(_load_json(raw_bundle_path), kind="RetrievalRunBundle")
    if raw_bundle.get("spec_fingerprint") != spec["artifact_fingerprint"]:
        raise JudgingAddendumError("raw bundle spec fingerprint does not match BakeoffSpec")
    cell = raw_bundle.get("cell")
    if not isinstance(cell, dict) or cell.get("kind") != "lexical":
        raise JudgingAddendumError("raw bundle is not lexical")
    for key, expected in {
        "channel": "bm25_raw_production",
        "lexical_policy": "raw_production",
        "production_faithful": True,
        "representation_root": spec["corpus_snapshot_hash"],
    }.items():
        if cell.get(key) != expected:
            raise JudgingAddendumError(f"raw lexical cell {key} is not production-faithful")
    if not isinstance(cell.get("lexical_snapshot_receipt_fingerprint"), str):
        raise JudgingAddendumError("raw lexical cell lacks snapshot receipt fingerprint")
    if not isinstance(cell.get("lexical_snapshot_db_sha256"), str):
        raise JudgingAddendumError("raw lexical cell lacks snapshot DB SHA-256")
    if raw_bundle.get("failures"):
        raise JudgingAddendumError("raw bundle has failures")
    queries, query_binding = _load_queries(query_manifest_path, expected_spec=spec)
    query_ids = {query["id"] for query in queries}
    if old_matrix.get("spec_fingerprint") != spec["artifact_fingerprint"]:
        raise JudgingAddendumError("historical judging matrix spec fingerprint mismatch")
    if old_matrix.get("corpus_root_hash") != spec["corpus_snapshot_hash"]:
        raise JudgingAddendumError("historical judging matrix corpus root mismatch")
    manifest = validate_corpus_manifest(_load_json(corpus_manifest_path))
    if manifest["corpus_root_hash"] != spec["corpus_snapshot_hash"]:
        raise JudgingAddendumError("corpus manifest root does not match spec")
    corpus_export = _load_json(corpus_export_path)
    entity_text = load_frozen_entity_text(corpus_export, manifest)
    missing = _missing_pairs(raw_bundle, old_matrix, query_ids)
    worklist = _load_json(worklist_path)
    try:
        verify_artifact_fingerprint(worklist, field="artifact_fingerprint")
    except ValueError as exc:
        raise JudgingAddendumError(f"worklist fingerprint invalid: {exc}") from exc
    if worklist.get("missing_pairs_by_query") != missing:
        raise JudgingAddendumError("worklist does not exactly equal recomputed raw pool escapes")
    if worklist.get("counts", {}).get("missing_pair_count") != sum(map(len, missing.values())):
        raise JudgingAddendumError("worklist missing-pair count is inconsistent")
    if len(missing) != 172 or sum(map(len, missing.values())) != 241:
        raise JudgingAddendumError("raw worklist must contain exactly 172 queries and 241 pairs")
    pools: dict[str, dict[str, dict[str, Any]]] = {}
    for query_id, entity_ids in missing.items():
        pools[query_id] = {}
        for entity_id in entity_ids:
            if entity_id in old_matrix["pools"][query_id]:
                raise JudgingAddendumError("addendum contains an already-labelled pair")
            text = entity_text.get(entity_id)
            if text is None or not text.get("title") and not text.get("body"):
                raise JudgingAddendumError(f"missing frozen entity text for {entity_id}")
            pools[query_id][entity_id] = {
                "title": text["title"],
                "full_content": text["body"],
                "ground_truth_forced_include": False,
            }
    payload = {
        "development_only": True,
        "evidence_tier": "exploratory_development_screening",
        "addendum_reason": "raw production lexical candidates outside historical judging pools",
        "spec_fingerprint": spec["artifact_fingerprint"],
        "corpus_root_hash": manifest["corpus_root_hash"],
        "query_manifest_fingerprint": query_binding["query_manifest_fingerprint"],
        "query_manifest_file_sha256": query_binding["query_manifest_file_sha256"],
        "query_split": "dev",
        "raw_bundle_fingerprint": raw_bundle["artifact_fingerprint"],
        "raw_cell": cell,
        "parent_judging_matrix_fingerprint": old_matrix["artifact_fingerprint"],
        "worklist_fingerprint": worklist["artifact_fingerprint"],
        "corpus_export_file_sha256": sha256_bytes(corpus_export_path.read_bytes()),
        "corpus_manifest_fingerprint": manifest["artifact_fingerprint"],
        "query_count": len(pools),
        "missing_pair_count": sum(map(len, pools.values())),
        "pools": pools,
    }
    return sign_artifact("JudgingMatrix", payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-bundle", type=Path, required=True)
    parser.add_argument("--worklist", type=Path, required=True)
    parser.add_argument("--judging-matrix", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--queries-dev", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--corpus-export", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_addendum(
        raw_bundle_path=args.raw_bundle,
        worklist_path=args.worklist,
        judging_matrix_path=args.judging_matrix,
        spec_path=args.spec,
        query_manifest_path=args.queries_dev,
        corpus_manifest_path=args.corpus_manifest,
        corpus_export_path=args.corpus_export,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
