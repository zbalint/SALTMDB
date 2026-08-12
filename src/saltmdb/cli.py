import argparse
import concurrent.futures
import os
import sys
from typing import Any


def _fmt_memory(m: dict) -> str:
    mid = m.get("id", "")
    mtype = m.get("memory_type") or "fact"
    is_core = str(bool(m.get("is_core", False))).lower()
    # Escape double-quotes and strip newlines so the YAML single-line title value stays valid.
    title = m.get("title", "").replace('"', '\\"').replace("\n", " ")
    # Escape closing tag in content to prevent premature block termination.
    content = (m.get("full_content") or m.get("snippet") or "").replace(
        "</memory>", "&lt;/memory&gt;"
    )
    return "\n".join(
        [
            f'<memory id="{mid}" type="{mtype}" is_core="{is_core}">',
            "---",
            f'title: "{title}"',
            f"type: {mtype}",
            f"is_core: {is_core}",
            "---",
            "",
            content,
            "</memory>",
        ]
    )


def _fmt_digest(core: list, project: list, project_keywords: str | None) -> str:
    lines = ["<saltmdb-digest>"]

    if core and not (len(core) == 1 and "error" in core[0]):
        lines.append("\n<core-rules>")
        for m in core:
            lines.append("")
            lines.append(_fmt_memory(m))
        lines.append("\n</core-rules>")

    if project_keywords and project and not (len(project) == 1 and "error" in project[0]):
        lines.append(f'\n<project-context keywords="{project_keywords}">')
        for m in project:
            lines.append("")
            lines.append(_fmt_memory(m))
        lines.append("\n</project-context>")

    lines.append("\n</saltmdb-digest>")
    return "\n".join(lines)


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
        "prefer_durable_types": False,
        "demote_superseded": False,
        "use_cross_encoder": False,
        "disable_semantic": False,
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
    except Exception as e:
        print(f"# Warning: Failed to fetch core memory: {e}", file=sys.stderr)
        core = []

    keywords = args.project_keywords or os.path.basename(os.getcwd().rstrip("\\/"))
    project: Any = []
    if keywords and args.project_limit > 0:
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
        except Exception as e:
            print(f"# Warning: Failed to fetch project memory: {e}", file=sys.stderr)
            project = []

    def _fetch_full(item):
        if isinstance(item, dict) and "id" in item:
            try:
                full_text = daemon_client.call(
                    db_path,
                    "search_memory",
                    _search_memory_defaults(entity_id=item["id"], fetch_full=True),
                )
                item["full_content"] = (
                    full_text if isinstance(full_text, str) else item.get("snippet", "")
                )
            except Exception as e:
                print(
                    f"# Warning: Failed to fetch full context for {item.get('id')}: {e}",
                    file=sys.stderr,
                )
                item["full_content"] = item.get("snippet", "")

    all_items = [i for i in (core + project) if isinstance(i, dict) and "id" in i]
    if all_items:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(_fetch_full, all_items))

    print(_fmt_digest(core, project, keywords))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="saltmdb-cli", description="Read-only SALTMDB CLI.")
    p.add_argument("--db-path", default=None, help="Override SALTMDB_DB_PATH.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("bootstrap-digest", help="Print a compact plain-text session digest.")
    d.add_argument("--project-keywords", default=None)
    d.add_argument("--core-limit", type=int, default=20)
    d.add_argument("--project-limit", type=int, default=0)
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
