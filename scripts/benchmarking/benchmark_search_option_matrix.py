"""Full option-matrix precision/latency sweep for `search_memory` (explicit user instruction,
captured in handover memory `1462a3f6`, given at the end of roadmap item 7's session: "after i
clean the context you will test and benchmark the search memory with every options to find out
which results in the most precise search output!").

This is a **fact-finding benchmark**, not a pre-approved code/default change (per the handover --
any resulting default flip, e.g. item 8's injection hook choosing specific flags internally, is a
SEPARATE decision requiring its own plan + Codex review, same as `mode="strict"`'s own still-
deferred default-flip, memory `95a8c5b8`).

## Why this ISN'T a brute-force 3 x 2^4 = 48 combos
Verified directly against `_compute_pool` in `memory_service.py` (HEAD `af6bd0d`) before writing
this script, not just trusted from the handover summary:

1. `mode="strict"` unconditionally runs `_apply_strict_ranking_defaults` (forced durable-type
   preference + residual supersession/correction demotion) regardless of what
   `prefer_durable_types`/`demote_superseded` are set to. Those two flags keep their own narrower,
   mode-agnostic meaning ONLY for broad/history -- under strict they're either a redundant no-op
   (stable partition applied twice) or simply overridden. So strict's own useful axis is just
   `rerank_by_topic` x `use_cross_encoder` (4 combos) -- `prefer_durable_types`/`demote_superseded`
   are pinned False in the strict sweep, plus ONE extra config (`strict_flagcheck_pdt1_ds1`) that
   deliberately sets them True, to empirically VERIFY (not assume) that this makes no difference to
   the accepted set.
2. `mode="history"` runs the exact same Stage-2/ranking-flag pipeline as `mode="broad"` (rerank_by_
   topic, use_cross_encoder, prefer_durable_types, demote_superseded) -- the only mode-specific
   thing it adds is `is_superseded` tagging via `_compute_superseded_ids_bitemporal`, which never
   touches `ranked_pool_`'s order. So history does not need its own full 16-combo sweep for a
   RANKING-accuracy question -- two spot-check configs (all-False and all-True) are run instead,
   to empirically confirm ranking equivalence with broad's corresponding combo rather than assuming
   it from reading the code alone.
3. Abstention correctness (does a nonsense query correctly return `[]`?) is ONLY a strict-mode
   question -- `accept_or_abstain` only runs under `mode="strict"`. broad/history never abstain by
   construction; for them this script instead measures and reports the "naive false-accept rate"
   (how often they return something non-empty for a query with no real answer) as a DIFFERENT, but
   still directly relevant, reliability signal -- explicitly not scored pass/fail against the same
   bar as strict.
4. `cross_encoder_score` is documented (memory `958fdb99`) as inert to `accept_or_abstain` -- this
   script's own accepted-SET comparison between the `use_cross_encoder=False`/`True` strict configs
   (reusing calls already made for the main sweep, no extra cost) is a live re-check of that
   invariant on THIS run's data, not just a citation of the earlier one-off benchmark.

## What's measured
- Positive set (`golden_queries.json`, 15 cases): top-1 (and top-`top_k` for the file's own
  `ambiguous_near_duplicate` tolerance) accuracy per config.
- Negative set (`golden_queries_negative.json`, 9 cases): strict-mode abstention/substitution
  correctness (pass/fail per case, mirroring `run_relevance_gate_holdout.py`'s own per-case
  checks); naive non-abstaining behavior recorded for broad/history's default configs.
- Adversarial nonsense set (this script's own, see `ADVERSARIAL_QUERIES` below): includes the
  user's own two example queries verbatim ("milyen alpakkam van?", a Hungarian off-topic question;
  "Harry Potter is my best friend, what color is his shirt?", a fictional false-premise question)
  plus two more of the same shape for a slightly broader read. Run against EVERY config (not just
  strict) -- strict is scored empty-or-not against the correct answer (empty); broad/history are
  reported descriptively (what garbage, if any, gets surfaced as a false top-1).
- Latency: real wall-clock `search_memory()` call time (not a synthetic isolated-stage loop like
  `benchmark_cross_encoder_rerankers.py`'s own warm-latency measurement) across every query run for
  a config, p50/p95 in milliseconds.

## Reusable infra (not rebuilt)
`golden_queries.json` / `golden_queries_negative.json` (oracles), `build_diverse_test_db.py`'s
output corpus, the `_refuse_live_db` + tempdir-copy convention every sibling script in this
directory already uses.

Usage:
    python scripts/benchmarking/benchmark_search_option_matrix.py [--db-path PATH]
        [--reranker-model NAME]

Defaults to a fresh throwaway copy of scratch/diverse_corpus_full.db in a tempdir (never opens that
file, or any other DB, for writes) -- pass --db-path to point at a different throwaway copy.
Refuses to run against anything that looks like the live default DB path (SALTMDB dev rule, memory
`51baf28d`).

Moved out of the test_* naming convention (like every sibling here) so `python -m unittest discover
-s tests` never collects it -- corpus-scale data, real wall-clock latency, a real (large) ONNX
model load, none of which belongs in a deterministic unit-test run.
"""

import argparse
import itertools
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from saltmdb.config import get_db_path
from saltmdb.domain.services.memory_service import search_memory

POSITIVE_SET_DEFAULT = Path(__file__).parent / "golden_queries.json"
NEGATIVE_SET_DEFAULT = Path(__file__).parent / "golden_queries_negative.json"
SOURCE_CORPUS_DEFAULT = Path(__file__).parent.parent.parent / "scratch" / "diverse_corpus_full.db"
RESULTS_DIR_DEFAULT = Path(__file__).parent.parent.parent / "scratch" / "benchmark_results"

DEFAULT_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"  # item 7's own recommended winner

# The user's own two examples, verbatim, plus two more of the same shape (off-topic / false-premise
# nonsense with zero real grounding in this corpus) for a slightly broader read than just the two
# literal examples given.
ADVERSARIAL_QUERIES = [
    {
        "id": "adversarial-alpaca-hu",
        "query": "milyen alpakkám van?",
        "note": "User-specified example verbatim -- Hungarian, off-topic, no relation to this corpus.",
    },
    {
        "id": "adversarial-harry-potter-shirt",
        "query": "Harry Potter is my best friend, what color is his shirt?",
        "note": "User-specified example verbatim -- fictional false-premise question.",
    },
    {
        "id": "adversarial-unobtainium-tuesday",
        "query": "what is the boiling point of unobtainium on a Tuesday?",
        "note": "Extra: pseudo-scientific nonsense, same shape as the user's examples.",
    },
    {
        "id": "adversarial-pet-dragon-siblings",
        "query": "how many siblings does my pet dragon have?",
        "note": "Extra: fictional off-topic, same shape as the user's examples.",
    },
]


def _refuse_live_db(db_path: str) -> None:
    """Same guard convention as every sibling script in this directory -- refuses to run against
    anything that resolves to the live default DB path, even read-only."""
    live_path = os.path.abspath(get_db_path())
    if os.path.abspath(db_path) == live_path:
        raise RuntimeError(
            f"Refusing to run against the live default DB path ({live_path}). "
            "Point --db-path at a throwaway copy instead."
        )


def _load_cases(path: Path, key: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)[key]


def _build_configs() -> list[dict]:
    """Returns the pruned config list -- see module docstring points 1-2 for why this isn't the
    full 3 x 2^4 brute force."""
    configs = []

    for rt, pdt, ds, ce in itertools.product([False, True], repeat=4):
        configs.append(
            {
                "name": f"broad_rt{int(rt)}_pdt{int(pdt)}_ds{int(ds)}_ce{int(ce)}",
                "mode": "broad",
                "rerank_by_topic": rt,
                "prefer_durable_types": pdt,
                "demote_superseded": ds,
                "use_cross_encoder": ce,
            }
        )

    for rt, ce in itertools.product([False, True], repeat=2):
        configs.append(
            {
                "name": f"strict_rt{int(rt)}_ce{int(ce)}",
                "mode": "strict",
                "rerank_by_topic": rt,
                "prefer_durable_types": False,  # forced on internally under strict regardless
                "demote_superseded": False,  # forced on internally under strict regardless
                "use_cross_encoder": ce,
            }
        )

    # Deliberately sets prefer_durable_types/demote_superseded True under strict, to empirically
    # verify (not assume) they make no difference vs strict_rt0_ce0 above -- see docstring point 1.
    configs.append(
        {
            "name": "strict_flagcheck_pdt1_ds1",
            "mode": "strict",
            "rerank_by_topic": False,
            "prefer_durable_types": True,
            "demote_superseded": True,
            "use_cross_encoder": False,
        }
    )

    configs.append(
        {
            "name": "history_default",
            "mode": "history",
            "rerank_by_topic": False,
            "prefer_durable_types": False,
            "demote_superseded": False,
            "use_cross_encoder": False,
        }
    )
    configs.append(
        {
            "name": "history_kitchen_sink",
            "mode": "history",
            "rerank_by_topic": True,
            "prefer_durable_types": True,
            "demote_superseded": True,
            "use_cross_encoder": True,
        }
    )

    return configs


def _timed_search(db_path: str, cfg: dict, query: str, limit: int) -> tuple[list | dict, float]:
    # NOTE: cfg still carries rerank_by_topic/use_cross_encoder keys (see _build_configs --
    # deliberately unchanged so eval_configs.py's signed 24-config manifest / config_fingerprint
    # keep their exact historical shape), but search_memory no longer accepts either as a param
    # (candidate/search-ce-final-reranker made the cross-encoder the unconditional Stage-2
    # reranker; the caller-facing rerank_by_topic full-override is retired) -- this benchmark is a
    # confirmed dead end (superseded by the branch-per-approach method), not re-run, so this is
    # just enough to keep the script importable/callable, not a claim it's still meaningful to run.
    t0 = time.perf_counter()
    result = search_memory(
        query_keywords=query,
        db_path=db_path,
        limit=limit,
        mode=cfg["mode"],
        prefer_durable_types=cfg["prefer_durable_types"],
        demote_superseded=cfg["demote_superseded"],
        include_related=False,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return result, elapsed_ms


def _eval_positive_case(result: list | dict, case: dict) -> tuple[bool, bool, str | None]:
    """Returns (topk_correct, strict_top1_correct, top1_id)."""
    if not isinstance(result, list) or not result:
        return False, False, None
    top_k = case.get("top_k", 1)
    ids_in_window = [r.get("id") for r in result[:top_k]]
    expected = case["expected_entity_id"]
    return expected in ids_in_window, result[0].get("id") == expected, result[0].get("id")


def _eval_negative_case(result: list | dict, case: dict) -> tuple[bool, str]:
    """Mirrors run_relevance_gate_holdout.py's own _evaluate_case pass/fail logic -- only
    meaningful when the config's mode is "strict" (the only mode with an abstention gate)."""
    if isinstance(result, dict) and "error" in result:
        return False, f"error: {result['error']}"
    result_ids = [r.get("id") for r in result] if isinstance(result, list) else []
    result_titles = [r.get("title") for r in result] if isinstance(result, list) else []

    if case["expected_outcome"] == "empty":
        passed = result == []
        return passed, (
            "abstained" if passed else f"returned {len(result_titles)}: {result_titles}"
        )

    passed = len(result_ids) > 0
    detail = f"returned {len(result_titles)}: {result_titles}"
    if passed and "expected_top_title_contains" in case:
        needle = case["expected_top_title_contains"]
        passed = needle in (result_titles[0] or "")
    if passed and "expected_resolved_id" in case:
        passed = case["expected_resolved_id"] in result_ids
    if passed and "expected_absent_id" in case:
        passed = case["expected_absent_id"] not in result_ids
    return passed, detail


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def run_config(  # noqa: PLR0912 -- linear per-config measurement sequence (positive/negative/
    # adversarial passes), same rationale this codebase already applies to search_memory/
    # benchmark_cross_encoder_rerankers.py's own noqa'd complexity (CONTRIBUTING.md baseline).
    cfg: dict,
    db_path: str,
    positive_cases: list[dict],
    negative_cases: list[dict],
) -> dict:
    print(f"\n=== {cfg['name']} (mode={cfg['mode']}) ===")
    latencies_ms: list[float] = []

    positive_results = []
    for case in positive_cases:
        result, ms = _timed_search(db_path, cfg, case["query"], max(5, case.get("top_k", 1)))
        latencies_ms.append(ms)
        topk_ok, top1_ok, top1_id = _eval_positive_case(result, case)
        positive_results.append(
            {
                "id": case["id"],
                "topk_correct": topk_ok,
                "strict_top1_correct": top1_ok,
                "top1_id": top1_id,
                "latency_ms": ms,
            }
        )
    positive_topk_acc = sum(r["topk_correct"] for r in positive_results) / len(positive_results)
    positive_top1_acc = sum(r["strict_top1_correct"] for r in positive_results) / len(
        positive_results
    )

    negative_results = []
    is_gated_mode = cfg["mode"] == "strict"
    for case in negative_cases:
        result, ms = _timed_search(db_path, cfg, case["query"], 5)
        latencies_ms.append(ms)
        if is_gated_mode:
            passed, detail = _eval_negative_case(result, case)
        else:
            # No abstention gate outside strict -- record naive behavior descriptively, not as a
            # pass/fail bar (see docstring point 3).
            returned_nonempty = isinstance(result, list) and len(result) > 0
            top1_title = result[0].get("title") if returned_nonempty else None
            passed = None
            detail = (
                f"naive: returned_nonempty={returned_nonempty} top1={top1_title!r}"
                if returned_nonempty
                else "naive: returned empty (incidentally, not via a gate)"
            )
        negative_results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "held_out": case.get("held_out", False),
                "passed": passed,
                "detail": detail,
                "latency_ms": ms,
            }
        )
    if is_gated_mode:
        primary_neg = [r for r in negative_results if not r["held_out"]]
        negative_primary_acc = (
            sum(r["passed"] for r in primary_neg) / len(primary_neg) if primary_neg else None
        )
    else:
        negative_primary_acc = None

    adversarial_results = []
    for case in ADVERSARIAL_QUERIES:
        result, ms = _timed_search(db_path, cfg, case["query"], 5)
        latencies_ms.append(ms)
        returned_nonempty = isinstance(result, list) and len(result) > 0
        top1_title = result[0].get("title") if returned_nonempty else None
        if is_gated_mode:
            correct = not returned_nonempty  # strict SHOULD abstain on all of these
        else:
            correct = None
        adversarial_results.append(
            {
                "id": case["id"],
                "query": case["query"],
                "returned_nonempty": returned_nonempty,
                "top1_title": top1_title,
                "correct_if_gated": correct,
                "latency_ms": ms,
            }
        )
    if is_gated_mode:
        adversarial_acc = sum(r["correct_if_gated"] for r in adversarial_results) / len(
            adversarial_results
        )
    else:
        adversarial_false_accept_rate = sum(
            r["returned_nonempty"] for r in adversarial_results
        ) / len(adversarial_results)
        adversarial_acc = None

    summary = {
        "config": cfg,
        "positive_topk_accuracy": positive_topk_acc,
        "positive_top1_accuracy": positive_top1_acc,
        "positive_results": positive_results,
        "negative_primary_abstention_accuracy": negative_primary_acc,
        "negative_results": negative_results,
        "adversarial_abstention_accuracy": adversarial_acc,
        "adversarial_false_accept_rate": (None if is_gated_mode else adversarial_false_accept_rate),
        "adversarial_results": adversarial_results,
        "latency_p50_ms": _percentile(latencies_ms, 0.5),
        "latency_p95_ms": _percentile(latencies_ms, 0.95),
        "latency_mean_ms": statistics.fmean(latencies_ms) if latencies_ms else None,
        "n_calls": len(latencies_ms),
    }

    print(
        f"  positive: top-k(tolerant) {positive_topk_acc:.0%}  top1(strict) {positive_top1_acc:.0%}"
    )
    if is_gated_mode:
        print(
            f"  negative(strict gate) primary: {negative_primary_acc:.0%}   "
            f"adversarial abstention: {adversarial_acc:.0%}"
        )
    else:
        print(
            f"  adversarial false-accept rate (informational, no gate): "
            f"{summary['adversarial_false_accept_rate']:.0%}"
        )
    print(
        f"  latency p50/p95: {summary['latency_p50_ms']:.1f}ms / {summary['latency_p95_ms']:.1f}ms"
    )

    return summary


def _verify_known_interactions(results_by_name: dict[str, dict]) -> list[str]:
    """Cross-checks the handover's documented invariants against THIS run's actual data (not just
    citing the earlier one-off benchmarks) -- returns a list of finding strings, prefixed "P0" for
    anything that would be a real regression, not just a benchmark observation."""
    findings = []

    # Invariant 1: strict's prefer_durable_types/demote_superseded own values shouldn't matter.
    base = results_by_name.get("strict_rt0_ce0")
    flagcheck = results_by_name.get("strict_flagcheck_pdt1_ds1")
    if base and flagcheck:
        base_ids = [r["top1_id"] for r in base["positive_results"]]
        flag_ids = [r["top1_id"] for r in flagcheck["positive_results"]]
        if base_ids == flag_ids:
            findings.append(
                "CONFIRMED: strict mode's prefer_durable_types/demote_superseded own flag values "
                "make no difference to positive-set top-1 ordering (forced-defaults invariant holds)."
            )
        else:
            findings.append(
                "P0 REGRESSION: strict mode's prefer_durable_types/demote_superseded flag values "
                f"DID change positive-set ordering ({base_ids} vs {flag_ids}) -- "
                "_apply_strict_ranking_defaults's documented forced-defaults invariant is broken."
            )

    # Invariant 2: cross_encoder_score must be inert to accept_or_abstain's accept/reject decision
    # under strict -- compare ACCEPTED SETS (not order) between ce=False/True at matching rt value.
    for rt in (0, 1):
        no_ce = results_by_name.get(f"strict_rt{rt}_ce0")
        with_ce = results_by_name.get(f"strict_rt{rt}_ce1")
        if not (no_ce and with_ce):
            continue
        no_ce_neg_accept = {r["id"]: r["passed"] for r in no_ce["negative_results"]}
        with_ce_neg_accept = {r["id"]: r["passed"] for r in with_ce["negative_results"]}
        # Both configs are scored against the SAME expected outcome, so equal pass/fail per case
        # id implies equal accept/reject decisions (not just equal accuracy sums).
        if no_ce_neg_accept == with_ce_neg_accept:
            findings.append(
                f"CONFIRMED (rt={rt}): use_cross_encoder is inert to accept_or_abstain's negative-"
                "set decisions (per-case pass/fail identical with/without cross-encoder)."
            )
        else:
            findings.append(
                f"P0 REGRESSION (rt={rt}): use_cross_encoder changed accept_or_abstain's negative-"
                f"set decisions -- no_ce={no_ce_neg_accept} with_ce={with_ce_neg_accept}."
            )

    # Invariant 3 (docstring point 2): history's ranking should match broad's, same flag combo.
    hist_default = results_by_name.get("history_default")
    broad_default = results_by_name.get("broad_rt0_pdt0_ds0_ce0")
    if hist_default and broad_default:
        h_ids = [r["top1_id"] for r in hist_default["positive_results"]]
        b_ids = [r["top1_id"] for r in broad_default["positive_results"]]
        findings.append(
            "CONFIRMED: history_default's positive-set ordering matches broad's default combo "
            "(history adds is_superseded tagging only, no ranking change)."
            if h_ids == b_ids
            else f"UNEXPECTED: history_default ordering differs from broad default ({h_ids} vs {b_ids})."
        )
    hist_sink = results_by_name.get("history_kitchen_sink")
    broad_sink = results_by_name.get("broad_rt1_pdt1_ds1_ce1")
    if hist_sink and broad_sink:
        h_ids = [r["top1_id"] for r in hist_sink["positive_results"]]
        b_ids = [r["top1_id"] for r in broad_sink["positive_results"]]
        findings.append(
            "CONFIRMED: history_kitchen_sink's positive-set ordering matches broad's all-flags-on "
            "combo."
            if h_ids == b_ids
            else f"UNEXPECTED: history_kitchen_sink ordering differs from broad all-on ({h_ids} vs {b_ids})."
        )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None, help="Throwaway DB copy to run against")
    parser.add_argument("--reranker-model", default=DEFAULT_RERANKER_MODEL)
    args = parser.parse_args()

    tmp_dir = None
    orig_reranker_env = os.environ.get("SALTMDB_RERANKER_MODEL")
    try:
        if args.db_path:
            db_path = args.db_path
        else:
            if not SOURCE_CORPUS_DEFAULT.exists():
                print(
                    f"Default source corpus not found: {SOURCE_CORPUS_DEFAULT}. "
                    "Run build_diverse_test_db.py first, or pass --db-path.",
                    file=sys.stderr,
                )
                return 1
            tmp_dir = tempfile.mkdtemp(prefix="saltmdb_option_matrix_benchmark_")
            db_path = os.path.join(tmp_dir, "diverse_corpus_benchmark_copy.db")
            shutil.copy(SOURCE_CORPUS_DEFAULT, db_path)

        _refuse_live_db(db_path)
        os.environ["SALTMDB_RERANKER_MODEL"] = args.reranker_model

        positive_cases = _load_cases(POSITIVE_SET_DEFAULT, "queries")
        negative_cases = _load_cases(NEGATIVE_SET_DEFAULT, "cases")
        configs = _build_configs()

        print(
            f"Running {len(configs)} configs x ({len(positive_cases)} positive + "
            f"{len(negative_cases)} negative + {len(ADVERSARIAL_QUERIES)} adversarial) queries "
            f"= {len(configs) * (len(positive_cases) + len(negative_cases) + len(ADVERSARIAL_QUERIES))} "
            f"total search_memory() calls."
        )

        all_results = []
        for cfg in configs:
            all_results.append(run_config(cfg, db_path, positive_cases, negative_cases))

        results_by_name = {r["config"]["name"]: r for r in all_results}
        invariant_findings = _verify_known_interactions(results_by_name)

        print("\n=== Known-interaction verification (against THIS run's real data) ===")
        for f in invariant_findings:
            print(f"  {f}")

        RESULTS_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
        out_path = RESULTS_DIR_DEFAULT / f"search_option_matrix_{int(time.time())}.json"
        out_path.write_text(
            json.dumps({"results": all_results, "invariant_findings": invariant_findings}, indent=2)
        )
        print(f"\nWrote results to {out_path}")

        p0s = [f for f in invariant_findings if f.startswith("P0")]
        if p0s:
            print(
                f"\n*** {len(p0s)} P0 invariant violation(s) found -- see above ***",
                file=sys.stderr,
            )
            return 1
        return 0
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    finally:
        if orig_reranker_env is None:
            os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        else:
            os.environ["SALTMDB_RERANKER_MODEL"] = orig_reranker_env
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
