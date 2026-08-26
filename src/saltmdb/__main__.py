import sys
import logging
from saltmdb.config import get_db_path, get_owner_id

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():  # noqa: PLR0915 -- one more statement than the threshold from the new signal-shutdown
    # block; splitting it out would obscure the branch it belongs to more than it would help (same
    # tradeoff daemon/server.py's own main() already accepts via its noqa: C901, PLR0912, PLR0915).
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
        import os
        import signal
        import anyio
        from saltmdb.daemon.client import ensure_daemon_running, get_current_session
        from saltmdb.mcp import tools
        from saltmdb.mcp.server import mcp
        from saltmdb.mcp.identity import SESSION_IDENTITY

        # Identity is deployment configuration, not agent-controlled tool input.
        SESSION_IDENTITY.configure_owner(get_owner_id())
        db_path = get_db_path()
        # Migration invisibility (§14): every existing MCP client registration continues to spawn
        # this exact same command; only what it does internally has changed -- a thin adapter
        # talking to the daemon over RPC, instead of a full DB-owning server. Backend
        # configuration happens here, synchronously, before mcp.run() -- not inside
        # server_lifespan (which owns only the SessionConnection, §5/§8).
        ensure_daemon_running(db_path)
        tools.configure_backend(tools.RpcBackend())

        async def _run_adapter_until_shutdown() -> None:
            """Run the MCP server alongside a SIGTERM/SIGINT watcher so a host-initiated kill
            still sends `goodbye` instead of the process dying immediately with no cleanup.

            Cancelling the task running mcp.run_stdio_async() is deliberately NOT how the signal
            path unwinds: its stdin read runs via anyio's generic blocking-thread file wrapper,
            and the host may still hold its own write end of the stdio pipe open even after
            sending the signal, so that blocked read can never unblock on its own -- cancellation
            would never actually propagate back through server_lifespan's `finally`. Instead, on
            a signal, reach the one live SessionConnection directly (daemon/client.py's
            get_current_session(), the same object server_lifespan's own `finally` would
            otherwise close) and send goodbye synchronously, then force-exit -- mirroring
            daemon/server.py's own os._exit(0) pattern for exactly this "can't cleanly join a
            stuck blocking operation" situation. The normal graceful-EOF path (host closes stdin)
            is untouched: mcp.run_stdio_async() still returns on its own and server_lifespan's
            `finally` still runs goodbye for that case."""
            async with anyio.create_task_group() as tg:

                async def _watch_signals() -> None:
                    sigs = (
                        (signal.SIGINT,)
                        if sys.platform == "win32"
                        else (signal.SIGTERM, signal.SIGINT)
                    )
                    with anyio.open_signal_receiver(*sigs) as signals:
                        async for sig in signals:
                            logger.info("SALTMDB adapter received signal %s; shutting down.", sig)
                            session = get_current_session()
                            if session is not None:
                                try:
                                    session.close()
                                except Exception:
                                    logger.exception(
                                        "Failed to send goodbye during signal shutdown"
                                    )
                            os._exit(0)

                tg.start_soon(_watch_signals)
                try:
                    await mcp.run_stdio_async()
                finally:
                    # Server exited on its own (normal stdin EOF) -- stop the signal watcher too,
                    # instead of leaving the task group waiting on a signal that may never come.
                    tg.cancel_scope.cancel()

        anyio.run(_run_adapter_until_shutdown)


if __name__ == "__main__":
    main()
