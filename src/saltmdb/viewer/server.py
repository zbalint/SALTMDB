import sys
import socketserver
import logging

logger = logging.getLogger(__name__)


class SALTMDBTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multithreaded TCPServer subclass that handles concurrent requests and suppresses noisy client disconnect tracebacks.

    Track B (scratch/plans/track_b_daemon_detailed.md §10): this class is now constructed directly
    by daemon/server.py (the daemon runs the Viewer as an in-process thread, not a subprocess) --
    `main()` below is retired as a production HTTP-serving entrypoint. `allow_reuse_address` is set
    explicitly by daemon/server.py before construction, since it bypasses main() where it used to
    be set here.
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc_type, exc_value, exc_tb = sys.exc_info()
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            logger.debug(
                "Client %s disconnected before request completed: %s", client_address, exc_value
            )
            return
        super().handle_error(request, client_address)


def main() -> None:
    """`saltmdb-viewer` console-script entrypoint. Retired as a direct-DB production server
    (Track B, §10) -- it never opens SQLite. It's now a thin RPC status client: asks the daemon
    (if one is running for the resolved DB path) whether its Viewer thread is up, and prints the
    URL or a clear "no daemon running" message."""
    import argparse
    from saltmdb.daemon import client as daemon_client, discovery

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="saltmdb-viewer")
    parser.parse_args()

    db_path = discovery.resolve_canonical_db_path()
    key = discovery.daemon_key(db_path)
    info = discovery.read(key)
    if info is None:
        print(f"No SALTMDB daemon running for {db_path}.", file=sys.stderr)
        sys.exit(1)

    # Codex round-1 finding: this previously asked "ping" (reachability only) and then answered
    # from the CLIENT's own local config.is_viewer_enabled()/get_viewer_port() -- if the invoking
    # shell's environment differed from whatever spawned the daemon, the reported status could be
    # flatly wrong. viewer_status is daemon-authoritative: it reports the daemon's own Viewer
    # thread state, and a successful response is itself proof of reachability (folding in what
    # the separate "ping" call used to do).
    try:
        status = daemon_client.call_method(db_path, "viewer_status", {})
    except Exception as e:
        print(f"No SALTMDB daemon reachable for {db_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not status.get("enabled"):
        print("The daemon is running but the Viewer is disabled (SALTMDB_VIEWER_ENABLED=false).")
        return

    viewer_port = status.get("port")
    print(f"SALTMDB Database Viewer is running! Open it in your browser at http://localhost:{viewer_port}")


if __name__ == "__main__":
    main()
