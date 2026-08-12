"""Replay the frozen 2026-08-12 historical search audit on one disposable database.

The historical worker reports were reconstructed into
``scratch/eval_results/historical-search-accuracy-replay-20260812/query_replay_manifest_unsigned.json``.
This runner binds that exact query list to a fixed, intentionally small eight-config experiment
manifest before executing anything.  It uses the existing matrix runner's read-only search
adapter and database guard, executes configurations query-major (one query interleaves all
contenders), checkpoints after complete queries, and writes rankings, diagnostics, latency,
errors, and exact-ID safety aggregates as JSON.

The latency values emitted here are direct-service diagnostics.  Promotion-grade latency still
requires the persistent-daemon protocol in ``latency_protocol.py``; this script does not mutate
runtime defaults, the corpus, or the live daemon.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from evaluation_artifacts import (  # noqa: E402
    artifact_fingerprint,
    build_provenance,
    file_fingerprint,
    git_commit_fingerprint,
    machine_fingerprint,
    validate_provenance,
    verify_artifact_fingerprint,
)
from judge_pool import judge_version_fingerprint  # noqa: E402
from run_evaluation_matrix import (  # noqa: E402
    RERANKER_MODEL,
    _fetch_entity_stub,
    _refuse_unsafe_db_path,
    preflight_cross_encoder_configs,
    run_one_config,
)
from saltmdb.db.connection import close_connection, get_connection  # noqa: E402
from saltmdb.domain.services.memory_service import get_last_search_diagnostics  # noqa: E402


DEFAULT_MANIFEST = (
    _REPO_ROOT
    / "scratch"
    / "eval_results"
    / "historical-search-accuracy-replay-20260812"
    / "query_replay_manifest_unsigned.json"
)
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = "historical_search_accuracy_replay"
QUERY_TYPES = ("whole_sentence", "semantic_paraphrase", "keyword", "negative_control")
EXPECTED_QUERY_COUNTS = {query_type: 150 for query_type in QUERY_TYPES}
CONTROL_NAME = "broad_rt0_pdt0_ds0_ce0"


def _config(
    name: str,
    *,
    use_chunk_candidates: bool = False,
    oversampling_multiplier: int = 1,
    candidate_window: int = 0,
    chunk_weight: float = 0.0,
    collapse_supersedes_families: bool = False,
    use_cross_encoder: bool = False,
    cross_encoder_candidate_cap: int | None = None,
    cross_encoder_text_cap_chars: int | None = None,
    force_cross_encoder: bool = False,
    use_retrieval_text_candidates: bool = False,
    retrieval_fts_weight: float = 0.0,
    retrieval_vector_weight: float = 0.0,
) -> dict[str, Any]:
    """Build one explicit broad-mode contender, including inert disabled sentinels."""
    return {
        "name": name,
        "mode": "broad",
        "rerank_by_topic": False,
        "prefer_durable_types": False,
        "demote_superseded": False,
        "use_cross_encoder": use_cross_encoder,
        "cross_encoder_candidate_cap": cross_encoder_candidate_cap,
        "cross_encoder_text_cap_chars": cross_encoder_text_cap_chars,
        "force_cross_encoder": force_cross_encoder,
        "use_chunk_candidates": use_chunk_candidates,
        "oversampling_multiplier": oversampling_multiplier,
        "candidate_window": candidate_window,
        "chunk_weight": chunk_weight,
        "collapse_supersedes_families": collapse_supersedes_families,
        "use_retrieval_text_candidates": use_retrieval_text_candidates,
        "retrieval_fts_weight": retrieval_fts_weight,
        "retrieval_vector_weight": retrieval_vector_weight,
    }


def frozen_configs() -> list[dict[str, Any]]:
    """Return the predeclared eight-config shortlist in stable execution order.

    The middle and high chunk settings are the centre and upper endpoint of the bounded runtime
    options.  CE uses the lowest allowed cap/text size for an efficient first replay.  This list
    is code-frozen and is never selected from observed replay results.
    """
    return [
        _config(CONTROL_NAME),
        _config(
            "chunk_mid_ov8_win40_w1",
            use_chunk_candidates=True,
            oversampling_multiplier=8,
            candidate_window=40,
            chunk_weight=1.0,
        ),
        _config(
            "chunk_high_ov12_win60_w1_5",
            use_chunk_candidates=True,
            oversampling_multiplier=12,
            candidate_window=60,
            chunk_weight=1.5,
        ),
        _config("collapse_only", collapse_supersedes_families=True),
        _config(
            "chunk_mid_collapse",
            use_chunk_candidates=True,
            oversampling_multiplier=8,
            candidate_window=40,
            chunk_weight=1.0,
            collapse_supersedes_families=True,
        ),
        _config(
            "ce_gate_cap10_text1000",
            use_cross_encoder=True,
            cross_encoder_candidate_cap=10,
            cross_encoder_text_cap_chars=1000,
        ),
        _config(
            "ce_force_cap10_text1000",
            use_cross_encoder=True,
            cross_encoder_candidate_cap=10,
            cross_encoder_text_cap_chars=1000,
            force_cross_encoder=True,
        ),
        _config(
            "retrieval_text_control",
            use_retrieval_text_candidates=True,
            retrieval_fts_weight=1.0,
            retrieval_vector_weight=1.0,
        ),
    ]


def _fingerprint(value: object) -> str:
    return artifact_fingerprint(value)


def _atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    temporary.replace(path)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    floor = int(index)
    ceil = min(floor + 1, len(ordered) - 1)
    if floor == ceil:
        return ordered[floor]
    return ordered[floor] + (ordered[ceil] - ordered[floor]) * (index - floor)


def _validate_frozen_configs(configs: list[dict[str, Any]]) -> None:
    expected = frozen_configs()
    if configs != expected:
        raise ValueError("config manifest differs from the code-frozen eight-config shortlist")
    names = [config["name"] for config in configs]
    if len(names) != len(set(names)) or CONTROL_NAME not in names:
        raise ValueError("config manifest has invalid or duplicate names")


def _normalize_queries(raw_queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map historical rows to the matrix runner shape without changing query text/order."""
    if len(raw_queries) != 600:
        raise ValueError(f"historical replay requires exactly 600 rows, got {len(raw_queries)}")
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    normalized: list[dict[str, Any]] = []
    for row in raw_queries:
        if not isinstance(row, dict):
            raise ValueError("historical query row must be an object")
        row_id = row.get("id")
        query = row.get("query")
        query_type = row.get("query_type")
        target_id = row.get("target_id")
        if (
            not isinstance(row_id, str)
            or not row_id
            or row_id in seen
            or not isinstance(query, str)
            or not query.strip()
            or query_type not in QUERY_TYPES
            or not isinstance(target_id, str)
            or not target_id
        ):
            raise ValueError("historical query row lacks a unique id, query, type, or target")
        seen.add(row_id)
        counts[query_type] += 1
        is_negative = query_type == "negative_control"
        normalized.append(
            {
                "id": row_id,
                "query": query,
                "source_entity_ids": [] if is_negative else [target_id],
                "category": query_type,
                "topic_family_id": f"historical-target:{target_id}",
                "expected_entity_id": target_id,
                "source": row.get("source"),
                "trial": row.get("trial"),
                "target_title": row.get("target_title"),
            }
        )
    if dict(counts) != EXPECTED_QUERY_COUNTS:
        raise ValueError(f"historical query condition counts mismatch: {dict(counts)}")
    return normalized


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _build_signed_manifest(
    raw: dict[str, Any],
    normalized_queries: list[dict[str, Any]],
    configs: list[dict[str, Any]],
    *,
    commit_fingerprint: str,
    corpus_fingerprint: str,
    random_seed: int,
    judge_fingerprint: str,
    machine_marker: str,
) -> dict[str, Any]:
    provenance = build_provenance(
        commit_fingerprint=commit_fingerprint,
        corpus_fingerprint=corpus_fingerprint,
        query_manifest_fingerprint=raw["queries_fingerprint"],
        random_seed=random_seed,
        config_fingerprint=_fingerprint(configs),
        judge_version_fingerprint=judge_fingerprint,
        machine_fingerprint_value=machine_marker,
        artifact_kind=MANIFEST_KIND,
    )
    signed = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_kind": MANIFEST_KIND,
        "signed": True,
        "source_manifest_fingerprint": _fingerprint(raw),
        "source_queries_fingerprint": raw["queries_fingerprint"],
        "queries_fingerprint": raw["queries_fingerprint"],
        "source_audit_context": raw.get("source_audit_context"),
        "sampling": raw.get("sampling"),
        "reconstruction": raw.get("reconstruction"),
        "queries": raw["queries"],
        "normalized_queries": normalized_queries,
        "configs": configs,
        "config_fingerprint": _fingerprint(configs),
        "provenance": provenance,
    }
    signed["manifest_fingerprint"] = _fingerprint(signed)
    return signed


def load_or_bind_manifest(
    path: Path,
    configs: list[dict[str, Any]],
    *,
    commit_fingerprint: str,
    corpus_fingerprint: str,
    random_seed: int,
    judge_fingerprint: str,
    machine_marker: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load the historical source or validate a previously bound manifest."""
    raw = _load_json(path)
    if raw.get("manifest_kind") != MANIFEST_KIND:
        raise ValueError("manifest_kind is not the historical search replay contract")
    if not isinstance(raw.get("queries"), list):
        raise ValueError("manifest lacks queries")
    if raw.get("signed") is True:
        verify_artifact_fingerprint(raw, field="manifest_fingerprint")
        _validate_frozen_configs(raw.get("configs"))
        if raw.get("source_queries_fingerprint") != raw.get("queries_fingerprint"):
            # Signed manifests use the explicit source_* name; this branch catches hand-edited
            # artifacts while keeping the source fingerprint available to callers.
            raise ValueError("signed manifest source query fingerprint is inconsistent")
        expected = {
            "commit_fingerprint": commit_fingerprint,
            "corpus_fingerprint": corpus_fingerprint,
            "query_manifest_fingerprint": raw["source_queries_fingerprint"],
            "random_seed": random_seed,
            "config_fingerprint": _fingerprint(configs),
            "judge_version_fingerprint": judge_fingerprint,
        }
        validate_provenance(raw, expected, artifact_label="historical replay manifest")
        normalized = raw.get("normalized_queries")
        if not isinstance(normalized, list):
            raise ValueError("signed manifest lacks normalized_queries")
        _normalize_queries(raw["queries"])
        if normalized != _normalize_queries(raw["queries"]):
            raise ValueError("signed normalized query rows do not match source rows")
        return raw, normalized

    if raw.get("signed") is not False:
        raise ValueError("source manifest must explicitly declare signed=false")
    if raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported historical manifest schema")
    if not isinstance(raw.get("queries_fingerprint"), str):
        raise ValueError("unsigned manifest lacks queries_fingerprint")
    if raw["queries_fingerprint"] != _fingerprint(raw["queries"]):
        raise ValueError("unsigned manifest query fingerprint mismatch")
    normalized = _normalize_queries(raw["queries"])
    return (
        _build_signed_manifest(
            raw,
            normalized,
            configs,
            commit_fingerprint=commit_fingerprint,
            corpus_fingerprint=corpus_fingerprint,
            random_seed=random_seed,
            judge_fingerprint=judge_fingerprint,
            machine_marker=machine_marker,
        ),
        normalized,
    )


def _validate_checkpoint(
    checkpoint: dict[str, Any],
    *,
    resume_meta: dict[str, Any],
    query_ids: set[str],
    config_names: set[str],
) -> None:
    if checkpoint.get("resume_meta") != resume_meta:
        raise RuntimeError("checkpoint inputs/configuration do not match this replay")
    completed = set(checkpoint.get("completed_query_ids", []))
    rankings = checkpoint.get("config_rankings", {})
    pools = checkpoint.get("pools", {})
    if not completed.issubset(query_ids):
        raise ValueError("checkpoint contains a query absent from the signed manifest")
    for query_id in completed:
        if query_id not in rankings or query_id not in pools:
            raise ValueError("checkpoint marks an incomplete query as completed")
        if set(rankings[query_id]) != config_names:
            raise ValueError(f"checkpoint query {query_id!r} lacks complete config coverage")


def _run_query(
    conn,
    db_path: str,
    query: dict[str, Any],
    configs: list[dict[str, Any]],
    limit: int,
) -> tuple[dict[str, list[str]], dict[str, dict], dict[str, float], list[dict], dict[str, dict]]:
    rankings: dict[str, list[str]] = {}
    pool: dict[str, dict] = {}
    latencies: dict[str, float] = {}
    errors: list[dict] = []
    diagnostics: dict[str, dict] = {}
    for config in configs:
        items, elapsed_ms, error = run_one_config(
            conn, db_path, query["query"], config, limit=limit
        )
        name = config["name"]
        rankings[name] = [item["id"] for item in items]
        latencies[name] = elapsed_ms
        diagnostics[name] = get_last_search_diagnostics()
        if error:
            errors.append({"query_id": query["id"], "config_name": name, "error": error})
        for item in items:
            pool.setdefault(
                item["id"],
                {
                    "title": item.get("title"),
                    "snippet": item.get("snippet"),
                    "memory_type": item.get("memory_type"),
                    "ground_truth_forced_include": False,
                },
            )
    # Positive target IDs are added for judging even when every config missed them.  Negative
    # controls deliberately have no source_entity_ids, so they are never force-injected.
    for source_id in query.get("source_entity_ids") or []:
        if source_id in pool:
            continue
        stub = _fetch_entity_stub(conn, source_id)
        if stub is None:
            errors.append(
                {
                    "query_id": query["id"],
                    "config_name": None,
                    "error": f"source_entity_id {source_id!r} not found/archived in corpus",
                }
            )
            continue
        pool[source_id] = {
            "title": stub["title"],
            "snippet": stub["snippet"],
            "memory_type": stub["memory_type"],
            "ground_truth_forced_include": True,
        }
    return rankings, pool, latencies, errors, diagnostics


def run_replay_for_queries(
    conn,
    db_path: str,
    queries: list[dict[str, Any]],
    configs: list[dict[str, Any]],
    *,
    limit: int = 20,
    checkpoint_path: Path | None = None,
    checkpoint_every: int = 10,
    resume_result: dict[str, Any] | None = None,
    resume_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one query at a time across all frozen contenders, preserving checkpoints."""
    _validate_frozen_configs(configs)
    resume_result = resume_result or {}
    config_names = {config["name"] for config in configs}
    query_ids = {query["id"] for query in queries}
    rankings: dict[str, dict[str, list[str]]] = resume_result.get("config_rankings", {})
    pools: dict[str, dict[str, dict]] = resume_result.get("pools", {})
    latencies_ms: dict[str, list[float]] = resume_result.get(
        "latencies_ms", {name: [] for name in config_names}
    )
    errors: list[dict] = resume_result.get("errors", [])
    diagnostics: dict[str, dict] = resume_result.get("execution_diagnostics", {})
    completed = set(resume_result.get("completed_query_ids", rankings))
    if resume_result:
        _validate_checkpoint(
            resume_result,
            resume_meta=resume_meta or {},
            query_ids=query_ids,
            config_names=config_names,
        )
    if not completed.issubset(query_ids):
        raise ValueError("completed query is absent from manifest")

    for index, query in enumerate(queries, start=1):
        query_id = query["id"]
        if query_id in completed:
            continue
        query_rankings, query_pool, query_latencies, query_errors, query_diagnostics = _run_query(
            conn, db_path, query, configs, limit
        )
        rankings[query_id] = query_rankings
        pools[query_id] = query_pool
        diagnostics[query_id] = query_diagnostics
        errors.extend(query_errors)
        for name, elapsed_ms in query_latencies.items():
            latencies_ms.setdefault(name, []).append(elapsed_ms)
        completed.add(query_id)
        if checkpoint_path and checkpoint_every and len(completed) % checkpoint_every == 0:
            _atomic_json_write(
                checkpoint_path,
                {
                    "schema_version": 1,
                    "config_rankings": rankings,
                    "pools": pools,
                    "latencies_ms": latencies_ms,
                    "errors": errors,
                    "execution_diagnostics": diagnostics,
                    "completed_query_ids": sorted(completed),
                    "resume_meta": resume_meta,
                },
            )
        if index % 50 == 0:
            print(f"  ... {index}/{len(queries)} queries done", file=sys.stderr)

    return {
        "config_rankings": rankings,
        "pools": pools,
        "latencies_ms": latencies_ms,
        "errors": errors,
        "execution_diagnostics": diagnostics,
        "completed_query_ids": sorted(completed),
        "resume_meta": resume_meta,
    }


def _win_loss_tie(candidate: list[bool], control: list[bool]) -> dict[str, int]:
    if len(candidate) != len(control):
        raise ValueError("paired exact-top1 vectors are not aligned")
    wins = sum(c and not b for c, b in zip(candidate, control))
    losses = sum(b and not c for c, b in zip(candidate, control))
    ties = len(candidate) - wins - losses
    return {"wins": wins, "losses": losses, "ties": ties}


def _channel_aggregate(
    config_name: str,
    diagnostics: dict[str, dict],
    queries: list[dict[str, Any]],
    conn,
) -> dict[str, Any]:
    records = [by_config.get(config_name, {}) for by_config in diagnostics.values()]
    chunk_records = [item.get("chunk_candidate", {}) for item in records]
    chunk_requested = sum(bool(item.get("requested")) for item in chunk_records)
    chunk_executed = sum(bool(item.get("executed")) for item in chunk_records)
    chunk_candidates = [int(item.get("unique_fresh_entities", 0) or 0) for item in chunk_records]
    chunk_shortfalls = [int(item.get("candidate_shortfall", 0) or 0) for item in chunk_records]

    ce_records = [item.get("cross_encoder", {}) for item in records]
    ce_requested = sum(bool(item.get("requested")) for item in ce_records)
    ce_executed = sum(bool(item.get("executed")) for item in ce_records)
    ce_calls = sum(int(item.get("execution_count", 0) or 0) for item in ce_records)

    retrieval_records = [item.get("retrieval_text", {}) for item in records]
    retrieval_requested = sum(bool(item.get("requested")) for item in retrieval_records)
    retrieval_executed = sum(bool(item.get("executed")) for item in retrieval_records)
    retrieval_candidate_queries = sum(
        bool(item.get("fts_candidate_count", 0) or item.get("vector_candidate_count", 0))
        for item in retrieval_records
    )
    retrieval_fts_rows = sum(int(item.get("fts_candidate_count", 0) or 0) for item in retrieval_records)
    retrieval_vector_rows = sum(
        int(item.get("vector_candidate_count", 0) or 0) for item in retrieval_records
    )

    collapse_records = [
        item.get("supersedes_collapse", {})
        for item in records
        if item.get("supersedes_collapse")
    ]
    collapse_changed = sum(
        int(item.get("before_count", 0) or 0) > int(item.get("after_count", 0) or 0)
        for item in collapse_records
    )

    result = {
        "chunk_candidates": {
            "requested_searches": chunk_requested,
            "executed_searches": chunk_executed,
            "execution_rate": chunk_executed / len(chunk_records) if chunk_records else 0.0,
            "queries_with_fresh_candidates": sum(value > 0 for value in chunk_candidates),
            "candidate_coverage_rate": (
                sum(value > 0 for value in chunk_candidates) / len(chunk_records)
                if chunk_records
                else 0.0
            ),
            "total_unique_fresh_entities": sum(chunk_candidates),
            "total_candidate_shortfall": sum(chunk_shortfalls),
            "max_candidate_shortfall": max(chunk_shortfalls, default=0),
        },
        "cross_encoder": {
            "requested_searches": ce_requested,
            "executed_searches": ce_executed,
            "execution_rate": ce_executed / ce_requested if ce_requested else 0.0,
            "execution_count": ce_calls,
        },
        "retrieval_text": {
            "requested_searches": retrieval_requested,
            "executed_searches": retrieval_executed,
            "execution_rate": retrieval_executed / retrieval_requested
            if retrieval_requested
            else 0.0,
            "queries_with_candidates": retrieval_candidate_queries,
            "candidate_coverage_rate": retrieval_candidate_queries / retrieval_requested
            if retrieval_requested
            else 0.0,
            "fts_candidate_rows": retrieval_fts_rows,
            "vector_candidate_rows": retrieval_vector_rows,
        },
        "supersedes_collapse": {
            "executed_searches": len(collapse_records),
            "result_pools_with_eligible_complete_chain": collapse_changed,
            "result_pool_change_rate": collapse_changed / len(collapse_records)
            if collapse_records
            else 0.0,
        },
    }
    if config_name == CONTROL_NAME:
        result["retrieval_text"]["target_coverage"] = _retrieval_target_coverage(conn, queries)
    else:
        # Coverage is corpus-level and independent of ranking; repeat the same evidence for the
        # optional channel so each contender is self-describing in machine-readable output.
        result["retrieval_text"]["target_coverage"] = _retrieval_target_coverage(conn, queries)
    return result


def _retrieval_target_coverage(conn, queries: list[dict[str, Any]]) -> dict[str, Any]:
    target_ids = sorted(
        {query["expected_entity_id"] for query in queries if query["category"] != "negative_control"}
    )
    if not target_ids:
        return {"available": True, "target_entities": 0}
    placeholders = ",".join("?" for _ in target_ids)
    try:
        rows = conn.execute(
            f"""
            SELECT e.id,
                   e.retrieval_text IS NOT NULL AND e.retrieval_text != '' AS has_text,
                   EXISTS(
                       SELECT 1 FROM retrieval_embedding_jobs j
                       WHERE j.entity_id = e.id
                         AND j.source_hash = e.retrieval_text_hash
                         AND j.state = 'succeeded'
                   ) AS has_embedding
            FROM entities e
            WHERE e.id IN ({placeholders}) AND e.status != 'archived'
            """,
            target_ids,
        ).fetchall()
    except Exception as exc:  # Compatibility for old/synthetic DBs without retrieval schema.
        return {"available": False, "target_entities": len(target_ids), "error": str(exc)}
    by_id = {row[0]: (bool(row[1]), bool(row[2])) for row in rows}
    fresh_text = sum(by_id.get(entity_id, (False, False))[0] for entity_id in target_ids)
    fresh_embedding = sum(by_id.get(entity_id, (False, False))[1] for entity_id in target_ids)
    fully_covered = sum(
        by_id.get(entity_id, (False, False)) == (True, True) for entity_id in target_ids
    )
    denominator = len(target_ids)
    return {
        "available": True,
        "target_entities": denominator,
        "fresh_retrieval_text_entities": fresh_text,
        "succeeded_fresh_embedding_entities": fresh_embedding,
        "fully_covered_entities": fully_covered,
        "full_coverage_rate": fully_covered / denominator if denominator else 0.0,
    }


def aggregate_results(
    result: dict[str, Any], queries: list[dict[str, Any]], configs: list[dict[str, Any]], conn
) -> dict[str, Any]:
    """Compute exact-ID safety metrics and paired control comparisons."""
    rankings = result.get("config_rankings", {})
    diagnostics = result.get("execution_diagnostics", {})
    errors_by_config: Counter[str] = Counter(
        error["config_name"] for error in result.get("errors", []) if error.get("config_name")
    )
    positive_queries = [query for query in queries if query["category"] != "negative_control"]
    negative_queries = [query for query in queries if query["category"] == "negative_control"]
    config_aggregates: dict[str, Any] = {}
    control_top1: list[bool] = []
    control_negative_pass: list[bool] = []

    for query in positive_queries:
        ranking = rankings[query["id"]][CONTROL_NAME]
        control_top1.append(bool(ranking and ranking[0] == query["expected_entity_id"]))
    for query in negative_queries:
        ranking = rankings[query["id"]][CONTROL_NAME]
        control_negative_pass.append(query["expected_entity_id"] not in ranking[:10])

    for config in configs:
        name = config["name"]
        positive_top1 = []
        positive_top10 = []
        negative_pass = []
        by_condition: dict[str, dict[str, Any]] = {}
        for query_type in QUERY_TYPES:
            selected = [query for query in queries if query["category"] == query_type]
            top1 = top10 = negative = 0
            for query in selected:
                ranking = rankings[query["id"]][name]
                if query_type == "negative_control":
                    negative += query["expected_entity_id"] not in ranking[:10]
                else:
                    hit_top1 = bool(ranking and ranking[0] == query["expected_entity_id"])
                    hit_top10 = query["expected_entity_id"] in ranking[:10]
                    top1 += hit_top1
                    top10 += hit_top10
                    positive_top1.append(hit_top1)
                    positive_top10.append(hit_top10)
            if query_type == "negative_control":
                negative_pass.extend(
                    query["expected_entity_id"]
                    not in rankings[query["id"]][name][:10]
                    for query in selected
                )
                by_condition[query_type] = {
                    "trials": len(selected),
                    "pass": negative,
                    "pass_rate": negative / len(selected) if selected else 0.0,
                }
            else:
                by_condition[query_type] = {
                    "trials": len(selected),
                    "top1": top1,
                    "top1_rate": top1 / len(selected) if selected else 0.0,
                    "top10": top10,
                    "top10_rate": top10 / len(selected) if selected else 0.0,
                }
        config_aggregates[name] = {
            "exact_positive_top1": {
                "trials": len(positive_top1),
                "hits": sum(positive_top1),
                "rate": sum(positive_top1) / len(positive_top1) if positive_top1 else 0.0,
            },
            "exact_positive_top10": {
                "trials": len(positive_top10),
                "hits": sum(positive_top10),
                "rate": sum(positive_top10) / len(positive_top10) if positive_top10 else 0.0,
            },
            "negative_target_absent_top10": {
                "trials": len(negative_pass),
                "pass": sum(negative_pass),
                "pass_rate": sum(negative_pass) / len(negative_pass) if negative_pass else 0.0,
            },
            "empty_results": sum(
                not rankings[query["id"]][name]
                for query in queries
            ),
            "errors": errors_by_config.get(name, 0),
            "by_condition": by_condition,
            "latency_ms": {
                "n": len(result.get("latencies_ms", {}).get(name, [])),
                "mean": statistics.fmean(result.get("latencies_ms", {}).get(name, []))
                if result.get("latencies_ms", {}).get(name)
                else None,
                "p50": _percentile(result.get("latencies_ms", {}).get(name, []), 0.50),
                "p95": _percentile(result.get("latencies_ms", {}).get(name, []), 0.95),
                "direct_service_diagnostic_only": True,
            },
            "wins_losses_ties_vs_control": _win_loss_tie(
                positive_top1, control_top1
            ),
            "negative_pass_wins_losses_ties_vs_control": _win_loss_tie(
                negative_pass, control_negative_pass
            ),
            "channels": _channel_aggregate(name, diagnostics, queries, conn),
        }
    return {"control": CONTROL_NAME, "configs": config_aggregates}


def _prepare_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    db_path = Path(args.db_path).resolve()
    if not db_path.is_file():
        raise ValueError("--db-path must point to an existing disposable database copy")
    _refuse_unsafe_db_path(str(db_path))
    out_path = Path(args.out)
    checkpoint_path = Path(args.checkpoint_path or f"{out_path}.checkpoint.json")
    signed_manifest_path = Path(args.signed_manifest_out or f"{out_path}.manifest.json")
    return db_path, out_path, checkpoint_path, signed_manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="Disposable copy of the frozen corpus.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", required=True, help="Machine-readable replay result JSON.")
    parser.add_argument("--signed-manifest-out", help="Bound manifest output path.")
    parser.add_argument("--checkpoint-path")
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--random-seed", type=int, default=0)
    parser.add_argument("--commit-fingerprint")
    parser.add_argument("--corpus-fingerprint")
    parser.add_argument("--judge-version-fingerprint")
    parser.add_argument("--machine-fingerprint")
    args = parser.parse_args(argv)
    if args.limit <= 0 or args.checkpoint_every < 0:
        parser.error("--limit must be positive and --checkpoint-every cannot be negative")

    db_path, out_path, checkpoint_path, signed_manifest_path = _prepare_paths(args)
    configs = frozen_configs()
    _validate_frozen_configs(configs)
    original_reranker_model = os.environ.get("SALTMDB_RERANKER_MODEL")
    os.environ["SALTMDB_RERANKER_MODEL"] = RERANKER_MODEL
    conn = None
    try:
        commit_fp = args.commit_fingerprint or git_commit_fingerprint(_REPO_ROOT)
        corpus_fp = args.corpus_fingerprint or file_fingerprint(db_path)
        judge_fp = args.judge_version_fingerprint or judge_version_fingerprint()
        machine_fp = args.machine_fingerprint or machine_fingerprint()
        signed_manifest, queries = load_or_bind_manifest(
            args.manifest,
            configs,
            commit_fingerprint=commit_fp,
            corpus_fingerprint=corpus_fp,
            random_seed=args.random_seed,
            judge_fingerprint=judge_fp,
            machine_marker=machine_fp,
        )
        _atomic_json_write(signed_manifest_path, signed_manifest)
        ce_preflight = preflight_cross_encoder_configs(configs)
        resume_meta = {
            "manifest_fingerprint": signed_manifest["manifest_fingerprint"],
            "config_fingerprint": signed_manifest["config_fingerprint"],
            "limit": args.limit,
            "random_seed": args.random_seed,
        }
        resume_result = None
        if args.resume:
            if not checkpoint_path.exists():
                raise RuntimeError(f"checkpoint does not exist: {checkpoint_path}")
            resume_result = _load_json(checkpoint_path)
        conn = get_connection(str(db_path))
        result = run_replay_for_queries(
            conn,
            str(db_path),
            queries,
            configs,
            limit=args.limit,
            checkpoint_path=checkpoint_path,
            checkpoint_every=args.checkpoint_every,
            resume_result=resume_result,
            resume_meta=resume_meta,
        )
        result["meta"] = {
            "schema_version": 1,
            "manifest_fingerprint": signed_manifest["manifest_fingerprint"],
            "config_fingerprint": signed_manifest["config_fingerprint"],
            "n_queries": len(queries),
            "n_configs": len(configs),
            "limit": args.limit,
            "query_order": "source/trial/condition order from signed historical manifest; configs interleaved per query",
            "reranker_model": RERANKER_MODEL,
            "cross_encoder_preflight": ce_preflight,
            "provenance": signed_manifest["provenance"],
        }
        result["queries"] = queries
        result["config_manifest"] = configs
        result["aggregates"] = aggregate_results(result, queries, configs, conn)
        result["artifact_fingerprint"] = _fingerprint(result)
        _atomic_json_write(checkpoint_path, result)
        _atomic_json_write(out_path, result)
        print(f"Wrote {out_path} ({len(queries)} queries x {len(configs)} configs)")
        if result["errors"]:
            print(
                f"Replay completed with {len(result['errors'])} recorded errors; see artifact.",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        if conn is not None:
            close_connection(conn)
        if original_reranker_model is None:
            os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        else:
            os.environ["SALTMDB_RERANKER_MODEL"] = original_reranker_model


if __name__ == "__main__":
    raise SystemExit(main())
