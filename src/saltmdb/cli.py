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


def cmd_bootstrap_digest(args):
    from saltmdb.config import get_db_path

    db_path = args.db_path or get_db_path()
    if not os.path.exists(db_path):
        return 0  # nothing to report yet, not an error

    from saltmdb.db.connection import get_connection, close_connection
    from saltmdb.domain.services.memory_service import search_memory
    from saltmdb.domain.services.event_service import get_recent_events

    if args.no_semantic:
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "false"

    conn = get_connection(db_path)
    try:
        core = search_memory(
            is_core=True, limit=args.core_limit, include_related=False, db_connection=conn
        )
    except Exception:
        core = []
    keywords = args.project_keywords or os.path.basename(os.getcwd().rstrip("\\/"))
    project: Any = []
    if keywords:
        try:
            project = search_memory(
                query_keywords=keywords,
                limit=args.project_limit,
                include_related=False,
                db_connection=conn,
            )
        except Exception:
            project = []
    try:
        events = get_recent_events(
            type_filter="consolidation_request", limit=args.events_limit, db_connection=conn
        )
        pending = [e for e in events if isinstance(e, dict) and e.get("status") == "pending"]
    except Exception:
        pending = []
    close_connection(conn)

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
