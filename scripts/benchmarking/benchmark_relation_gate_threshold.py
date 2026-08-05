"""Threshold-calibration benchmark for the memory-core rework Phase 5 `manage_relation`
governance gate (`relation_service.store_relation`'s similarity check on "strong" predicates --
see plans/structured-finding-matsumoto.md D3/D6 and SALTMDB memory `5c09effa`/`6490fe88`).

Locks `RELATION_GATE_MIN_SIMILARITY_THRESHOLD` in config.py from this script's real
bge-small-en-v1.5 measurement -- mirrors how COHESION_MIN_PAIRWISE_THRESHOLD/
CLUSTER_MIN_PAIRWISE_THRESHOLD (benchmark_cohesion_threshold.py, SALTMDB memory `d2d7cf85`) and
SUPERSESSION_MIN_SIMILARITY_THRESHOLD (benchmark_supersession_threshold.py, SALTMDB memory
`2765b632`) were locked from real measurement, not taken on faith. Re-run this script and re-lock
the constant if the corpus is ever meaningfully revised -- do not hand-edit the threshold.

This comparison shape is structurally different from anything already benchmarked: two RAW,
individually-authored entity centroids (each a standalone fact/bugreport/decision, the shape
`manage_relation` callers actually link), not a consolidated-summary-vs-fragment pair (Phase 4's
shape) and not a same-topic group's MIN pairwise (Phase 3's shape -- that gate looks at whole
parent SETS, this one looks at exactly one pair per call). Reusing an existing constant here
would apply a threshold calibrated for a different geometry.

Corpus (synthetic but grounded in real, documented SALTMDB-codebase facts -- the same six
fix/design topics benchmark_supersession_threshold.py draws on, reused here because they are
real, already-verified engineering facts, not because this is the same comparison shape):
  - Positive class ("should pass ungated"): two texts about the SAME specific fix/decision that
    a strong predicate (`elaborates_on`/`resolves`/`supersedes`) would concretely and correctly
    connect -- e.g. a bugreport and its confirmed root-cause-and-fix.
  - Negative class ("should require override"): the `c0ebc365` fingerprint -- one node's content
    genuinely unrelated to the other's specific topic, same broad SALTMDB-engineering domain at
    most. Built by pairing each topic's primary text against a DIFFERENT topic's primary text
    (rotated, one cross-pair per topic, mirroring benchmark_supersession_threshold.py's negative
    construction).

Risk asymmetry (see plan D6): an explicit 0%-false-accept target against the known negative set --
the threshold is chosen so EVERY negative-class pair fails the gate, even at the cost of a
nonzero false-reject rate on the positive class (reported below, not hidden).

Numpy-only cosine math over real chunk embeddings (embed_texts, chunk_text) -- same dependency
posture as the Phase 3/4 benchmarks. Reuses cohesion_service's own centroid helper directly (not
a reimplementation) so this benchmark measures the exact production algorithm.

Moved out of tests/ (and off the test_* naming convention) so `python -m unittest discover -s
tests` never collects it -- a one-time calibration pass, not a regression test.
"""

import sys

import numpy as np

from saltmdb.config import CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
from saltmdb.utils.chunking import chunk_text
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.cohesion_service import _centroid

# ---------------------------------------------------------------------------
# Six real SALTMDB-codebase topics, each a (fact_a, fact_b, predicate) triple where fact_b
# concretely elaborates/resolves/supersedes fact_a -- the positive class. predicate is recorded
# for documentation only; the gate applies one threshold across all three strong predicates.
# ---------------------------------------------------------------------------

TOPICS = {
    "wal_checkpoint_fix": {
        "predicate": "resolves",
        "fact_a": (
            "Bug report: the Librarian subprocess's WAL checkpoint is failing silently. "
            "wal_pages keeps climbing on long-running hosts even though PRAGMA "
            "wal_checkpoint(TRUNCATE) is supposed to run after every maintenance pass."
        ),
        "fact_b": (
            "Root-caused and fixed: PRAGMA wal_checkpoint(TRUNCATE) lived inside the leader "
            "lock's early-release path and never actually executed. Moved the checkpoint call "
            "into _run_librarian_maintenance, gated on holding the leader lock; wal_pages now "
            "drops to zero after every run."
        ),
    },
    "chunk_freshness_guard": {
        "predicate": "elaborates_on",
        "fact_a": (
            "entity_chunk_embeddings rows carry the entities.content_hash value that produced "
            "them, so a stale write left behind by a failed async refresh can be detected via a "
            "join requiring c.content_hash IS e.content_hash and e.status != 'archived'."
        ),
        "fact_b": (
            "backfill_chunk_embeddings extends the freshness guard into a startup repair sweep: "
            "it scans for any entity with missing or stale chunk rows and re-embeds it "
            "synchronously before the server starts serving search traffic."
        ),
    },
    "cadet_oauth_token_bug": {
        "predicate": "resolves",
        "fact_a": (
            "Bug report: a CADET delegate_task job to the agy provider hangs with zero output "
            "for over 20 minutes. Execing into the container shows the OAuth token was never "
            "written to the shared volume."
        ),
        "fact_b": (
            "Root-caused and fixed the CADET agy OAuth outage: the containerized agy process "
            "started before the host's OAuth token refresh had written the credential file into "
            "the shared volume. The container entrypoint now waits on a readiness file the host "
            "writes only after the token is confirmed present on disk."
        ),
    },
    "viewer_pidfile_leak": {
        "predicate": "elaborates_on",
        "fact_a": (
            "The web viewer's start_viewer() hardcodes its PID tracking file path, so parallel "
            "test runs against different temp databases all collide on the same real "
            "~/.saltmdb pid file and corrupt the live viewer_8080.pid state."
        ),
        "fact_b": (
            "Fix for the viewer pid-file collision: derive the pid-file path from the same "
            "db_path each viewer instance was started against, so parallel test runs against "
            "distinct temp databases no longer share a single tracking file."
        ),
    },
    "quality_gate_thresholds": {
        "predicate": "supersedes",
        "fact_a": (
            "QG_MIN_LENGTH was originally set to 10 characters in evaluate_memory_quality's "
            "Tier 1 hard-reject check, which turned out to be too permissive and let single-word "
            "throwaway content pass consolidation's quality gate."
        ),
        "fact_b": (
            "QG_MIN_LENGTH was raised from 10 to 20 characters after Tier 1 tuning, closing the "
            "gap that let short throwaway strings pass consolidation's quality gate while still "
            "allowing legitimate terse one-fact decisions through."
        ),
    },
    "predicate_canonicalization": {
        "predicate": "resolves",
        "fact_a": (
            "Bug report: typo'd predicate strings like 'super-sedes' are being stored as "
            "distinct rows from 'supersedes' in the predicates table, fragmenting relation "
            "queries that expect one canonical name per semantic predicate."
        ),
        "fact_b": (
            "Fixed via resolve_or_create_predicate: it normalizes a raw predicate string "
            "(lowercase, non-alnum runs collapsed to underscore) before checking it against the "
            "predicates table, so a typo'd variant like 'super-sedes' normalizes to the same "
            "canonical 'supersedes' row instead of fragmenting into a new one."
        ),
    },
}


def _text_centroid(text: str) -> list:
    """Chunks + embeds one text the exact way cohesion_service.get_fresh_entity_centroids'
    fallback path does for a single raw entity, then centroids it via the same _centroid helper
    -- so this benchmark measures the real production algorithm end to end, not an
    approximation of it."""
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


def run_threshold_calibration() -> dict:
    embedding_service.get_model()

    print("\n=== Relation gate: raw-entity-pair similarity, strong predicates ===")
    names = list(TOPICS.keys())

    centroid_a = {name: _text_centroid(TOPICS[name]["fact_a"]) for name in names}
    centroid_b = {name: _text_centroid(TOPICS[name]["fact_b"]) for name in names}

    # Positive class: fact_a vs fact_b WITHIN the same topic -- a strong predicate genuinely,
    # concretely connecting them (bugreport<->fix, design<->extension, old-value<->new-value).
    positive_scores = {name: _cosine(centroid_a[name], centroid_b[name]) for name in names}

    # Negative class: fact_a of topic i vs fact_a of topic (i+1) -- same broad SALTMDB
    # engineering domain, different specific fact. Rotated so every topic contributes exactly
    # one negative pair, mirroring benchmark_supersession_threshold.py's construction.
    negative_scores = {}
    for i, name in enumerate(names):
        other = names[(i + 1) % len(names)]
        negative_scores[f"{name}_vs_{other}"] = _cosine(centroid_a[name], centroid_a[other])

    _bucket_stats("positive", positive_scores)
    _bucket_stats("negative", negative_scores)

    result = _derive_threshold(positive_scores, negative_scores)
    threshold = result["threshold"]
    print(f"\n  RELATION_GATE_MIN_SIMILARITY_THRESHOLD = {threshold:.4f}")
    print(
        f"  false_accept_count = {result['false_accept_count']} (target: 0), "
        f"false_reject_count = {result['false_reject_count']} / {len(positive_scores)}"
    )
    if result["false_reject_labels"]:
        print(f"  false rejects: {result['false_reject_labels']}")
    if result["false_accept_labels"]:
        print(f"  false accepts: {result['false_accept_labels']}")

    return {
        "positive_scores": positive_scores,
        "negative_scores": negative_scores,
        **result,
    }


if __name__ == "__main__":
    try:
        run_threshold_calibration()
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)
