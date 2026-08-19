# Gate D Disposition — Closed, Not Rescheduled

**Status: evaluated via 600-query replay, no contender promotable, baseline kept.**
This document formally closes "Gate D" (the synthetic/historical retrieval-architecture
bakeoff — chunk-embedding variants, pool collapsing, cross-encoder gated/forced reranking)
as a finished experiment with a real, signed negative result, so it stops being carried
forward as "still gated, needs a blind stage."

## What Gate D was

An architecture bakeoff comparing several candidate changes to SALTMDB's retrieval ranking
(chunk-embedding pooling variants, pool collapsing, and an optional ONNX cross-encoder
reranker, gated or forced) against the deployed baseline, to decide whether any contender
was worth promoting to default-on.

## Two things that got conflated, and should not be

1. **The Phase-0 development-judging orchestration genuinely failed.** Postmortem `bae078de`
   documents unverified background runners, malformed judge output, undisclosed model
   substitution, and no quota ceiling defined upfront. This was a real process failure.
2. **The actual promotion evidence — the 600-query real-data replay — ran cleanly and
   produced a valid, signed negative result.** This is the part that matters for the
   disposition decision below, and it is a completed experiment, not a stalled one.

## The promotion evidence (memory `c56be162`)

Frozen replay, 2026-08-12, 600 historical queries across eight interleaved configurations on
a disposable snapshot, zero search errors, exact reconstruction (600/600 rows, byte-matched
prior manifest). Runtime commit `b703d1da`, snapshot/query/config/result fingerprints all
recorded for reproducibility.

Every tested contender either regressed safety-critical retrieval beyond the allowed margin
or showed no admissible gain:

- **Chunk-embedding variants (mid/high pooling)**: sentence top-1 regressed 27–28 points
  (145→117-118), keyword regressed 14-15 points (145→99), with no semantic top-1 gain
  (66 vs. 66 control). Not close to viable.
- **Pool collapsing**: smaller keyword regression, no semantic gain.
- **Cross-encoder gated/forced reranking**: did improve human-judged semantic rank-1 quality
  (`SAME_SPECIFIC_FACT` 99→114 gated, 99→112 forced) — but at a cost of 9.3–15.3 percentage
  points of sentence top-1 regression and 2.0–4.7 points of keyword regression, both well
  outside the allowed one-point safety-loss budget. The semantic gain also does not survive
  Holm correction across the seven predeclared comparisons (`p≈0.14`).

No runtime defaults were changed as a result of this replay — this was already the de facto
state; this document just makes the disposition explicit and citable instead of leaving the
roadmap implying a follow-up "blind stage" is still owed.

## Decision

**Stop further Gate-D-style architecture bakeoff work.** Grinding more quota into more
ranking-architecture variants has a confirmed, not merely felt, diminishing-returns result —
this shape of experiment already answered "no" once, cleanly. Appropriate for a beta launch:
close the open-ended science project instead of shipping with it dangling.

## What stays open, separately (already done, not part of this closure)

The **live-retrieval-audit** (`6ee96334`, a smaller, separate investigation from the Gate D
bakeoff) diagnosed a specific false-accept root cause (H1: FTS AND→OR silent fallback
bypassing `mode="strict"` abstention) and a fix was designed (`ed7cc8d5`) and **already
shipped** in commit `675f5f5` (2026-08-09), with test coverage in
`tests/test_relevance_gate.py`. This was mistakenly still listed as outstanding in an earlier
draft of the agent-experience-layer plan; see memory `4c54e2d3` for the correction. It is not
part of the Gate D bakeoff and required no further scheduling.

## Known limitation carried into beta (not a blocker)

The 46% semantic-paraphrase top-1 rate from the original retrieval audit
(`summary.md` in this directory) is a documented, known limitation going into beta — not
something Gate D was expected to fix (Gate D's own contenders didn't move this number enough
to justify their regressions either). The agent-experience-layer plan's telemetry layer
(retrieval-outcome logging via `log_event`) is the mechanism intended to eventually show
whether this matters in real usage, rather than another synthetic benchmark pass.
