import argparse
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


def build_parser():
    p = argparse.ArgumentParser(prog="saltmdb-cli", description="Read-only SALTMDB CLI.")
    p.add_argument("--db-path", default=None, help="Override SALTMDB_DB_PATH.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser(
        "bootstrap-digest",
        help="Print the canonical core-memory bootstrap digest (session-start hook).",
    )
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
