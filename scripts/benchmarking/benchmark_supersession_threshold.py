"""Threshold-calibration benchmark for the memory-core rework Phase 4 Stage-1 filter
(`librarian_service.scout_consolidated_supersessions`'s consolidated-vs-raw similarity gate --
see plans/eager-beaming-hippo.md D2 and SALTMDB memory `32f9ac84`).

Locked `SUPERSESSION_MIN_SIMILARITY_THRESHOLD = 0.7557` in config.py from this script's real
measurement (0% false-accept, 0% false-reject; results and Stage-2 separation proof recorded as
SALTMDB memory `2765b632`) -- mirrors how COHESION_MIN_PAIRWISE_THRESHOLD /
CLUSTER_MIN_PAIRWISE_THRESHOLD were locked from real measurement in Phase 3
(benchmark_cohesion_threshold.py, SALTMDB memory `d2d7cf85`), not taken on faith. Re-run this
script and re-lock the constant if the corpus is ever meaningfully revised -- do not hand-edit
the threshold.

This comparison shape is structurally different from anything already benchmarked: one
*post-merge synthesis* centroid (the dense, abstracted paragraph shape commit_consolidation
actually produces) against one *raw fragment* centroid -- not raw-vs-raw (Phase 3's Population A
or B). Reusing an existing constant here would apply a threshold calibrated for a different
geometry.

Corpus (synthetic -- no confirmed real historical *supersession-detection* bad-call incident
exists in SALTMDB memory; searched, only the general clustering-chaining `3deae748` and omnibus
`6a8fec3d` incidents exist, already grounding Phase 3's populations):
  - Positive class ("should trigger Stage 1"): a synthesized consolidated-summary paragraph
    (the shape commit_consolidation produces) paired with a *new* raw fragment that concretely
    updates/extends/regresses the SAME specific fact the summary describes.
  - Negative class ("should NOT trigger Stage 1"): the same consolidated-summary paired with a
    raw fragment from the same broad engineering domain but about a DIFFERENT specific decision
    or fact -- mirroring the "same specific topic" vs. "broad theme" distinction Phase 2's
    RERANK_SAME_TOPIC_THRESHOLD / RERANK_BROAD_THEME_THRESHOLD (`4b178f4b`) measured for a
    structurally similar problem (cited conceptually, not aliased as a value -- different
    aggregation shape: single-vector-vs-single-vector here, not chunk reranking).

Risk asymmetry (see plan "Risk notes"): an explicit 0%-false-accept target against the known
different-specific-fact negative set -- the threshold is chosen so EVERY negative-class pair
fails the gate, even at the cost of a nonzero false-reject rate on the positive class (reported
below, not hidden).

Critical validation this script must also perform (Codex review round 1, finding 3): if the
locked threshold T is high enough that two unit vectors each individually >= T similar to the
same consolidated centroid are *geometrically guaranteed* to be >= CLUSTER_MIN_PAIRWISE_THRESHOLD
similar to each other (the bound is 2*T^2 - 1, from the spherical law of cosines, worst case both
candidate vectors on opposite sides of the centroid in the same great circle), then Stage 2's
mutual-cohesion gate can never reject anything and the anti-chaining fixture becomes
unconstructible. This script computes that bound against the locked T and fails loudly if there
is no real separation, rather than letting a silently-vacuous Stage 2 ship.

Numpy-only cosine math over real chunk embeddings (embed_texts, chunk_text) -- same dependency
posture as benchmark_cohesion_threshold.py. Reuses cohesion_service's own centroid helper
directly (not a reimplementation) so this benchmark measures the exact production algorithm.

Moved out of tests/ (and off the test_* naming convention) so `python -m unittest discover -s
tests` never collects it -- a one-time calibration pass, not a regression test.
"""

import sys

import numpy as np

from saltmdb.config import CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS, CLUSTER_MIN_PAIRWISE_THRESHOLD
from saltmdb.utils.chunking import chunk_text
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.cohesion_service import _centroid

# ---------------------------------------------------------------------------
# Consolidated-summary paragraphs -- the dense, abstracted shape
# commit_consolidation actually produces, one per topic.
# ---------------------------------------------------------------------------

CONSOLIDATED_SUMMARIES = {
    "wal_checkpoint_fix": (
        "The Librarian subprocess's WAL checkpoint was failing silently because "
        "PRAGMA wal_checkpoint(TRUNCATE) lived inside the leader lock's early-release path and "
        "never actually executed. Fixed by moving the checkpoint call into "
        "_run_librarian_maintenance, gated on holding the leader lock; wal_pages now drops to "
        "zero after every run."
    ),
    "chunk_freshness_guard": (
        "entity_chunk_embeddings rows carry the entities.content_hash value that produced them, "
        "so a stale write left behind by a failed async refresh can be detected. The staleness "
        "guard join requires c.content_hash IS e.content_hash and e.status != 'archived'; "
        "backfill_chunk_embeddings re-embeds any entity with missing or stale chunk rows "
        "synchronously as a startup repair sweep."
    ),
    "cadet_oauth_token_bug": (
        "A CADET delegate_task job to the agy provider hung with zero output because the "
        "containerized agy process started before the host's OAuth token refresh had written the "
        "credential file into the shared volume. Fixed by making the container entrypoint wait "
        "on a readiness file the host writes only after the OAuth token is confirmed present on "
        "disk."
    ),
    "viewer_pidfile_leak": (
        "The web viewer's start_viewer() hardcoded its PID tracking file path, so parallel test "
        "runs against different temp databases collided on the same real ~/.saltmdb pid file and "
        "corrupted the live viewer_8080.pid state for an already-running production viewer. "
        "Fixed by deriving the pid-file path from the same db_path each viewer instance was "
        "started against."
    ),
    "quality_gate_thresholds": (
        "evaluate_memory_quality runs a two-tier check: Tier 1 hard-rejects on structural "
        "signals (QG_MIN_LENGTH=20, symbol ratio, n-gram duplication), Tier 2 applies softer "
        "Coleman-Liau readability bounds (QG_CLI_MIN=2.0, QG_CLI_MAX=26.0) only once Tier 1 "
        "already passed, keeping throwaway one-line content out of consolidation."
    ),
    "predicate_canonicalization": (
        "resolve_or_create_predicate normalizes a raw predicate string (lowercase, non-alnum "
        "runs collapsed to underscore) before checking it against the predicates table. Unlike "
        "resolve_or_create_tag it has no plural/suffix fallback heuristic, since the seed "
        "predicate vocabulary is short and already snake_case; an unrecognized predicate is "
        "always auto-created rather than rejected."
    ),
}

# Positive class: a NEW raw fragment concretely updating/extending/regressing the SAME specific
# fact the consolidated summary describes -- exactly what should re-open that node for review.
POSITIVE_RAW_FRAGMENTS = {
    "wal_checkpoint_fix": (
        "Regression: the wal_checkpoint(TRUNCATE) fix inside _run_librarian_maintenance stopped "
        "firing again after the leader-lock refactor moved the release point earlier; wal_pages "
        "is climbing on long-running hosts again."
    ),
    "chunk_freshness_guard": (
        "Found an edge case in the content_hash staleness guard: a row can pass c.content_hash "
        "IS e.content_hash even when e.status = 'archived' if the archive write and the chunk "
        "write race, letting one archived entity's stale chunks survive one extra Librarian pass."
    ),
    "cadet_oauth_token_bug": (
        "The readiness-file wait added for the CADET agy OAuth outage has its own timeout bug: "
        "it polls forever with no upper bound, so a genuinely broken token refresh now hangs the "
        "container indefinitely instead of failing fast like the old code did."
    ),
    "viewer_pidfile_leak": (
        "The db_path-derived pid-file fix for the viewer still collides when two different "
        "SALTMDB_DB_PATH values hash to the same truncated filename component on Windows, "
        "reproducing the original parallel-test collision on that platform only."
    ),
    "quality_gate_thresholds": (
        "QG_MIN_LENGTH=20 is rejecting legitimate short consolidation outputs for terse "
        "one-fact decisions; proposing QG_MIN_LENGTH be conditional on Tier 1's n-gram check "
        "passing cleanly rather than a flat floor."
    ),
    "predicate_canonicalization": (
        "resolve_or_create_predicate's lowercase-and-collapse normalization silently merges two "
        "semantically distinct predicates ('supersedes' and 'super-sedes' typo) into one row, "
        "since the seed vocabulary assumption of 'always already snake_case' doesn't hold for "
        "typo'd input."
    ),
}

# Negative class: same broad engineering domain, but about a DIFFERENT specific decision or
# fact than the consolidated summary -- should NOT re-open that node.
NEGATIVE_RAW_FRAGMENTS = {
    "wal_checkpoint_fix": (
        "trigger_librarian's cooldown check collapses concurrent callers to a single winner via "
        "one atomic UPDATE on _system_locks.last_run_at, replacing an older acquire/release lock "
        "dance that spent two separate write transactions on the same check."
    ),
    "chunk_freshness_guard": (
        "CHUNK_SIZE_CHARS=1200 and CHUNK_OVERLAP_CHARS=200 were settled as the sliding-window "
        "chunking parameters across three benchmark rounds during Phase 1 of the memory-core "
        "rework."
    ),
    "cadet_oauth_token_bug": (
        "CADET's delegate_task has no per-job git worktree/branch isolation -- every job runs "
        "directly against the literal cwd passed in, the same shared working tree as every other "
        "concurrent job."
    ),
    "viewer_pidfile_leak": (
        "Track 1 Phase 1 of the viewer redesign killed glassmorphism and gradients in favor of "
        "an opaque surface ladder, changing the visual theme system but not any file-handling "
        "code."
    ),
    "quality_gate_thresholds": (
        "predicate_canonicalization normalizes a raw predicate string (lowercase, non-alnum "
        "runs collapsed to underscore) before checking it against the predicates table, with no "
        "plural/suffix fallback heuristic."
    ),
    "predicate_canonicalization": (
        "merge_tags_heuristics groups tags by a normalized form (lowercased, dashes/underscores "
        "and '#' stripped) and merges every non-canonical duplicate into the first row per group, "
        "picking the canonical survivor arbitrarily by SQL row order."
    ),
}


def _text_centroid(text: str) -> list:
    """Chunks + embeds one text the exact way cohesion_service.get_fresh_entity_centroids'
    fallback path does, then centroids it via the same _centroid helper -- so this benchmark
    measures the real production algorithm end to end, not an approximation of it."""
    chunks = chunk_text(text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
    if not chunks:
        chunks = [{"text": text}]
    vectors = embedding_service.embed_texts([c["text"] for c in chunks])
    return _centroid([np.array(v, dtype=np.float32) for v in vectors])


def _cosine(a: list, b: list) -> float:
    va = np.array(a, dtype=np.float64)
    vb = np.array(b, dtype=np.float64)
    va = va / max(np.linalg.norm(va), 1e-10)
    vb = vb / max(np.linalg.norm(vb), 1e-10)
    return float(va @ vb)


def _bucket_stats(name: str, scores: dict) -> None:
    arr = np.array(list(scores.values()))
    print(
        f"  {name:15s} n={len(arr):2d}  min={arr.min():.4f}  mean={arr.mean():.4f}  max={arr.max():.4f}"
    )
    for label, score in sorted(scores.items(), key=lambda kv: kv[1]):
        print(f"      {score:.4f}  {label}")


def _derive_threshold(positive_scores: dict, negative_scores: dict, margin: float = 0.02) -> dict:
    """0%-false-accept target: threshold set just above the worst (highest) negative-class
    score, so every negative-class pair fails the gate even at the cost of a nonzero
    false-reject rate on the positive class (reported, not hidden)."""
    worst_negative = max(negative_scores.values())
    threshold = round(worst_negative + margin, 4)
    false_rejects = {label: s for label, s in positive_scores.items() if s < threshold}
    false_accepts = {label: s for label, s in negative_scores.items() if s >= threshold}
    return {
        "threshold": threshold,
        "false_reject_count": len(false_rejects),
        "false_reject_labels": false_rejects,
        "false_accept_count": len(false_accepts),
        "false_accept_labels": false_accepts,
    }


def _check_stage2_separation(threshold: float, cohesion_floor: float) -> dict:
    """Codex round-1 finding 3: if two unit vectors are each >= threshold similar to the same
    consolidated centroid, the spherical law of cosines guarantees their pairwise similarity is
    >= 2*threshold^2 - 1 (worst case: both on the far side of the centroid from each other, same
    great circle). If that guaranteed floor already clears cohesion_floor
    (CLUSTER_MIN_PAIRWISE_THRESHOLD), Stage 2 can never reject a Stage-1 survivor pair and the
    anti-chaining fixture (individually-similar-but-mutually-unrelated raw candidates) is
    mathematically unconstructible at this threshold."""
    guaranteed_min_pairwise = 2 * threshold**2 - 1
    gap = cohesion_floor - guaranteed_min_pairwise
    return {
        "guaranteed_min_pairwise": round(guaranteed_min_pairwise, 4),
        "cohesion_floor": cohesion_floor,
        "gap": round(gap, 4),
        "stage2_can_reject": gap > 0,
    }


def run_threshold_calibration() -> dict:
    embedding_service.get_model()

    print("\n=== Stage 1: consolidated-summary-vs-raw-fragment similarity ===")
    summary_centroids = {
        name: _text_centroid(text) for name, text in CONSOLIDATED_SUMMARIES.items()
    }

    positive_scores = {
        name: _cosine(summary_centroids[name], _text_centroid(frag))
        for name, frag in POSITIVE_RAW_FRAGMENTS.items()
    }
    negative_scores = {
        name: _cosine(summary_centroids[name], _text_centroid(frag))
        for name, frag in NEGATIVE_RAW_FRAGMENTS.items()
    }
    _bucket_stats("positive", positive_scores)
    _bucket_stats("negative", negative_scores)

    result = _derive_threshold(positive_scores, negative_scores)
    threshold = result["threshold"]
    print(f"\n  SUPERSESSION_MIN_SIMILARITY_THRESHOLD = {threshold:.4f}")
    print(
        f"  false_accept_count = {result['false_accept_count']} (target: 0), "
        f"false_reject_count = {result['false_reject_count']} / {len(positive_scores)}"
    )
    if result["false_reject_labels"]:
        print(f"  false rejects: {result['false_reject_labels']}")
    if result["false_accept_labels"]:
        print(f"  false accepts: {result['false_accept_labels']}")

    print("\n=== Stage 2 separation check (Codex round-1 finding 3) ===")
    sep = _check_stage2_separation(threshold, CLUSTER_MIN_PAIRWISE_THRESHOLD)
    print(
        f"  guaranteed_min_pairwise (2*T^2-1) = {sep['guaranteed_min_pairwise']:.4f}  "
        f"vs  CLUSTER_MIN_PAIRWISE_THRESHOLD = {sep['cohesion_floor']:.4f}  "
        f"gap = {sep['gap']:.4f}"
    )
    if sep["stage2_can_reject"]:
        print("  OK: Stage 2 retains a real ability to reject Stage-1 survivors.")
    else:
        print(
            "  FAIL: at this threshold, every pair that clears Stage 1 is geometrically "
            "guaranteed to clear Stage 2 as well -- the anti-chaining fixture is "
            "unconstructible. Threshold or corpus must be revised before this plan is final."
        )
        # Codex round-2 correction: this guard previously only printed FAIL and exited 0,
        # contradicting this script's own docstring claim of failing loudly. A future re-lock
        # of either threshold that closes the Stage-2 separation gap must not ship silently.
        raise RuntimeError(
            f"Stage-2 separation check failed: guaranteed_min_pairwise="
            f"{sep['guaranteed_min_pairwise']:.4f} >= cohesion_floor={sep['cohesion_floor']:.4f} "
            f"(gap={sep['gap']:.4f}). Threshold or corpus must be revised."
        )

    return {
        "positive_scores": positive_scores,
        "negative_scores": negative_scores,
        **result,
        "stage2_separation": sep,
    }


if __name__ == "__main__":
    try:
        run_threshold_calibration()
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)
