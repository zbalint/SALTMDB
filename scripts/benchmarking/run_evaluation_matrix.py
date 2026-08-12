"""Runs the 24-config candidate matrix (`eval_configs.py`) against a generated query set, per
plan §3 (`scratch/plans/precision_first_search_evaluation.md`). Produces the per-query judgment
pools and per-config rankings that `judge_pool.py` and `analyze_evaluation_matrix.py` consume.

Real compute against a throwaway copy of `scratch/diverse_corpus_full.db` -- never the live DB,
never the shared frozen fixture file directly (§0 item 9's hardened guard, below). Chunked with
periodic checkpoint writes so a long run surviving an interruption doesn't restart from zero,
same pattern `build_diverse_test_db.py` already uses.

Not run yet this session -- built and unit-testable (via a tiny synthetic query set against the
real frozen corpus copy) per the user's "build scripts first, hold off dispatching" instruction;
this script's own compute is local (not a CADET/codex dispatch), so a *small* smoke-test run
against a couple of real queries is a legitimate correctness check, not "dispatching a batch."
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(_REPO_ROOT / "src"))
from eval_configs import _build_evaluation_configs  # noqa: E402
from analyze_evaluation_matrix import validate_frozen_shortlist  # noqa: E402
from bakeoff_state import authorize_blind_file  # noqa: E402
from build_evaluation_queries import validate_queries  # noqa: E402
from evaluation_artifacts import (  # noqa: E402
    build_provenance,
    build_query_trace,
    file_fingerprint,
    git_commit_fingerprint,
    machine_fingerprint,
    validate_query_trace,
    validate_provenance,
)
from judge_pool import judge_version_fingerprint  # noqa: E402

from saltmdb.config import get_db_path  # noqa: E402
from saltmdb.db.connection import close_connection, get_connection  # noqa: E402
from saltmdb.domain.services.memory_service import (  # noqa: E402
    get_last_search_diagnostics,
    search_memory,
)
from saltmdb.domain.services import reranker_service  # noqa: E402

SHARED_FIXTURE_PATH = Path(__file__).parent.parent.parent / "scratch" / "diverse_corpus_full.db"
RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"  # pinned, §1 / §0b item 5


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_artifact_fingerprint(value: object, *, field: str = "artifact_fingerprint") -> None:
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise ValueError(f"artifact lacks {field}")
    unsigned = dict(value)
    stored = unsigned.pop(field)
    if stored != _fingerprint(unsigned):
        raise ValueError(f"artifact {field} mismatch")


def _validate_matrix_contract(  # noqa: C901, PLR0912
    result: dict, queries: list[dict], configs: list[dict], limit: int
) -> None:
    """Prove every completed query has every configuration and structurally valid rankings."""
    query_ids = [query.get("id") for query in queries]
    if any(not isinstance(query_id, str) or not query_id for query_id in query_ids):
        raise ValueError("queries must have non-empty string IDs")
    if len(query_ids) != len(set(query_ids)):
        raise ValueError("queries contain duplicate IDs")
    config_names = [cfg.get("name") for cfg in configs]
    if len(config_names) != len(set(config_names)):
        raise ValueError("configs contain duplicate names")
    expected_queries, expected_configs = set(query_ids), set(config_names)
    rankings = result.get("config_rankings", {})
    pools = result.get("pools", {})
    traces = result.get("traces", {})
    if not set(rankings).issubset(expected_queries) or not set(pools).issubset(expected_queries):
        raise ValueError("matrix contains an unknown query")
    for query_id in rankings:
        by_config = rankings[query_id]
        if set(by_config) != expected_configs:
            raise ValueError(f"matrix query {query_id!r} lacks complete config coverage")
        for name, ranking in by_config.items():
            if not isinstance(ranking, list) or len(ranking) > limit:
                raise ValueError(f"invalid ranking for {query_id!r}/{name!r}")
            if any(
                not isinstance(candidate_id, str) or not candidate_id for candidate_id in ranking
            ):
                raise ValueError("ranking contains an invalid candidate ID")
            if len(ranking) != len(set(ranking)):
                raise ValueError("ranking contains duplicate candidate IDs")
    if set(traces) != set(rankings):
        raise ValueError("matrix traces must cover exactly the completed queries")
    for query_id, by_config in traces.items():
        if set(by_config) != expected_configs:
            raise ValueError(f"matrix query {query_id!r} lacks complete trace coverage")
        for trace in by_config.values():
            validate_query_trace(trace)
    for query_id, pool in pools.items():
        if not isinstance(pool, dict):
            raise ValueError("matrix pool is not an object")
        for candidate_id, item in pool.items():
            if not isinstance(candidate_id, str) or not isinstance(item, dict):
                raise ValueError("matrix pool contains an invalid candidate")
            if not isinstance(item.get("ground_truth_forced_include"), bool):
                raise ValueError("pool candidate lacks ground_truth_forced_include flag")
    errors = result.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("matrix errors must be a list")


def validate_matrix_artifact(value: dict, *, expected_provenance: dict | None = None) -> None:
    """Verify a finalized matrix and reject missing, malformed, or stale provenance."""
    verify_artifact_fingerprint(value)
    meta = value.get("meta", {})
    resume_meta = value.get("resume_meta", {})
    if meta.get("queries_fingerprint") != resume_meta.get("queries_fingerprint"):
        raise ValueError("matrix query fingerprint metadata mismatch")
    if meta.get("configs_fingerprint") != resume_meta.get("configs_fingerprint"):
        raise ValueError("matrix config fingerprint metadata mismatch")
    validate_provenance(value, expected_provenance, artifact_label="matrix")


def preflight_cross_encoder_configs(configs: list[dict]) -> dict | None:
    """Run the real bundled reranker before any matrix containing a CE contender.

    A disabled, malformed, or nonfinite scorer makes the experiment invalid immediately instead
    of allowing a nominal cross-encoder configuration to produce baseline-equivalent rankings.
    """
    if not any(config.get("use_cross_encoder") for config in configs):
        return None
    result = reranker_service.score_pairs_preflight(
        query="Which memory directly answers the requested fact?",
        candidates=[
            "This candidate directly answers the requested fact.",
            "This candidate discusses an unrelated subject.",
        ],
    )
    if not result.get("ready"):
        raise RuntimeError(f"cross-encoder preflight failed: {result.get('diagnostics', {})}")
    return result


def summarize_cross_encoder_execution(result: dict, configs: list[dict]) -> dict | None:
    """Fail closed when a requested CE experiment executed zero successful score calls."""
    ce_names = {config["name"] for config in configs if config.get("use_cross_encoder")}
    if not ce_names:
        return None
    requested = 0
    executed = 0
    diagnostics = result.get("execution_diagnostics", {})
    for by_config in diagnostics.values():
        for config_name, search_diagnostics in by_config.items():
            if config_name not in ce_names:
                continue
            requested += 1
            ce = search_diagnostics.get("cross_encoder", {})
            if ce.get("executed") and ce.get("execution_count", 0) > 0:
                executed += 1
    summary = {
        "configured_contenders": sorted(ce_names),
        "requested_searches": requested,
        "executed_searches": executed,
        "execution_rate": (executed / requested) if requested else 0.0,
    }
    if executed == 0:
        raise RuntimeError("cross-encoder experiment invalid: zero successful executions")
    return summary


def _load_signed_queries(path: Path, *, expected_split: str | None = None) -> tuple[list[dict], str]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get("queries"), list):
        raise ValueError("matrix input must be a signed query manifest")
    # build_evaluation_queries writes manifest_fingerprint; accepting only that signed wrapper
    # prevents a caller from swapping the query body after source-slot assignment.
    if not isinstance(value.get("manifest_fingerprint"), str):
        raise ValueError("query manifest lacks manifest_fingerprint")
    unsigned = dict(value)
    stored = unsigned.pop("manifest_fingerprint")
    if stored != _fingerprint(unsigned):
        raise ValueError("query manifest manifest_fingerprint mismatch")
    if value.get("queries_fingerprint") != _fingerprint(value["queries"]):
        raise ValueError("query manifest queries_fingerprint mismatch")
    validate_provenance(value, artifact_label="query manifest")
    validate_queries(value["queries"])
    if expected_split is not None and any(
        query.get("split") != expected_split for query in value["queries"]
    ):
        raise ValueError("query manifest contains rows outside the declared split")
    return value["queries"], value["manifest_fingerprint"]


def _atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True))
    temporary.replace(path)


def require_frozen_dev_shortlist(path: Path | None) -> None:
    """Enforce the development/blind boundary before a blind matrix can be executed."""
    if path is None or not path.exists():
        raise RuntimeError("blind matrix requires a signed --dev-shortlist")
    validate_frozen_shortlist(json.loads(path.read_text()))


def authorize_query_manifest(
    query_path: Path,
    split: str,
    *,
    dev_shortlist: Path | None = None,
    blind_vault_dir: Path | None = None,
    bakeoff_spec: Path | None = None,
    development_winner: Path | None = None,
    blind_unlock: Path | None = None,
) -> None:
    """Authorize a query manifest without opening it; development is unrestricted."""
    if split != "blind":
        return
    require_frozen_dev_shortlist(dev_shortlist)
    authorization_paths = (blind_vault_dir, bakeoff_spec, development_winner, blind_unlock)
    if any(path is None for path in authorization_paths):
        raise RuntimeError(
            "blind matrix requires --blind-vault-dir, --bakeoff-spec, "
            "--development-winner, and --blind-unlock"
        )
    assert blind_vault_dir and bakeoff_spec and development_winner and blind_unlock
    authorize_blind_file(
        query_path, blind_vault_dir, bakeoff_spec, development_winner, blind_unlock
    )


def _refuse_unsafe_db_path(db_path: str) -> None:
    """§0 item 9's hardened guard: resolved-path/samefile-safe (not the sibling scripts' raw
    string compare), refuses BOTH the live default DB path AND the shared frozen fixture file
    itself (every run must operate on a throwaway COPY, never that shared file directly)."""
    resolved = os.path.realpath(db_path)
    live_resolved = os.path.realpath(get_db_path())
    if resolved == live_resolved:
        raise RuntimeError(
            f"Refusing to run against the live default DB path ({live_resolved}). "
            "Point --db-path at a throwaway copy instead."
        )
    fixture_resolved = os.path.realpath(SHARED_FIXTURE_PATH)
    if resolved == fixture_resolved:
        raise RuntimeError(
            f"Refusing to run against the shared frozen fixture file itself "
            f"({fixture_resolved}). Copy it to a throwaway path first."
        )


def run_one_config(
    conn, db_path: str, query_text: str, config: dict, limit: int = 20
) -> tuple[list[dict], float, str | None]:
    """Calls search_memory for one (query, config) pair. Returns (items, latency_ms, error).
    items is [] on either a genuine empty result OR a caught exception (error will be set in the
    latter case) OR a strict-mode abstention (a real, structurally-identical empty list -- not
    distinguished from "no results" here, matching how every sibling benchmark script in this
    directory already treats mode="strict"'s abstention as an ordinary empty result)."""
    t0 = time.perf_counter()
    try:
        result = search_memory(
            query_keywords=query_text,
            db_path=db_path,
            db_connection=conn,
            limit=limit,
            mode=config["mode"],
            rerank_by_topic=config["rerank_by_topic"],
            prefer_durable_types=config["prefer_durable_types"],
            demote_superseded=config["demote_superseded"],
            use_cross_encoder=config["use_cross_encoder"],
            cross_encoder_candidate_cap=config.get("cross_encoder_candidate_cap"),
            cross_encoder_text_cap_chars=config.get("cross_encoder_text_cap_chars"),
            force_cross_encoder=config.get("force_cross_encoder", False),
            use_chunk_candidates=config.get("use_chunk_candidates", False),
            oversampling_multiplier=config.get("oversampling_multiplier"),
            candidate_window=config.get("candidate_window"),
            chunk_weight=config.get("chunk_weight"),
            collapse_supersedes_families=config.get("collapse_supersedes_families", False),
            use_retrieval_text_candidates=config.get("use_retrieval_text_candidates", False),
            retrieval_fts_weight=config.get("retrieval_fts_weight"),
            retrieval_vector_weight=config.get("retrieval_vector_weight"),
            include_related=False,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if isinstance(result, dict) and "error" in result:
            return [], elapsed_ms, str(result["error"])
        if not isinstance(result, list):
            return [], elapsed_ms, f"unexpected result type: {type(result)!r}"
        # ``search_memory``'s compatibility boundary may return a one-item error list when a
        # runtime dependency (for example sqlite_vec) is unavailable.  Treat that as a structured
        # matrix failure rather than dereferencing ``item["id"]`` and losing the whole checkpoint.
        if any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in result
        ):
            if len(result) == 1 and isinstance(result[0], dict) and result[0].get("error"):
                return [], elapsed_ms, str(result[0]["error"])
            return [], elapsed_ms, "unexpected result item shape"
        return result, elapsed_ms, None
    except Exception as e:  # search_memory is documented to catch+wrap most cases; this is a
        # belt-and-suspenders guard so one bad (query, config) pair can't kill the whole run.
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return [], elapsed_ms, f"{type(e).__name__}: {e}"


def _fetch_entity_stub(conn, entity_id: str) -> dict | None:
    """Fetches {id, title, snippet} for a force-included ground-truth entity that no config
    happened to retrieve (§0b item 17) -- same direct read-only SELECT-against-the-throwaway-copy
    convention benchmark_precision_snapshot.py's own _expected_entity_info already uses (not a
    new pattern). snippet here is the first 500 chars of full_content (a config's own snippet
    would be FTS-match-centered; a force-included item has no FTS match to center on, so a
    leading-text snippet is the reasonable fallback for a judge to read)."""
    row = conn.execute(
        "SELECT id, title, full_content, memory_type FROM entities WHERE id = ? "
        "AND status != 'archived'",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    eid, title, full_content, memory_type = row
    snippet = (full_content or "")[:500]
    return {"id": eid, "title": title, "snippet": snippet, "memory_type": memory_type}


def _run_query_configs(
    conn, db_path: str, query: dict, configs: list[dict], limit: int
) -> tuple[
    dict[str, list[str]],
    dict[str, dict],
    dict[str, float],
    list[dict],
    dict[str, dict],
    dict[str, dict],
]:
    query_id = query["id"]
    rankings: dict[str, list[str]] = {}
    pool: dict[str, dict] = {}
    latencies: dict[str, float] = {}
    errors: list[dict] = []
    diagnostics: dict[str, dict] = {}
    traces: dict[str, dict] = {}
    for cfg in configs:
        items, latency_ms, error = run_one_config(conn, db_path, query["query"], cfg, limit=limit)
        latencies[cfg["name"]] = latency_ms
        diagnostics[cfg["name"]] = get_last_search_diagnostics()
        diag = diagnostics[cfg["name"]]
        traces[cfg["name"]] = build_query_trace(
            query["id"],
            config_name=cfg["name"],
            bm25=diag.get(
                "bm25",
                {
                    "candidate_count": diag.get("fts_candidate_count", diag.get("fts_count", 0)),
                },
            ),
            dense_entity=diag.get("dense_entity", diag.get("semantic", {})),
            dense_chunk=diag.get("chunk_candidate", {}),
            late_interaction=diag.get("late_interaction", {}),
            retrieval_text=diag.get("retrieval_text", {}),
            lifecycle=diag.get(
                "lifecycle",
                {"supersedes_collapse": diag.get("supersedes_collapse", {})},
            ),
            reranker=diag.get("cross_encoder", diag.get("reranker", {})),
        )
        validate_query_trace(traces[cfg["name"]])
        if error:
            errors.append({"query_id": query_id, "config_name": cfg["name"], "error": error})
        rankings[cfg["name"]] = [item["id"] for item in items]
        # The pool is the contender union of each configuration's *top 20*, even when a
        # diagnostic run asks the search adapter for a longer ranking.
        for item in items[:20]:
            pool.setdefault(
                item["id"],
                {
                    "title": item.get("title"),
                    "snippet": item.get("snippet"),
                    "memory_type": item.get("memory_type"),
                    "ground_truth_forced_include": False,
                },
            )
    for source_id in query.get("source_entity_ids") or []:
        if source_id in pool:
            continue
        stub = _fetch_entity_stub(conn, source_id)
        if stub is None:
            errors.append(
                {
                    "query_id": query_id,
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
    return rankings, pool, latencies, errors, diagnostics, traces


def run_matrix_for_queries(
    conn,
    db_path: str,
    queries: list[dict],
    configs: list[dict],
    limit: int = 20,
    progress_every: int = 50,
    checkpoint_path: Path | None = None,
    checkpoint_every: int = 10,
    resume_result: dict | None = None,
    resume_meta: dict | None = None,
) -> dict:
    """queries: list of {"id": ..., "query": ..., "source_entity_ids": [...], "category": ...}
    (the dev or blind QueryRow set, as dicts). Returns:
      {
        "config_rankings": {query_id: {config_name: [ordered candidate ids]}},
        "pools": {query_id: {candidate_id: {"title", "snippet", "memory_type",
                                             "ground_truth_forced_include": bool}}},
        "latencies_ms": {config_name: [floats]},
        "errors": [{"query_id", "config_name", "error"}],
        "execution_diagnostics": {"query_id": {"config_name": {"cross_encoder": ...}}},
      }
    """
    resume_result = resume_result or {}
    config_rankings: dict[str, dict[str, list[str]]] = resume_result.get("config_rankings", {})
    pools: dict[str, dict[str, dict]] = resume_result.get("pools", {})
    latencies_ms: dict[str, list[float]] = resume_result.get(
        "latencies_ms", {cfg["name"]: [] for cfg in configs}
    )
    errors: list[dict] = resume_result.get("errors", [])
    execution_diagnostics: dict[str, dict] = resume_result.get("execution_diagnostics", {})
    execution_traces: dict[str, dict] = resume_result.get("traces", {})
    completed_query_ids = set(resume_result.get("completed_query_ids", config_rankings))
    known_query_ids = {query["id"] for query in queries}
    if not completed_query_ids.issubset(known_query_ids):
        raise ValueError("checkpoint contains a query absent from this manifest")
    for query_id in completed_query_ids:
        if query_id not in config_rankings or query_id not in pools:
            raise ValueError("checkpoint marks an incomplete query as completed")

    total = len(queries)
    for i, q in enumerate(queries):
        query_id = q["id"]
        if query_id in completed_query_ids:
            continue
        (
            rankings,
            pool_for_query,
            query_latencies,
            query_errors,
            query_diagnostics,
            query_traces,
        ) = _run_query_configs(conn, db_path, q, configs, limit)
        config_rankings[query_id] = rankings
        for config_name, latency_ms in query_latencies.items():
            latencies_ms[config_name].append(latency_ms)
        errors.extend(query_errors)
        pools[query_id] = pool_for_query
        execution_diagnostics[query_id] = query_diagnostics
        execution_traces[query_id] = query_traces

        completed_query_ids.add(query_id)
        if (
            checkpoint_path
            and checkpoint_every
            and len(completed_query_ids) % checkpoint_every == 0
        ):
            _atomic_json_write(
                checkpoint_path,
                {
                    "config_rankings": config_rankings,
                    "pools": pools,
                    "latencies_ms": latencies_ms,
                    "errors": errors,
                    "execution_diagnostics": execution_diagnostics,
                    "traces": execution_traces,
                    "completed_query_ids": sorted(completed_query_ids),
                    "resume_meta": resume_meta,
                },
            )
        if progress_every and (i + 1) % progress_every == 0:
            print(f"  ... {i + 1}/{total} queries done", file=sys.stderr)

    result = {
        "config_rankings": config_rankings,
        "pools": pools,
        "latencies_ms": latencies_ms,
        "errors": errors,
        "execution_diagnostics": execution_diagnostics,
        "traces": execution_traces,
        "completed_query_ids": sorted(completed_query_ids),
    }
    _validate_matrix_contract(result, queries, configs, limit)
    return result


def main():  # noqa: PLR0915
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="Throwaway copy of the frozen corpus.")
    parser.add_argument("--queries", required=True, help="Path to a queries_{dev,blind}.json file.")
    parser.add_argument("--split", choices=("dev", "blind"), required=True)
    parser.add_argument("--out", required=True, help="Output path for the matrix run JSON.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--corpus-fingerprint",
        help="Fingerprint from the frozen corpus manifest (defaults to the throwaway DB file SHA-256).",
    )
    parser.add_argument("--random-seed", type=int, default=0)
    parser.add_argument("--commit-fingerprint", help="Full git commit hash; defaults to HEAD.")
    parser.add_argument(
        "--judge-version-fingerprint",
        help="Frozen grade/rubric fingerprint; defaults to the Stage-1 judge contract.",
    )
    parser.add_argument(
        "--machine-fingerprint",
        help="Fixed machine marker for latency diagnostics; defaults to the current host marker.",
    )
    parser.add_argument("--checkpoint-path")
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--dev-shortlist",
        type=Path,
        help="Signed development shortlist, required when --queries is the blind manifest.",
    )
    parser.add_argument("--blind-vault-dir", type=Path)
    parser.add_argument("--bakeoff-spec", type=Path)
    parser.add_argument("--development-winner", type=Path)
    parser.add_argument("--blind-unlock", type=Path)
    parser.add_argument(
        "--config-name",
        action="append",
        dest="config_names",
        help="Run only named configuration(s); diagnostic-only, never a final matrix artifact.",
    )
    args = parser.parse_args()

    # Blind authorization is validated before the first read of the blind query manifest.
    authorize_query_manifest(
        Path(args.queries),
        args.split,
        dev_shortlist=args.dev_shortlist,
        blind_vault_dir=args.blind_vault_dir,
        bakeoff_spec=args.bakeoff_spec,
        development_winner=args.development_winner,
        blind_unlock=args.blind_unlock,
    )

    _refuse_unsafe_db_path(args.db_path)
    os.environ["SALTMDB_RERANKER_MODEL"] = RERANKER_MODEL  # §0b item 5

    queries, queries_manifest_fingerprint = _load_signed_queries(
        Path(args.queries), expected_split=args.split
    )

    configs = _build_evaluation_configs()
    if args.config_names:
        wanted = set(args.config_names)
        configs = [config for config in configs if config["name"] in wanted]
        if {config["name"] for config in configs} != wanted:
            raise RuntimeError("unknown --config-name")
    cross_encoder_preflight = preflight_cross_encoder_configs(configs)
    commit_fp = args.commit_fingerprint or git_commit_fingerprint(_REPO_ROOT)
    corpus_fp = args.corpus_fingerprint or file_fingerprint(Path(args.db_path))
    judge_fp = args.judge_version_fingerprint or judge_version_fingerprint()
    machine_fp = args.machine_fingerprint or machine_fingerprint()
    provenance = build_provenance(
        commit_fingerprint=commit_fp,
        corpus_fingerprint=corpus_fp,
        query_manifest_fingerprint=queries_manifest_fingerprint,
        random_seed=args.random_seed,
        config_fingerprint=_fingerprint(configs),
        judge_version_fingerprint=judge_fp,
        machine_fingerprint_value=machine_fp,
        model_fingerprint=RERANKER_MODEL,
        rubric_fingerprint=judge_fp,
        artifact_kind="matrix",
    )
    checkpoint_path = Path(args.checkpoint_path or (args.out + ".checkpoint.json"))
    expected = {
        "queries_fingerprint": _fingerprint(queries),
        "configs_fingerprint": _fingerprint(configs),
        "limit": args.limit,
        "reranker_model": RERANKER_MODEL,
        "queries_manifest_fingerprint": queries_manifest_fingerprint,
        "commit_fingerprint": commit_fp,
        "corpus_fingerprint": corpus_fp,
        "random_seed": args.random_seed,
        "judge_version_fingerprint": judge_fp,
        "machine_fingerprint": machine_fp,
        "provenance_fingerprint": provenance["fingerprint"],
    }
    resume_result = None
    if args.resume:
        checkpoint = json.loads(checkpoint_path.read_text())
        if checkpoint.get("resume_meta") != expected:
            raise RuntimeError("checkpoint inputs/configuration do not match this run")
        resume_result = checkpoint

    conn = get_connection(args.db_path)
    try:
        result = run_matrix_for_queries(
            conn,
            args.db_path,
            queries,
            configs,
            limit=args.limit,
            checkpoint_path=checkpoint_path,
            checkpoint_every=args.checkpoint_every,
            resume_result=resume_result,
            resume_meta=expected,
        )
    finally:
        close_connection(conn)

    cross_encoder_execution = summarize_cross_encoder_execution(result, configs)

    result["meta"] = {
        "db_path": args.db_path,
        "queries_path": args.queries,
        "n_queries": len(queries),
        "n_configs": len(configs),
        "reranker_model_intended": RERANKER_MODEL,
        "reranker_model_env_resolved": os.environ.get("SALTMDB_RERANKER_MODEL"),
        "queries_fingerprint": expected["queries_fingerprint"],
        "configs_fingerprint": expected["configs_fingerprint"],
        "queries_manifest_fingerprint": queries_manifest_fingerprint,
        "config_manifest": configs,
        "provenance": provenance,
        "cross_encoder_preflight": cross_encoder_preflight,
        "cross_encoder_execution": cross_encoder_execution,
    }
    result["resume_meta"] = expected
    result["provenance"] = provenance
    result["artifact_fingerprint"] = _fingerprint(result)
    if result["errors"]:
        _atomic_json_write(checkpoint_path, result)
        raise RuntimeError(
            f"matrix contains {len(result['errors'])} errors; refusing final artifact"
        )
    _atomic_json_write(checkpoint_path, result)
    _atomic_json_write(Path(args.out), result)
    print(f"Wrote {args.out} ({len(queries)} queries x {len(configs)} configs)")


if __name__ == "__main__":
    main()
