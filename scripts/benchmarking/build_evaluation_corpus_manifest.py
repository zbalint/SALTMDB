"""Freezes and fingerprints the evaluation corpus for the precision-first normal-search
evaluation (`scratch/plans/precision_first_search_evaluation.md`, §1 / §6 artifact 1).

Reuses `scratch/diverse_corpus_full.db` as-is (built by `build_diverse_test_db.py`: a locally
supplied cross-domain evaluation layer plus ingested public datasets for volume and multilingual
coverage) rather than rebuilding it. The private source corpus is deliberately not distributed
with the repository. This script does not ingest or modify anything; it only reads a throwaway
copy and records what's in it, using the same
`_refuse_live_db`/copy-only convention every sibling script in this directory uses.

Output feeds every later stage of the evaluation plan: the fingerprint here is what
`run_evaluation_matrix.py` / `analyze_evaluation_matrix.py` cross-check to confirm every stage of
the pipeline ran against the exact same frozen corpus (same role `compare_benchmark_runs.py`'s
`corpus_manifest.fingerprint` mismatch-warning already plays for its own before/after pair).

Usage:
    python scripts/benchmarking/build_evaluation_corpus_manifest.py --db-path PATH --out PATH

Refuses to run against the live default DB path even read-only, per SALTMDB dev rule (memory
`51baf28d`). Point --db-path at a throwaway copy of scratch/diverse_corpus_full.db, never that
file directly (avoid any accidental write lock on the shared frozen fixture), and never the live
DB.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from saltmdb.config import get_db_path
from saltmdb.db.connection import close_connection, get_connection

SOURCE_CORPUS_DEFAULT = Path(__file__).parent.parent.parent / "scratch" / "diverse_corpus_full.db"
SOURCE_MANIFEST_DEFAULT = Path(f"{SOURCE_CORPUS_DEFAULT}.manifest.json")

# Pinned per the source evaluation plan §1 -- "Hold the cross-encoder model fixed; model
# selection is a separate experiment." Matches benchmark_precision_snapshot.py's hardcoded
# EMBEDDING_MODEL_NAME and benchmark_search_option_matrix.py's DEFAULT_RERANKER_MODEL.
PINNED_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
PINNED_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

PLAN_PATH = (
    Path(__file__).parent.parent.parent
    / "scratch"
    / "plans"
    / ("precision_first_search_evaluation.md")
)


def _refuse_live_db(db_path: str) -> None:
    live_path = os.path.abspath(get_db_path())
    if os.path.abspath(db_path) == live_path:
        raise RuntimeError(
            f"Refusing to run against the live default DB path ({live_path}). "
            "Point --db-path at a throwaway copy instead."
        )


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit(repo_dir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _entity_fingerprint(conn) -> dict:
    """Same shape as benchmark_precision_snapshot.py's _corpus_manifest: cheap (id, updated_at)
    hash over every non-archived entity, plus counts and a per-memory-type breakdown."""
    total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    non_archived = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status != 'archived'"
    ).fetchone()[0]
    by_type = dict(
        conn.execute(
            "SELECT memory_type, COUNT(*) FROM entities WHERE status != 'archived' "
            "GROUP BY memory_type"
        ).fetchall()
    )
    rows = conn.execute(
        "SELECT id, updated_at FROM entities WHERE status != 'archived' ORDER BY id"
    ).fetchall()
    fingerprint_src = "|".join(f"{rid}:{rupdated}" for rid, rupdated in rows)
    entity_fingerprint = hashlib.sha256(fingerprint_src.encode()).hexdigest()[:16]
    return {
        "total_entities": total,
        "non_archived_entities": non_archived,
        "by_memory_type": by_type,
        "entity_fingerprint": entity_fingerprint,
    }


def _relations_inventory(conn) -> dict:
    """Plan §0 item 2 / §1: real relation-edge inventory, load-bearing for sourcing the "current
    vs superseded" (active `supersedes` edges) and "closely related incidents" (`elaborates_on`/
    `similar_to`/`resolves`) query categories -- computed here, once, so every downstream script
    reads counts from the frozen manifest rather than re-deriving them."""
    by_predicate = dict(
        conn.execute("SELECT predicate, COUNT(*) FROM relations GROUP BY predicate").fetchall()
    )
    active_supersedes_rows = conn.execute(
        "SELECT source_id, target_id FROM relations "
        "WHERE predicate = 'supersedes' AND valid_to IS NULL"
    ).fetchall()
    active_supersedes_usable = [(s, t) for s, t in active_supersedes_rows if s != t]
    return {
        "by_predicate": by_predicate,
        "active_supersedes_total": len(active_supersedes_rows),
        "active_supersedes_self_loop_count": len(active_supersedes_rows)
        - len(active_supersedes_usable),
        "active_supersedes_usable_count": len(active_supersedes_usable),
    }


def build_manifest(db_path: str, source_manifest_path: Path) -> dict:
    _refuse_live_db(db_path)
    file_fingerprint = _file_sha256(Path(db_path))

    conn = get_connection(db_path)
    try:
        entity_info = _entity_fingerprint(conn)
        relations_info = _relations_inventory(conn)
    finally:
        close_connection(conn)

    source_manifest = None
    if source_manifest_path.exists():
        source_manifest = json.loads(source_manifest_path.read_text())

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "generated_by_git_commit": _git_commit(Path(__file__).resolve().parents[2]),
        "plan_path": str(PLAN_PATH),
        "db_copy_path": str(db_path),
        "db_file_sha256": file_fingerprint,
        **entity_info,
        "relations_inventory": relations_info,
        "source_build_manifest": source_manifest,
        "pinned_models": {
            "embedding_model": PINNED_EMBEDDING_MODEL,
            "reranker_model": PINNED_RERANKER_MODEL,
        },
        "limitations": [
            "Corpus is frozen as of the source build_diverse_test_db.py run (see "
            "source_build_manifest); it does NOT include this repo's memories/commits created "
            "after that build. Deliberate -- see plan §1.",
            "Decision/procedure/preference/event category volume is bounded by what the locally "
            "supplied corpus contains, not invented to hit a target count.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to a throwaway COPY of scratch/diverse_corpus_full.db (never the live DB, "
        "never the shared fixture file directly).",
    )
    parser.add_argument(
        "--source-manifest",
        default=str(SOURCE_MANIFEST_DEFAULT),
        help="Path to build_diverse_test_db.py's own manifest for the source corpus.",
    )
    parser.add_argument("--out", required=True, help="Output corpus_manifest.json path.")
    args = parser.parse_args()

    manifest = build_manifest(args.db_path, Path(args.source_manifest))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {args.out}", file=sys.stderr)
