import argparse
import json
import os
import sys


def cmd_bootstrap_digest(args):
    from saltmdb.config import get_db_path
    from saltmdb.daemon import client as daemon_client

    db_path = args.db_path or get_db_path()
    if not os.path.exists(db_path):
        return 0  # nothing to report yet, not an error

    # Core-memory bootstrap governance (see core_governance_service.py, the sole owner of digest
    # rendering): a single daemon call returns the already-rendered, already-fail-closed-checked
    # digest -- no project-keyword search, no core-limit truncation, no per-item full-content N+1
    # fetch loop. Calls through daemon/client.py the same way mcp/tools.py does (Track B), so this
    # frequently-invoked, latency-sensitive hook script benefits from the daemon's already-warm
    # embedding model instead of paying a cold Python-process-plus-model-load cost every time.
    try:
        digest = daemon_client.call(db_path, "get_core_bootstrap_digest", {})
    except Exception as e:
        print(f"# Warning: Failed to fetch core-memory bootstrap digest: {e}", file=sys.stderr)
        return 0

    print(digest if isinstance(digest, str) else "<saltmdb-digest>\n\n</saltmdb-digest>")
    return 0


def cmd_export_corpus_snapshot(args):
    """Agent API redesign plan §5.12/Phase 7 item 29: export_corpus_snapshot moved off MCP --
    "evaluation/benchmark tooling for building SALTMDB itself, not agent operation" (§5.12).

    Streams every page of iter_corpus_snapshot_pages (one continuous read transaction, so the
    whole export shares one immutable provenance -- no cursor/snapshot_hash juggling needed,
    unlike the old per-call MCP contract) and merges them into the single complete-final-page
    document scripts/benchmarking/freeze_live_corpus.py already expects (has_more=False,
    next_cursor=None, entity_count == len(entities)).
    """
    from saltmdb.config import get_db_path
    from saltmdb.domain.services.corpus_snapshot_service import (
        CorpusSnapshotError,
        SnapshotChangedError,
        iter_corpus_snapshot_pages,
    )

    # --owner-id is argparse-required (see build_parser), so it's always present here.
    db_path = args.db_path or get_db_path()
    try:
        pages = list(
            iter_corpus_snapshot_pages(
                owner_id=args.owner_id,
                page_size=args.page_size,
                include_archived=args.include_archived,
                db_path=db_path,
            )
        )
    except (CorpusSnapshotError, SnapshotChangedError) as e:
        print(f"# Error: {e}", file=sys.stderr)
        return 1

    merged_entities: list = []
    for page in pages:
        merged_entities.extend(page["entities"])  # type: ignore[arg-type]

    combined = dict(pages[-1]) if pages else {"entities": [], "entity_count": 0}
    combined["entities"] = merged_entities
    combined["has_more"] = False
    combined["next_cursor"] = None
    if combined.get("entity_count") != len(merged_entities):
        print(
            "# Error: merged entity count does not match the snapshot's reported entity_count "
            "-- the corpus changed mid-export; retry",
            file=sys.stderr,
        )
        return 1

    output = json.dumps(combined, indent=2, sort_keys=True, ensure_ascii=False)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        tmp_path = args.out + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(output + "\n")
        os.replace(tmp_path, args.out)
    else:
        print(output)
    return 0


def cmd_orphans(args):
    """Agent API redesign plan §5.4/Phase 7 item 30: orphan detection moved off MCP.

    No entity_id, owner-wide maintenance scan -- flagged reversible under the plan's §1.2 (an
    unlinked memory is still fully searchable; the corpus does not degrade if this never runs).
    """
    from saltmdb.config import get_db_path
    from saltmdb.domain.services.memory_service import detect_orphaned_memories

    db_path = args.db_path or get_db_path()
    result = detect_orphaned_memories(owner_id=args.owner_id, db_path=db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if "error" in result else 0


def _corpus_health_data(conn, days: int, telemetry_limit: int) -> dict:
    """Assembles the corpus-health report body from existing, already-populated tables/services
    -- no new persisted state, no new subsystem. Every signal here already exists for exactly
    this purpose: entity/tag/predicate counts are plain aggregates over tables the store already
    maintains, orphans/overdue-cores reuse the same domain-service functions the (removed) MCP
    surface and review_core_memory already use, and tool_call_telemetry's own schema.py comment
    says "Strictly local, CLI-readable, not an MCP tool" -- this is that reader."""
    from datetime import UTC, datetime, timedelta

    from saltmdb.domain.services.core_governance_service import build_inventory, load_active_cores
    from saltmdb.domain.services.memory_service import detect_orphaned_memories
    from saltmdb.utils.predicate_vocabulary import (
        AGENT_SELECTABLE_PREDICATES,
        LEGACY_READONLY_PREDICATES,
        RESERVED_PREDICATES,
    )

    entity_counts = dict(
        conn.execute("SELECT status, COUNT(*) FROM entities GROUP BY status").fetchall()
    )
    core_count = conn.execute("SELECT COUNT(*) FROM entities WHERE is_core = 1").fetchone()[0]

    orphans = detect_orphaned_memories(db_connection=conn)

    core_inventory = build_inventory(load_active_cores(conn))
    overdue_cores = [c for c in core_inventory if c["review_due"]]

    tag_total = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    tag_canonical = conn.execute("SELECT COUNT(*) FROM tags WHERE canonical_id IS NULL").fetchone()[
        0
    ]

    canonical_predicate_names = (
        AGENT_SELECTABLE_PREDICATES | set(RESERVED_PREDICATES) | LEGACY_READONLY_PREDICATES
    )
    active_predicate_rows = conn.execute(
        "SELECT predicate, COUNT(*) FROM relations WHERE valid_to IS NULL GROUP BY predicate"
    ).fetchall()
    drifted_predicates = {
        pred: count
        for pred, count in active_predicate_rows
        if pred not in canonical_predicate_names
    }

    # Computed in Python, not SQL: tool_call_telemetry.timestamp is written as
    # datetime.now(UTC).isoformat() ("...T...+00:00"), never SQLite's own datetime('now')
    # format ("... " with a space separator, no offset) -- comparing the two as strings would
    # silently never filter anything (a space sorts before 'T', so a SQLite-format cutoff is
    # lexicographically "less than" every stored row regardless of the actual dates).
    telemetry_since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    telemetry_total = conn.execute(
        "SELECT COUNT(*) FROM tool_call_telemetry WHERE timestamp >= ?", (telemetry_since,)
    ).fetchone()[0]
    telemetry_by_status = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM tool_call_telemetry WHERE timestamp >= ? GROUP BY status",
            (telemetry_since,),
        ).fetchall()
    )
    telemetry_by_error_code = dict(
        conn.execute(
            "SELECT error_code, COUNT(*) FROM tool_call_telemetry "
            "WHERE timestamp >= ? AND error_code IS NOT NULL "
            "GROUP BY error_code ORDER BY COUNT(*) DESC LIMIT ?",
            (telemetry_since, telemetry_limit),
        ).fetchall()
    )
    avg_latency_row = conn.execute(
        "SELECT AVG(latency_ms) FROM tool_call_telemetry WHERE timestamp >= ?", (telemetry_since,)
    ).fetchone()
    flagged_stale_count = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status != 'archived' AND json_extract(metadata, '$.drift_flag') IS NOT NULL"
    ).fetchone()[0]

    return {
        "entities": {
            "raw": entity_counts.get("raw", 0),
            "consolidated": entity_counts.get("consolidated", 0),
            "archived": entity_counts.get("archived", 0),
            "core": core_count,
            "total": sum(entity_counts.values()),
        },
        "flagged_stale": {
            "note": (
                "entities carrying a non-null metadata.drift_flag (set only by the manual/cron "
                "hooks/saltmdb-checkable-fact-drift-sweep.py sweep) -- advisory only, never "
                "auto-corrected; archived entities are excluded since they are never retrieved."
            ),
            "count": flagged_stale_count,
        },
        "orphaned_memories": orphans,
        "overdue_core_reviews": {
            "count": len(overdue_cores),
            "entries": overdue_cores,
        },
        "tag_fragmentation": {
            "canonical_tags": tag_canonical,
            "total_tags": tag_total,
            "alias_tags": tag_total - tag_canonical,
        },
        "predicate_drift": {
            "note": (
                "active relation edges whose predicate is outside the closed vocabulary "
                "(saltmdb.utils.predicate_vocabulary) -- pending Phase 8 data migration, not a "
                "live defect: the write-time gate already prevents new drift."
            ),
            "drifted_active_edge_count": sum(drifted_predicates.values()),
            "by_predicate": drifted_predicates,
        },
        "telemetry": {
            "window_days": days,
            "total_calls": telemetry_total,
            "by_status": telemetry_by_status,
            "top_error_codes": telemetry_by_error_code,
            "avg_latency_ms": round(avg_latency_row[0], 2)
            if avg_latency_row[0] is not None
            else None,
        },
    }


def cmd_corpus_health(args):
    """Agent API redesign plan §5.10/Phase 7 item 31: corpus-health report as CLI output for the
    optional hooks tier. Never an MCP tool -- an agent-facing report an agent must choose to
    request and adopt is the Librarian's queue with a different transport (§1.3), and would be
    ignored identically while costing permanent schema budget. This command exists to feed a
    scheduled hook where curation is the session's entire purpose, not to be called by an agent
    mid-task.
    """
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import managed_connection

    db_path = args.db_path or get_db_path()
    if not os.path.exists(db_path):
        print("# No database found yet -- nothing to report.", file=sys.stderr)
        return 0

    with managed_connection(db_path=db_path) as conn:
        report = _corpus_health_data(conn, days=args.days, telemetry_limit=args.telemetry_limit)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="saltmdb-cli", description="Read-only SALTMDB CLI.")
    p.add_argument("--db-path", default=None, help="Override SALTMDB_DB_PATH.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser(
        "bootstrap-digest",
        help="Print the canonical core-memory bootstrap digest (session-start hook).",
    )
    d.set_defaults(func=cmd_bootstrap_digest)

    e = sub.add_parser(
        "export-corpus-snapshot",
        help="Export the full corpus as one immutable, hash-verified JSON snapshot "
        "(evaluation/benchmark tooling, not agent operation).",
    )
    e.add_argument("--owner-id", required=True, help="Owner whose corpus to export.")
    e.add_argument("--page-size", type=int, default=None, help="Rows fetched per internal page.")
    e.add_argument(
        "--include-archived", action="store_true", help="Include archived entities in the export."
    )
    e.add_argument("--out", default=None, help="Write to this file instead of stdout.")
    e.set_defaults(func=cmd_export_corpus_snapshot)

    o = sub.add_parser(
        "orphans", help="List active memories with zero relationship links (maintenance scan)."
    )
    o.add_argument("--owner-id", default=None, help="Restrict the scan to one owner.")
    o.set_defaults(func=cmd_orphans)

    c = sub.add_parser(
        "corpus-health",
        help="Print a corpus-health report (orphans, overdue core reviews, tag/predicate "
        "fragmentation, recent telemetry) for the optional scheduled-maintenance hooks tier.",
    )
    c.add_argument(
        "--days", type=int, default=7, help="Telemetry lookback window in days (default 7)."
    )
    c.add_argument(
        "--telemetry-limit",
        type=int,
        default=10,
        help="Max distinct error codes to report, ranked by count (default 10).",
    )
    c.set_defaults(func=cmd_corpus_health)

    return p


def main():
    args = build_parser().parse_args()
    try:
        sys.exit(args.func(args) or 0)
    except Exception as e:
        print(f"# SALTMDB CLI error: {e}", file=sys.stderr)
        # bootstrap-digest is consumed by a SessionStart hook that treats stdout as best-effort
        # context and must never fail the session merely because SALTMDB itself errored -- that
        # command alone is swallowed to exit 0 (matching its pre-Phase-7 behavior exactly). Every
        # other subcommand is either a human-run dev tool (export-corpus-snapshot) or feeds a
        # scheduled-maintenance hook (orphans, corpus-health) that needs a real nonzero exit code
        # to detect failure, so those propagate genuinely.
        sys.exit(0 if args.command == "bootstrap-digest" else 1)


if __name__ == "__main__":
    main()
