"""Phase 1c of the Codex live-retrieval-audit investigation.

SALTMDB memory `fad8df05` (handover) / `cd006191` (Phase 1a recovery) /
`.claude/plans/hey-so-i-asked-shimmying-raccoon.md` (full plan, Codex round-4 approved).

Snapshot-only recording-proxy harness: replays the recovered original `search_memory()`
calls (`recovered_audit_calls.RECOVERED_CALLS`, transcribed verbatim from Codex's own
session transcript -- see that module's docstring) against a WAL-safe snapshot of the
live DB, and produces the full per-candidate evidence table the investigation plan's
Phase 1-2 need. It does NOT modify production code and NEVER opens a live DB path.

Design constraints (plan Phase 1c, all enforced below):
  - Refuses any db_path that isn't an explicit snapshot file living under a `snapshot`-
    named directory (see `_refuse_live_path`) -- belt-and-suspenders on top of Phase 0's
    "exactly one Connection.backup() read" already having happened in Phase 1b.
  - Wraps every named seam as a RECORDING PROXY (unittest.mock.patch with a side_effect
    that calls the real, original function and records args/result) -- same seam-patching
    idiom tests/test_relevance_gate.py already uses, applied non-destructively (patches are
    undone via context-manager exit after each call).
  - Each evidence row carries request_id (one per recovered call) + attempt_index
    (incremented once per `_run_fts_search` invocation, since mode="strict"'s overfetch
    loop can invoke the whole pipeline multiple times per request).
  - `_run_fts_search`'s FTS AND->OR fallback branch is independently probed (a read-only
    equivalent AND-only query issued directly against the snapshot, mirroring the same
    where_clauses/params/limit/offset) so the evidence table records whether the AND path
    would have returned zero rows, without needing any production code change.
  - Topic-rerank / cross-encoder reordering has no dedicated seam of its own (both are
    inline `sorted(...)` calls inside `search_memory`) -- the harness reconstructs that
    order from the recorded raw scores using the identical sort key, and labels it
    "reconstructed" rather than "observed".
  - Runtime state is emulated to match Phase 1a's recovered daemon state (SALTMDB_ENABLE_
    SEMANTIC=true, SALTMDB_RERANKER_MODEL unset) BEFORE importing any saltmdb module.

Usage:
    python scripts/diagnostics/live_audit_repro.py --snapshot <path to .db snapshot>
        [--out <output json path>] [--label-filter <substring>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

# --- Runtime-state emulation MUST happen before any saltmdb import (Phase 1a finding:
# live daemon ran with SALTMDB_ENABLE_SEMANTIC=true, SALTMDB_RERANKER_MODEL unset). -------
os.environ["SALTMDB_ENABLE_SEMANTIC"] = "true"
os.environ.pop("SALTMDB_RERANKER_MODEL", None)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from unittest.mock import patch  # noqa: E402

import recovered_audit_calls  # noqa: E402


SEAM_NAMES = [
    "_run_fts_search",
    "semantic_search",  # bonus: not in the plan's must-cover list, but cheap and useful
    "reciprocal_rank_fusion",
    "_rrf_gap_confident",
    "rerank_candidates_by_topic",
    "_score_topics_with_fallback",
    "_batch_semantic_similarities",
    "_build_candidate_evidence",
    "accept_or_abstain",
    "_apply_type_bias",
    "_apply_supersession_demotion",
    "_apply_strict_ranking_defaults",
    "_resolve_supersession_chains",
    "_substitute_resolved_heads",
]


def _refuse_live_path(db_path: str) -> None:
    """Belt-and-suspenders guard: refuse anything that isn't an explicit snapshot file.

    Phase 0's live-access allowlist already limits this investigation to exactly one
    Connection.backup() read (done in Phase 1b); this harness must never be the second one,
    even by accident (e.g. a caller passing get_db_path()'s default by mistake).
    """
    resolved = Path(db_path).resolve()
    from saltmdb.config import get_db_path

    live_resolved = Path(get_db_path()).resolve()
    if resolved == live_resolved:
        raise RuntimeError(f"REFUSED: db_path resolves to the live DB path: {resolved}")
    if "snapshot" not in resolved.name.lower() and "snapshot" not in str(resolved.parent).lower():
        raise RuntimeError(
            f"REFUSED: db_path does not look like a snapshot (no 'snapshot' in path): {resolved}"
        )
    if not resolved.exists():
        raise RuntimeError(f"snapshot db_path does not exist: {resolved}")


class RequestRecorder:
    """Accumulates evidence rows for ONE recovered call (one request_id).

    attempt_index increments once per `_run_fts_search` call -- mode="strict"'s overfetch
    loop re-invokes the whole `_compute_pool` closure (and therefore _run_fts_search) up to
    several times per request as candidate_window doubles; without this index, evidence
    from different overfetch attempts on the same logical query would merge ambiguously.
    """

    def __init__(self, request_id: str, label: str):
        self.request_id = request_id
        self.label = label
        self.attempt_index = 0
        self.rows: list[dict] = []

    def bump_attempt(self):
        self.attempt_index += 1

    def record(self, seam: str, args, kwargs, result, extra: dict | None = None):
        row = {
            "request_id": self.request_id,
            "label": self.label,
            "attempt_index": self.attempt_index,
            "seam": seam,
            "args_repr": _safe_repr(args),
            "kwargs_repr": _safe_repr(kwargs),
            "result_repr": _safe_repr(result),
            # Kept in-process only for same-run reconstruction (e.g. topic/cross-encoder
            # inline sort order); stripped before JSON serialization by `sanitize_rows_for_json`.
            "_raw_result": result,
            "_raw_args": args,
        }
        if extra:
            row.update(extra)
        self.rows.append(row)


def sanitize_rows_for_json(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if not k.startswith("_raw_")} for row in rows]


def _safe_repr(obj, max_len: int = 4000) -> str:
    try:
        s = repr(obj)
    except Exception as e:  # pragma: no cover - defensive only
        s = f"<repr failed: {e}>"
    return s if len(s) <= max_len else s[:max_len] + f"...<truncated {len(s) - max_len} chars>"


def _probe_and_only_fts(memory_service, conn, sanitized_query, where_clauses, params, limit, offset):
    """Independently issue the equivalent AND-only FTS5 query `_run_fts_search` would run,
    using the SAME sql shape (bm25 weights, snippet extraction, where_clauses/params/limit/
    offset) -- reproduced here rather than imported, since the AND branch is not exposed as
    its own callable in production code. Returns (and_only_row_count, and_only_query_str).
    """
    ms = memory_service
    raw_terms = sanitized_query.split()
    terms = [t for t in raw_terms if t.lower() not in ms.STOP_WORDS] or raw_terms
    if not terms:
        return 0, ""
    fts_query_str = " ".join(f'"{t}"*' for t in terms)
    where_sql = f" AND {' AND '.join(where_clauses)}" if where_clauses else ""
    bm25_weights = f"{ms.BM25_TITLE_WEIGHT}, {ms.BM25_CONTENT_WEIGHT}, {ms.BM25_ALIAS_WEIGHT}"
    sql = f"""
        SELECT e.id
        FROM entities_fts fts
        JOIN entities e ON fts.id = e.id
        WHERE fts.entities_fts MATCH ?{where_sql}
        ORDER BY (bm25(entities_fts, {bm25_weights}) * e.weight) ASC
        LIMIT ? OFFSET ?
    """
    exec_params = [fts_query_str] + list(params) + [limit, offset]
    rows = conn.execute(sql, exec_params).fetchall()
    return len(rows), fts_query_str


def build_patches(memory_service, reranker_service, recorder: RequestRecorder):
    """Returns a list of unittest.mock.patch context managers, one per seam, each recording
    into `recorder` and delegating to the real original function.
    """
    ctxs = []

    originals = {name: getattr(memory_service, name) for name in SEAM_NAMES}
    reranker_original = reranker_service.score_pairs

    def make_side_effect(name, original):
        def _side_effect(*args, **kwargs):
            if name == "_run_fts_search":
                recorder.bump_attempt()
                conn, sanitized_query, where_clauses, params, limit, offset = args[:6]
                and_count, and_query = _probe_and_only_fts(
                    memory_service, conn, sanitized_query, where_clauses, params, limit, offset
                )
                result = original(*args, **kwargs)
                # H1 fix (memory `ed7cc8d5`): production now calls _run_fts_search with
                # return_fallback_flag=True (its single call site, _compute_pool), so `result`
                # is a (rows, used_or_fallback) 2-tuple, not a bare list -- unwrap and use the
                # real flag directly rather than re-deriving it from len(result), which would
                # silently become a constant 2 (always truthy) once the call requests the tuple
                # form. Still handle a bare-list result defensively, in case this harness is ever
                # pointed at a call site that doesn't request the flag.
                if kwargs.get("return_fallback_flag"):
                    rows, or_fallback_fired = result
                else:
                    rows = result
                    or_fallback_fired = and_count == 0 and len(rows) > 0
                recorder.record(
                    name, args[1:], kwargs, rows,
                    extra={
                        "and_only_would_return_rows": and_count,
                        "and_only_query": and_query,
                        "or_fallback_fired": or_fallback_fired,
                        "final_row_count": len(rows),
                    },
                )
                return result
            result = original(*args, **kwargs)
            recorder.record(name, args, kwargs, result)
            return result

        return _side_effect

    for name in SEAM_NAMES:
        original = originals[name]
        ctxs.append(
            patch.object(memory_service, name, side_effect=make_side_effect(name, original))
        )

    def _reranker_side_effect(*args, **kwargs):
        result = reranker_original(*args, **kwargs)
        recorder.record("reranker_service.score_pairs", args, kwargs, result)
        return result

    ctxs.append(patch.object(reranker_service, "score_pairs", side_effect=_reranker_side_effect))
    return ctxs


def reconstruct_topic_rerank_order(recorder: RequestRecorder, pre_rerank_pool_ids: list[str] | None) -> dict | None:
    """Reconstruct `sorted(pool_ids, key=lambda eid: -topic_scores_map_[eid]["topic_score"])`
    (search_memory's inline full-override topic-rerank sort, no dedicated seam of its own)
    from the LAST `_score_topics_with_fallback` call's raw result in this request (the
    final/widest overfetch attempt's scoring, matching what the real code would have sorted
    with on its last, decisive pass). Labeled "reconstructed" per the plan's explicit
    instruction, since it re-derives rather than directly observes the resulting order.
    """
    topic_rows = [
        r for r in recorder.rows
        if r["seam"] in ("_score_topics_with_fallback", "rerank_candidates_by_topic")
    ]
    if not topic_rows:
        return None
    last = topic_rows[-1]
    scores_map = last.get("_raw_result")
    if not isinstance(scores_map, dict):
        return None
    pool = pre_rerank_pool_ids if pre_rerank_pool_ids is not None else list(scores_map.keys())
    scored_pool = [eid for eid in pool if eid in scores_map]
    order = sorted(scored_pool, key=lambda eid: -scores_map[eid]["topic_score"])
    return {
        "source_attempt_index": last["attempt_index"],
        "source_seam": last["seam"],
        "reconstructed_order": order,
        "topic_scores": {eid: scores_map[eid]["topic_score"] for eid in order},
        "semantic_verdicts": {eid: scores_map[eid]["semantic_verdict"] for eid in order},
        "label": "reconstructed",
    }


def reconstruct_cross_encoder_order(recorder: RequestRecorder) -> dict | None:
    """Same reconstruction shape as `reconstruct_topic_rerank_order`, for the cross-encoder's
    own inline full-override sort. In this audit, cross-encoder never ran (Phase 1a confirmed
    no recovered call passed use_cross_encoder=True), so this is expected to return None for
    every recovered call -- present for completeness / future H4 follow-up experiments.
    """
    ce_rows = [r for r in recorder.rows if r["seam"] == "reranker_service.score_pairs"]
    if not ce_rows:
        return None
    last = ce_rows[-1]
    return {
        "source_attempt_index": last["attempt_index"],
        "raw_scores": last.get("_raw_result"),
        "label": "reconstructed",
    }


def run_one_call(memory_service, reranker_service, snapshot_path: str, label: str, kwargs: dict) -> dict:
    request_id = str(uuid.uuid4())
    recorder = RequestRecorder(request_id, label)
    patches = build_patches(memory_service, reranker_service, recorder)

    call_kwargs = dict(kwargs)
    call_kwargs["db_path"] = snapshot_path

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            result = memory_service.search_memory(**call_kwargs)
            error = None
        except Exception as e:  # pragma: no cover - captured as evidence, not raised
            result = None
            error = f"{type(e).__name__}: {e}"

    # Pre-rerank pool = the RRF fusion order at the last (widest) attempt, i.e. the
    # `list(rrf_map.keys())` search_memory itself would sort by topic_score -- taken from the
    # last reciprocal_rank_fusion call's raw result (already rank-ordered dict).
    rrf_rows = [r for r in recorder.rows if r["seam"] == "reciprocal_rank_fusion"]
    pre_rerank_pool_ids = None
    if rrf_rows:
        last_rrf = rrf_rows[-1]["_raw_result"]
        if isinstance(last_rrf, dict):
            pre_rerank_pool_ids = list(last_rrf.keys())

    reconstructed_topic_order = (
        reconstruct_topic_rerank_order(recorder, pre_rerank_pool_ids)
        if kwargs.get("rerank_by_topic")
        else None
    )
    reconstructed_cross_encoder_order = (
        reconstruct_cross_encoder_order(recorder) if kwargs.get("use_cross_encoder") else None
    )

    # Final rank pre/post each ranking-flag stage: directly OBSERVED (not reconstructed) from
    # the recorded args (pre) / result (post) of the three stage functions, at their LAST
    # (widest/final) attempt in this request -- exactly what "final rank pre/post each
    # ranking-flag stage" in the plan's Phase 1c spec asks for.
    stage_pre_post = {}
    for seam in ("_apply_type_bias", "_apply_supersession_demotion", "_apply_strict_ranking_defaults"):
        seam_rows = [r for r in recorder.rows if r["seam"] == seam]
        if seam_rows:
            last = seam_rows[-1]
            stage_pre_post[seam] = {
                "attempt_index": last["attempt_index"],
                "pre": last["_raw_args"][0] if last["_raw_args"] else None,
                "post": last["_raw_result"],
                "label": "observed",
            }

    final_ids = []
    if isinstance(result, list):
        final_ids = [item.get("id") for item in result if isinstance(item, dict)]
    elif isinstance(result, dict) and "error" in result:
        error = error or result["error"]

    return {
        "request_id": request_id,
        "label": label,
        "kwargs": kwargs,
        "error": error,
        "final_result_ids": final_ids,
        "final_result_titles": [item.get("title") for item in result] if isinstance(result, list) else None,
        "final_result_topic_scores": (
            [item.get("topic_score") for item in result] if isinstance(result, list) else None
        ),
        "evidence_rows": sanitize_rows_for_json(recorder.rows),
        "reconstructed_topic_order": reconstructed_topic_order,
        "reconstructed_cross_encoder_order": reconstructed_cross_encoder_order,
        "stage_pre_post_ranking": stage_pre_post,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", required=True, help="Path to a WAL-safe snapshot .db file (must contain 'snapshot' in its path).")
    ap.add_argument("--out", default=None, help="Output JSON path for the evidence table.")
    ap.add_argument("--label-filter", default=None, help="Only run recovered calls whose label contains this substring.")
    args = ap.parse_args()

    _refuse_live_path(args.snapshot)

    from saltmdb.domain.services import memory_service, reranker_service

    calls = recovered_audit_calls.RECOVERED_CALLS
    if args.label_filter:
        calls = [c for c in calls if args.label_filter in c["label"]]

    print(f"Replaying {len(calls)} recovered calls against snapshot: {args.snapshot}")
    all_results = []
    for i, call in enumerate(calls):
        print(f"[{i + 1}/{len(calls)}] {call['label']} ...", file=sys.stderr)
        out = run_one_call(memory_service, reranker_service, args.snapshot, call["label"], call["kwargs"])
        out["source_exec_call_id"] = call["source_exec_call_id"]
        out["expected_id"] = call["expected_id"]
        if call["expected_id"]:
            out["expected_rank"] = (
                out["final_result_ids"].index(call["expected_id"]) + 1
                if call["expected_id"] in out["final_result_ids"]
                else None
            )
        all_results.append(out)

    out_path = args.out or str(REPO_ROOT.parent / "scratch_diag_out.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nWrote {len(all_results)} call results to {out_path}")


if __name__ == "__main__":
    main()
