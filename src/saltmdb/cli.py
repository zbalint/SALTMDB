import argparse
import json
import os
import sys
from typing import Any


def _fmt_digest(core, project, pending_events, project_keywords):
    lines = ["## SALTMDB Session Digest"]
    if core and not (len(core) == 1 and "error" in core[0]):
        lines.append("\n### Core Rules")
        lines += [f"- {m['title']}: {m['snippet']}" for m in core]
    if project_keywords and project and not (len(project) == 1 and "error" in project[0]):
        lines.append(f"\n### Project Context ({project_keywords})")
        lines += [f"- {m['title']}: {m['snippet']}" for m in project]
    if pending_events:
        lines.append(f"\n### Pending Consolidation Requests: {len(pending_events)}")
        lines += [f"- {_summarize_event(e)}" for e in pending_events[:5]]
    return "\n".join(lines) if len(lines) > 1 else ""


def _summarize_event(ev):
    try:
        data = json.loads(ev.get("content", "{}"))
        n = len(data.get("entity_ids") or data.get("new_raw_entity_ids") or [])
        return f"{data.get('target', 'unknown')} ({n} entries, agent={ev.get('agent_id')})"
    except Exception:
        return (ev.get("content") or "")[:120]


def _search_memory_defaults(**overrides) -> dict:
    """Full kwargs shape daemon/dispatch.py's search_memory expects -- mirrors mcp/tools.py's
    search_memory defaulting exactly, since this CLI calls the daemon directly (Track B, §14)
    rather than through tools.py's own normalization layer."""
    base = {
        "entity_id": None,
        "fetch_full": False,
        "owner_id": None,
        "query_keywords": None,
        "tags_filter": None,
        "metadata_filter": None,
        "explain_mode": False,
        "limit": 5,
        "context_id": None,
        "is_core": None,
        "memory_type_filter": None,
        "tag_operator": "AND",
        "cursor": None,
        "mode": "broad",
        "include_related": True,
        "rerank_by_topic": False,
        "prefer_durable_types": True,
        "demote_superseded": True,
        "use_cross_encoder": False,
        "disable_semantic": False,
    }
    base.update(overrides)
    return base


def _get_events_defaults(**overrides) -> dict:
    """Mirrors mcp/tools.py's get_events defaulting exactly, same rationale as above."""
    base = {
        "mode": "events",
        "limit": 20,
        "offset": 0,
        "session_id": None,
        "agent_id": None,
        "type_filter": None,
        "status_filter": None,
        "owner_id": None,
    }
    base.update(overrides)
    return base


def cmd_bootstrap_digest(args):
    from saltmdb.config import get_db_path
    from saltmdb.daemon import client as daemon_client

    db_path = args.db_path or get_db_path()
    if not os.path.exists(db_path):
        return 0  # nothing to report yet, not an error

    # Track B (scratch/plans/track_b_daemon_detailed.md §14): calls through daemon/client.py the
    # same way mcp/tools.py does -- not a new code path. This is a frequently-invoked, latency-
    # sensitive hook script; it benefits from the daemon's already-warm embedding model instead of
    # paying a cold Python-process-plus-model-load cost on every invocation.
    try:
        core = daemon_client.call(
            db_path,
            "search_memory",
            _search_memory_defaults(
                is_core=True,
                limit=args.core_limit,
                include_related=False,
                disable_semantic=args.no_semantic,
            ),
        )
        if not isinstance(core, list):
            core = []
    except Exception:
        core = []

    keywords = args.project_keywords or os.path.basename(os.getcwd().rstrip("\\/"))
    project: Any = []
    if keywords:
        try:
            project = daemon_client.call(
                db_path,
                "search_memory",
                _search_memory_defaults(
                    query_keywords=keywords,
                    limit=args.project_limit,
                    include_related=False,
                    disable_semantic=args.no_semantic,
                ),
            )
            if not isinstance(project, list):
                project = []
        except Exception:
            project = []

    try:
        events = daemon_client.call(
            db_path,
            "get_events",
            _get_events_defaults(type_filter="consolidation_request", limit=args.events_limit),
        )
        pending = [e for e in events if isinstance(e, dict) and e.get("status") == "pending"]
    except Exception:
        pending = []

    print(_fmt_digest(core, project, pending, keywords))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="saltmdb-cli", description="Read-only SALTMDB CLI.")
    p.add_argument("--db-path", default=None, help="Override SALTMDB_DB_PATH.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("bootstrap-digest", help="Print a compact plain-text session digest.")
    d.add_argument("--project-keywords", default=None)
    d.add_argument("--core-limit", type=int, default=20)
    d.add_argument("--project-limit", type=int, default=5)
    d.add_argument("--events-limit", type=int, default=20)
    d.add_argument("--no-semantic", action="store_true")
    d.set_defaults(func=cmd_bootstrap_digest)

    return p


def main():
    args = build_parser().parse_args()
    try:
        sys.exit(args.func(args) or 0)
    except Exception as e:
        # Read-only reporting tool: never let an unexpected error crash the caller
        # (a SessionStart hook script shells out to this and treats stdout as context).
        print(f"# SALTMDB CLI error: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
