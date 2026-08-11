import sys
import logging
from saltmdb.config import get_db_path

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    # Track B (scratch/plans/track_b_daemon_detailed.md §11): both --backfill-chunk-embeddings
    # and --librarian are RPC-forwarding clients now, not direct-DB one-shot processes -- neither
    # opens SQLite directly anymore. daemon_client.call_method() itself calls
    # ensure_daemon_running() first, so these still work even if no adapter session is currently
    # active (a daemon is started on demand if none exists).
    if "--backfill-chunk-embeddings" in sys.argv:
        from saltmdb.daemon import client as daemon_client

        db_path = get_db_path()
        logger.info("Requesting a chunk-embedding backfill pass from the daemon for %s...", db_path)
        try:
            result = daemon_client.call_method(db_path, "run_backfill_chunk_embeddings_now", {})
        except Exception as e:
            print(
                f"Error: could not reach a SALTMDB daemon for {db_path}: {e}",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)
        print(result, flush=True)
    elif "--librarian" in sys.argv:
        from saltmdb.daemon import client as daemon_client

        db_path = get_db_path()
        logger.info("Requesting a Librarian maintenance pass from the daemon for %s...", db_path)
        try:
            result = daemon_client.call_method(db_path, "run_librarian_now", {"force": True})
        except Exception as e:
            print(
                f"Error: could not reach a SALTMDB daemon for {db_path}: {e}",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)
        print(result, flush=True)
    else:
        from saltmdb.daemon.client import ensure_daemon_running
        from saltmdb.mcp import tools
        from saltmdb.mcp.server import mcp

        db_path = get_db_path()
        # Migration invisibility (§14): every existing MCP client registration continues to spawn
        # this exact same command; only what it does internally has changed -- a thin adapter
        # talking to the daemon over RPC, instead of a full DB-owning server. Backend
        # configuration happens here, synchronously, before mcp.run() -- not inside
        # server_lifespan (which owns only the SessionConnection, §5/§8).
        ensure_daemon_running(db_path)
        tools.configure_backend(tools.RpcBackend())
        mcp.run()


if __name__ == "__main__":
    main()
