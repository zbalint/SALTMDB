"""Threshold-calibration benchmark for the memory-core rework Phase 3 pairwise cohesion gate
(`cohesion_service.min_pairwise_cohesion`, used by both `relation_service.commit_consolidation`
and `librarian_service.consolidate_vector_clusters` -- see plans/ and SALTMDB memory `5c09effa`).

`COHESION_MIN_PAIRWISE_THRESHOLD` / `CLUSTER_MIN_PAIRWISE_THRESHOLD` in config.py ship as a 0.60
placeholder pending this benchmark -- mirrors how RERANK_SAME_TOPIC_THRESHOLD/
RERANK_BROAD_THEME_THRESHOLD were locked from real measurement in Phase 2
(benchmark_rerank_thresholds.py, SALTMDB memory `4b178f4b`), not taken on faith.

Two populations, benchmarked separately, since they differ in typical group size:
  - Population A: commit-gate parent sets (2-4 items) -> COHESION_MIN_PAIRWISE_THRESHOLD.
  - Population B: Librarian candidate-pool clusters (6-10 items) -> CLUSTER_MIN_PAIRWISE_THRESHOLD.

Corpus grounded in two real, documented incidents (not hypothetical):
  - Positive class ("genuinely related, should pass"): hand-crafted fragment groups, each
    multiple notes about the SAME specific bug/decision -- the shape of legitimate
    consolidation candidates.
  - Negative class ("should be rejected"): reconstructs the two confirmed bad-merge shapes --
    `6a8fec3d`'s 37-way omnibus (topically-unrelated engineering notes force-merged into one
    node: CADET workflow decisions, a SQLite connection wrapper design note, an is_core bugfix
    handover, a clustering-plan review, an Engineering Rule governance note, a hooks feature
    request, a live-test fixture, a memory-graph governance incident, a CI lint fix) and
    `3deae748`'s confirmed single-linkage chaining incident (a genuinely-related fragment group
    with one or two unrelated standalone memories dragged in via shared vocabulary).

Risk asymmetry (see plans/ "Risk notes"): an explicit 0% false-accept target against the known
bad-bundle set -- the threshold is chosen so EVERY negative-class score fails the gate, even at
the cost of a nonzero false-reject rate on the positive class (reported below, not hidden).

Numpy-only cosine math over real chunk embeddings (embed_texts, chunk_text) -- same dependency
posture as benchmark_rerank_thresholds.py. Reuses cohesion_service's own centroid/min-pairwise
functions directly (not a reimplementation) so this benchmark measures the exact production
algorithm, not an approximation of it.

Moved out of tests/ (and off the test_* naming convention) so `python -m unittest discover -s
tests` never collects it -- a one-time calibration pass, not a regression test.
"""

import numpy as np

from saltmdb.config import CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
from saltmdb.utils.chunking import chunk_text
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.cohesion_service import _centroid, min_pairwise_cohesion

# ---------------------------------------------------------------------------
# Population A: commit-gate parent sets (2-4 fragments each)
# ---------------------------------------------------------------------------

POSITIVE_GROUPS_A = {
    "wal_checkpoint_fix": [
        "The Librarian subprocess's WAL checkpoint was failing silently because "
        "PRAGMA wal_checkpoint(TRUNCATE) was never called after the leader lock was released.",
        "Root cause of the WAL checkpoint failure: the checkpoint call lived inside the leader "
        "lock's early-release path, which returned before the checkpoint pragma ever executed.",
        "Fix verified: moved the wal_checkpoint(TRUNCATE) call into _run_librarian_maintenance, "
        "gated on holding the leader lock, and confirmed wal_pages drops to zero after a run.",
    ],
    "cadet_oauth_token_bug": [
        "A CADET delegate_task job to the agy provider hung with zero output for over 20 "
        "minutes; exec'ing into the container showed the OAuth token was never set.",
        "Traced the CADET agy OAuth outage to the containerized agy process starting before the "
        "host's token refresh had actually written the credential file into the shared volume.",
        "Fix for the CADET OAuth outage: the container's entrypoint now waits on a readiness "
        "file the host writes only after the OAuth token is confirmed present on disk.",
    ],
    "chunk_freshness_guard": [
        "entity_chunk_embeddings rows now carry the entities.content_hash value that produced "
        "them, so a stale write left behind by a failed async refresh can be detected.",
        "The staleness guard join requires c.content_hash IS e.content_hash and e.status != "
        "'archived', excluding any chunk row that no longer matches the entity's live content.",
        "backfill_chunk_embeddings scans for entities with missing or stale chunk rows and "
        "re-embeds them synchronously as a startup repair sweep.",
    ],
    "viewer_pidfile_leak": [
        "The web viewer's start_viewer() hardcodes its PID tracking file path, so parallel test "
        "runs against different temp databases all collide on the same real ~/.saltmdb pid file.",
        "This viewer pid-file collision corrupts the live viewer_8080.pid state for a real, "
        "already-running production viewer process whenever a test suite runs concurrently.",
        "Proposed fix: derive the pid-file path from the same db_path each viewer instance was "
        "started against, instead of a single hardcoded location shared by every process.",
    ],
    "quality_gate_thresholds": [
        "evaluate_memory_quality's Coleman-Liau bounds (QG_CLI_MIN=2.0, QG_CLI_MAX=26.0) reject "
        "content whose readability index falls outside a normal prose range.",
        "QG_MIN_LENGTH=20 exists specifically to reject throwaway one-line consolidation "
        "content that wouldn't survive as a standalone memory on its own.",
        "The quality gate's 3-gram/5-gram duplication ratio thresholds catch content that "
        "repeats the same short phrase over and over instead of synthesizing real information.",
    ],
    "predicate_canonicalization": [
        "resolve_or_create_predicate normalizes a raw predicate string (lowercase, non-alnum "
        "runs collapsed to underscore) before checking it against the predicates table.",
        "Unlike resolve_or_create_tag, predicate canonicalization has no plural/suffix fallback "
        "heuristic, since the seed predicate vocabulary is short and already snake_case.",
        "An unrecognized predicate is always auto-created and returned rather than rejected, "
        "keeping store_relation non-blocking for novel relationship types.",
    ],
    "librarian_cooldown_throttle": [
        "trigger_librarian's cooldown check collapses concurrent callers to a single winner via "
        "one atomic UPDATE on _system_locks.last_run_at guarded by its own WHERE clause.",
        "The cooldown throttle replaced an older acquire_librarian_lock plus immediate "
        "release_librarian_lock dance that spent two separate write transactions on the same check.",
        "LIBRARIAN_TRIGGER_COOLDOWN_S controls how often the Librarian subprocess can be "
        "re-spawned from the same process, independent of the subprocess's own leader-election lock.",
    ],
    "tag_merge_heuristic": [
        "merge_tags_heuristics groups tags by a normalized form (lowercased, dashes/underscores "
        "and '#' stripped) and merges every non-canonical duplicate into the first row per group.",
        "Unlike the explicit merge_tags tool, merge_tags_heuristics picks the canonical survivor "
        "arbitrarily by SQL row order rather than letting the caller choose which name wins.",
        "After a heuristic merge, entity_tags rows pointing at the old tag id are repointed to "
        "the canonical tag id, with a defensive DELETE guarding against a resulting duplicate pair.",
    ],
}

# Each negative-class entry is itself a *group* of parent_ids' worth of content that a
# commit_consolidation call could plausibly (and wrongly) be asked to merge -- either a direct
# unrelated pair (the 37-way omnibus shape) or a genuinely-related trio plus one unrelated
# tag-along (the confirmed `3deae748` chaining shape).
NEGATIVE_GROUPS_A = {
    "omnibus_cadet_vs_sqlite_wrapper": [
        "Decision: CADET's delegate_task always routes through the agy/cursor/codex/copilot "
        "provider abstraction rather than shelling out to each CLI directly from the caller.",
        "The SQLite connection.py wrapper centralizes PRAGMA busy_timeout and WAL mode setup so "
        "every caller gets consistent retry/backoff behavior without repeating the setup.",
    ],
    "omnibus_iscore_bugfix_vs_clustering_review": [
        "Bugfix: commit_consolidation's entity INSERT hardcoded is_core=0, silently dropping "
        "core status whenever a core memory was consolidated alongside a non-core one.",
        "Eighth review round on the clustering plan: Codex flagged that HDBSCAN flags nearly "
        "everything as noise on small single-operator corpora, so Connected Components was kept.",
    ],
    "omnibus_governance_rule_vs_hooks_request": [
        "Engineering rule: never run sqlite3 directly against saltmdb.db -- all access must go "
        "through the MCP server tools, since direct access skips secrets-redaction middleware.",
        "Feature request: a UserPromptSubmit hook that auto-searches memory before every user "
        "turn, so relevant context is injected without an explicit search_memory call.",
    ],
    "omnibus_livetest_fixture_vs_memory_governance": [
        "Live-test fixture note: the smoke test spins up a throwaway SALTMDB_DB_PATH, inserts "
        "three raw entities, and asserts the FTS5 index reflects them within one write.",
        "Governance incident: an agent's own 'I fixed my mess' memory node had no real content "
        "and only existed to wire up plausible-sounding cross-links to unrelated pre-existing nodes.",
    ],
    "chaining_wal_checkpoint_plus_ci_lint_fix": [
        # A genuinely-related trio (same as positive_groups_a's wal_checkpoint_fix)...
        "The Librarian subprocess's WAL checkpoint was failing silently because "
        "PRAGMA wal_checkpoint(TRUNCATE) was never called after the leader lock was released.",
        "Root cause of the WAL checkpoint failure: the checkpoint call lived inside the leader "
        "lock's early-release path, which returned before the checkpoint pragma ever executed.",
        "Fix verified: moved the wal_checkpoint(TRUNCATE) call into _run_librarian_maintenance, "
        "gated on holding the leader lock, and confirmed wal_pages drops to zero after a run.",
        # ...plus one unrelated tag-along, mirroring `3deae748`'s confirmed chaining shape.
        "CI lint fix: ruff flagged an unused import in a viewer route handler left over from a "
        "prior refactor; removed it and confirmed ruff check passes clean on the full diff.",
    ],
}

# ---------------------------------------------------------------------------
# Population B: Librarian candidate-pool clusters (larger groups)
# ---------------------------------------------------------------------------

POSITIVE_GROUPS_B = {
    "rework_progress_notes": [
        "Phase 1 of the memory-core rework built entity_chunk_embeddings as a sqlite-vec vec0 "
        "table with entity_id as PARTITION KEY and a deterministic entity_id::chunk_index id.",
        "Phase 1 established CHUNK_SIZE_CHARS=1200 and CHUNK_OVERLAP_CHARS=200 as the sliding-window "
        "chunking parameters, settled across three benchmark rounds.",
        "Phase 1's chunking convention is to self-load sqlite_vec defensively on every connection "
        "via try_load_vector_extension, rather than assume a prior call already attached it.",
        "Phase 2 added a content_hash column to entity_chunk_embeddings so stale chunk rows left "
        "behind by a failed async refresh could be detected and excluded.",
        "Phase 2 wired store_memory and commit_consolidation to trigger a chunk-embedding write on "
        "the same background pool used for the existing entity-level embed job.",
        "Phase 2 shipped an opt-in rerank_by_topic flag on search_memory that reranks hybrid search "
        "results using precomputed chunk vectors instead of a single whole-document vector.",
        "Phase 2's RERANK_SAME_TOPIC_THRESHOLD and RERANK_BROAD_THEME_THRESHOLD were locked from a "
        "real benchmark over hand-labeled same-topic/related-theme/unrelated triplets, not guessed.",
        "Phase 3 adds a pairwise cohesion gate to commit_consolidation and rewrites "
        "consolidate_vector_clusters to extract multiple disjoint cohesive subsets per component.",
    ],
    "quality_gate_design_notes": [
        "evaluate_memory_quality's Coleman-Liau bounds (QG_CLI_MIN=2.0, QG_CLI_MAX=26.0) reject "
        "content whose readability index falls outside a normal prose range.",
        "QG_MIN_LENGTH=20 exists specifically to reject throwaway one-line consolidation "
        "content that wouldn't survive as a standalone memory on its own.",
        "The quality gate's 3-gram/5-gram duplication ratio thresholds catch content that "
        "repeats the same short phrase over and over instead of synthesizing real information.",
        "QG_MAX_SYMBOL_RATIO caps how much of a memory's content can be non-alphanumeric symbols, "
        "catching garbled or corrupted paste-ins before they're committed as a memory.",
        "QG_MIN_TTR (type-token ratio) rejects content with too little lexical variety, a common "
        "signature of low-effort filler text rather than a genuine synthesized fact.",
        "The quality gate runs as a two-tier check: Tier 1 hard rejects on structural signals, "
        "Tier 2 applies the softer readability/entropy bounds only once Tier 1 already passed.",
    ],
}

# Mirrors `6a8fec3d`'s real 37-way omnibus shape at a representative (not literal 1:1) scale:
# a spread of genuinely unrelated engineering-note topics that a naive Connected-Components pass
# at a single fixed edge threshold could plausibly chain together via shared project vocabulary.
NEGATIVE_GROUPS_B = {
    "omnibus_style_unrelated_bundle": [
        "Decision: CADET's delegate_task always routes through the agy/cursor/codex/copilot "
        "provider abstraction rather than shelling out to each CLI directly from the caller.",
        "The SQLite connection.py wrapper centralizes PRAGMA busy_timeout and WAL mode setup so "
        "every caller gets consistent retry/backoff behavior without repeating the setup.",
        "Bugfix: commit_consolidation's entity INSERT hardcoded is_core=0, silently dropping "
        "core status whenever a core memory was consolidated alongside a non-core one.",
        "Eighth review round on the clustering plan: Codex flagged that HDBSCAN flags nearly "
        "everything as noise on small single-operator corpora, so Connected Components was kept.",
        "Engineering rule: never run sqlite3 directly against saltmdb.db -- all access must go "
        "through the MCP server tools, since direct access skips secrets-redaction middleware.",
        "Feature request: a UserPromptSubmit hook that auto-searches memory before every user "
        "turn, so relevant context is injected without an explicit search_memory call.",
        "Live-test fixture note: the smoke test spins up a throwaway SALTMDB_DB_PATH, inserts "
        "three raw entities, and asserts the FTS5 index reflects them within one write.",
        "Governance incident: an agent's own 'I fixed my mess' memory node had no real content "
        "and only existed to wire up plausible-sounding cross-links to unrelated pre-existing nodes.",
    ],
    "chaining_style_mostly_related_bundle": [
        # A genuinely-related sextet (rework_progress_notes' first 6 entries)...
        "Phase 1 of the memory-core rework built entity_chunk_embeddings as a sqlite-vec vec0 "
        "table with entity_id as PARTITION KEY and a deterministic entity_id::chunk_index id.",
        "Phase 1 established CHUNK_SIZE_CHARS=1200 and CHUNK_OVERLAP_CHARS=200 as the sliding-window "
        "chunking parameters, settled across three benchmark rounds.",
        "Phase 1's chunking convention is to self-load sqlite_vec defensively on every connection "
        "via try_load_vector_extension, rather than assume a prior call already attached it.",
        "Phase 2 added a content_hash column to entity_chunk_embeddings so stale chunk rows left "
        "behind by a failed async refresh could be detected and excluded.",
        "Phase 2 wired store_memory and commit_consolidation to trigger a chunk-embedding write on "
        "the same background pool used for the existing entity-level embed job.",
        "Phase 2 shipped an opt-in rerank_by_topic flag on search_memory that reranks hybrid search "
        "results using precomputed chunk vectors instead of a single whole-document vector.",
        # ...plus two unrelated standalone memories dragged in via shared project vocabulary,
        # mirroring `3deae748`'s confirmed chaining incident exactly (a governance incident and a
        # CI lint fix riding along with a real cluster).
        "Governance incident: an agent's own 'I fixed my mess' memory node had no real content "
        "and only existed to wire up plausible-sounding cross-links to unrelated pre-existing nodes.",
        "CI lint fix: ruff flagged an unused import in a viewer route handler left over from a "
        "prior refactor; removed it and confirmed ruff check passes clean on the full diff.",
    ],
}


def _entity_centroid(text: str) -> list:
    """Chunks + embeds one text the exact way cohesion_service.get_fresh_entity_centroids'
    fallback path does (via compute_entity_chunk_embeddings), then centroids it via the same
    _centroid helper -- so this benchmark measures the real production algorithm end to end,
    not an approximation of it."""
    chunks = chunk_text(text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
    if not chunks:
        chunks = [{"text": text}]
    vectors = embedding_service.embed_texts([c["text"] for c in chunks])
    return _centroid([np.array(v, dtype=np.float32) for v in vectors])


def _group_min_pairwise(fragments: list) -> float:
    centroids = {f"f{i}": _entity_centroid(text) for i, text in enumerate(fragments)}
    min_sim, _offending_pair = min_pairwise_cohesion(centroids)
    return min_sim


def _bucket_stats(name: str, scores: dict) -> None:
    arr = np.array(list(scores.values()))
    print(
        f"  {name:15s} n={len(arr):2d}  min={arr.min():.4f}  mean={arr.mean():.4f}  max={arr.max():.4f}"
    )
    for label, score in sorted(scores.items(), key=lambda kv: kv[1]):
        print(f"      {score:.4f}  {label}")


def _derive_threshold(positive_scores: dict, negative_scores: dict, margin: float = 0.02) -> dict:
    """0%-false-accept target: threshold set just above the worst (highest) negative-class
    score, so every negative-class group fails the gate even at the cost of a nonzero
    false-reject rate on the positive class (reported, not hidden -- see module docstring)."""
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


def run_threshold_calibration() -> dict:
    embedding_service.get_model()

    print("\n=== POPULATION A: commit-gate parent sets (2-4 fragments) ===")
    positive_scores_a = {
        name: _group_min_pairwise(frags) for name, frags in POSITIVE_GROUPS_A.items()
    }
    negative_scores_a = {
        name: _group_min_pairwise(frags) for name, frags in NEGATIVE_GROUPS_A.items()
    }
    _bucket_stats("positive", positive_scores_a)
    _bucket_stats("negative", negative_scores_a)
    result_a = _derive_threshold(positive_scores_a, negative_scores_a)
    print(f"\n  COHESION_MIN_PAIRWISE_THRESHOLD = {result_a['threshold']:.4f}")
    print(
        f"  false_accept_count = {result_a['false_accept_count']} (target: 0), "
        f"false_reject_count = {result_a['false_reject_count']} / {len(positive_scores_a)}"
    )
    if result_a["false_reject_labels"]:
        print(f"  false rejects: {result_a['false_reject_labels']}")

    print("\n=== POPULATION B: Librarian candidate-pool clusters (larger groups) ===")
    positive_scores_b = {
        name: _group_min_pairwise(frags) for name, frags in POSITIVE_GROUPS_B.items()
    }
    negative_scores_b = {
        name: _group_min_pairwise(frags) for name, frags in NEGATIVE_GROUPS_B.items()
    }
    _bucket_stats("positive", positive_scores_b)
    _bucket_stats("negative", negative_scores_b)
    result_b = _derive_threshold(positive_scores_b, negative_scores_b)
    print(f"\n  CLUSTER_MIN_PAIRWISE_THRESHOLD = {result_b['threshold']:.4f}")
    print(
        f"  false_accept_count = {result_b['false_accept_count']} (target: 0), "
        f"false_reject_count = {result_b['false_reject_count']} / {len(positive_scores_b)}"
    )

    return {
        "population_a": {
            "positive_scores": positive_scores_a,
            "negative_scores": negative_scores_a,
            **result_a,
        },
        "population_b": {
            "positive_scores": positive_scores_b,
            "negative_scores": negative_scores_b,
            **result_b,
        },
    }


if __name__ == "__main__":
    run_threshold_calibration()
