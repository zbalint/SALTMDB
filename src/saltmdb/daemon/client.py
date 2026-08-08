"""Adapter-side daemon client: ensure_daemon_running() (spawn-or-connect handshake, with periodic
re-spawn while draining), call_method()/call() (the short-lived per-call RPC primitive and its
tool_call convenience wrapper), and SessionConnection (the separate, persistent-socket hello/
goodbye connection -- never built on call_method()).

See scratch/plans/track_b_daemon_detailed.md §4/§5/§12/§13 for the full design and 5-round review
trail.
"""

import hmac
import logging
import os
import socket
import subprocess
import sys
import time
from typing import Any

from saltmdb.config import (
    DAEMON_DISCOVERY_RETRY_ATTEMPTS,
    DAEMON_DISCOVERY_RETRY_DELAY_S,
    DAEMON_RESPAWN_RETRY_INTERVAL,
    DAEMON_RPC_CALL_TIMEOUT_S,
    DAEMON_RPC_CONNECT_TIMEOUT_S,
)
from saltmdb.daemon import discovery, protocol

logger = logging.getLogger(__name__)


class DaemonStartupError(Exception):
    """Raised when ensure_daemon_running() exhausts its bounded discovery-retry window."""


class DaemonRpcError(Exception):
    """Raised for a well-formed RPC error response that isn't safe to silently retry/paper over
    (see call_method()'s two-phase failure classification for what IS handled transparently)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# Module-level singleton -- set by mcp/server.py's server_lifespan immediately after constructing
# its SessionConnection. Added after Codex round 4 (finding: call_method()'s restart-detection
# logic had no defined way to reach the SessionConnection server_lifespan creates as a local
# variable). Production has exactly one SessionConnection per process for its whole life, so a
# plain module-level reference is the correct, minimal primitive -- same "configure once" pattern
# as mcp/tools.py's configure_backend(). None outside any server_lifespan (e.g. cli.py's direct
# usage, §14) -- the restart-detection check is simply skipped in that case.
_current_session: "SessionConnection | None" = None


def _identify_probe(db_path: str, key: str) -> dict[str, Any] | None:
    """Connects to the probe port (never the election/guard port) and asks "identify". Returns
    the parsed response dict, or None on any connection/framing failure (caller treats that as
    "nothing reachable there yet", not a hard error)."""
    port = discovery.probe_port(key)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=DAEMON_RPC_CONNECT_TIMEOUT_S) as sock:
            sock.settimeout(DAEMON_RPC_CONNECT_TIMEOUT_S)
            protocol.send_frame(sock, {"method": "identify"})
            return protocol.recv_frame(sock)
    except (OSError, protocol.FrameError) as e:
        logger.debug("Identify probe against port %d failed: %s", port, e)
        return None


def _classify_startup_failure(db_path: str, key: str) -> str:
    """Bounded-window classification (§4/§7/§13): distinguishes a foreign non-SALTMDB occupant, a
    genuine different-DB election-port collision, or a daemon subprocess that simply exited, and
    returns a clear, actionable error message for each -- never a generic timeout message."""
    info = _identify_probe(db_path, key)
    if info is None:
        log_tail = _read_daemon_log_tail(db_path)
        return (
            f"election port {discovery.election_port(key)} is held by an unrelated process -- not "
            f"a SALTMDB daemon. Check what's listening (e.g. lsof -i :{discovery.election_port(key)} "
            f"/ netstat) before retrying.\nDaemon log tail:\n{log_tail}"
        )
    other_db_path = info.get("db_path")
    if other_db_path and other_db_path != db_path:
        return (
            f"election port {discovery.election_port(key)} is already owned by a SALTMDB daemon "
            f"for a different database ({other_db_path}) -- this is a rare hash collision against "
            f"{db_path}. Move one database to a different path to change its derived port."
        )
    log_tail = _read_daemon_log_tail(db_path)
    return (
        f"no SALTMDB daemon became reachable for {db_path} within the startup window.\n"
        f"Daemon log tail:\n{log_tail}"
    )


def _daemon_log_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(db_path) or os.path.expanduser("~/.saltmdb"), "daemon.log")


def _read_daemon_log_tail(db_path: str, lines: int = 20) -> str:
    try:
        log_path = _daemon_log_path(db_path)
        if not os.path.exists(log_path):
            return "(no daemon.log found)"
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return "(daemon.log unreadable)"


def _spawn_daemon_subprocess(db_path: str) -> None:
    """Detached, log-redirected daemon spawn -- matches viewer/server.py's start_viewer() existing
    Popen kwargs shape. env carries the CANONICAL db_path explicitly, never a re-derived/raw
    value. A losing contender's own election-bind attempt (daemon/server.py) fails almost
    instantly and exits cleanly, so calling this speculatively/redundantly is cheap."""
    log_dir = os.path.dirname(db_path) or os.path.expanduser("~/.saltmdb")
    os.makedirs(log_dir, exist_ok=True)
    log_path = _daemon_log_path(db_path)
    log_file = open(log_path, "a", encoding="utf-8")

    env = dict(os.environ)
    env["SALTMDB_DB_PATH"] = db_path

    popen_kwargs: dict[str, Any] = {
        "stdout": log_file,
        "stderr": log_file,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            [sys.executable, "-m", "saltmdb.daemon.server"], **popen_kwargs
        )
    finally:
        log_file.close()


def _authenticated_ping_ok(info: dict[str, Any]) -> bool:
    try:
        with socket.create_connection(
            ("127.0.0.1", info["service_port"]), timeout=DAEMON_RPC_CONNECT_TIMEOUT_S
        ) as sock:
            sock.settimeout(DAEMON_RPC_CONNECT_TIMEOUT_S)
            protocol.send_frame(sock, protocol.build_request("ping", {}, token=info["auth_token"]))
            resp = protocol.recv_frame(sock)
            return bool(resp.get("ok"))
    except (OSError, protocol.FrameError):
        return False


def ensure_daemon_running(db_path: str) -> dict[str, Any]:
    """Spawn-or-connect handshake -- the single chokepoint both SessionConnection's restart-
    detection and call_method()'s connect-phase-failure retry go through. Corrected after Codex
    round 5: canonicalizes its own input, unconditionally -- no caller is trusted to have done it
    already."""
    db_path = discovery.resolve_canonical_db_path(db_path)
    key = discovery.daemon_key(db_path)
    info = discovery.read(key)
    if info and info.get("db_path") == db_path and _authenticated_ping_ok(info):
        return info

    _spawn_daemon_subprocess(db_path)
    for attempt in range(DAEMON_DISCOVERY_RETRY_ATTEMPTS):
        time.sleep(DAEMON_DISCOVERY_RETRY_DELAY_S)
        info = discovery.read(key)
        if info and info.get("db_path") == db_path and _authenticated_ping_ok(info):
            return info
        # Round-2 fix: periodically re-attempt a fresh spawn, not just re-poll, so progress
        # doesn't depend on winning a one-shot timing race against an unrelated shutdown.
        if attempt % DAEMON_RESPAWN_RETRY_INTERVAL == 0:
            _spawn_daemon_subprocess(db_path)
    raise DaemonStartupError(_classify_startup_failure(db_path, key))


def call_method(db_path: str, method: str, params: dict[str, Any], _retry: bool = True) -> Any:
    """The short-lived RPC primitive: connect, send one frame, read one response, close. Every
    other client-side operation (call()/tool_call, ping, run_librarian_now, ...) is built on this.

    Two-phase failure classification (§12):
    1. Connect-phase failure (refused/timeout, DAEMON_SHUTTING_DOWN, or a stale-token AUTH_FAILED
       -- round-4 fix, since auth is checked before any method dispatch, it's exactly as
       side-effect-free as a connect failure): transparently re-runs ensure_daemon_running() and
       retries the SAME request once. Safe for both read and write tools.
    2. Mid-send/mid-recv failure: raised to the caller as DaemonRpcError with a
       DAEMON_CONNECTION_LOST_DURING_WRITE-shaped message for write tools (mcp/tools.py's
       _backend_or_raise().call() classifies by protocol.WRITE_TOOLS/READ_TOOLS and decides
       whether to retry transparently or surface the structured result -- see tools.py).
    """
    info = ensure_daemon_running(db_path)

    # Round-4 fix: if our SessionConnection's cached identity is stale (daemon restarted since we
    # last said hello), reconnect/re-hello before proceeding.
    if _current_session is not None:
        _current_session.ensure_fresh(db_path)

    try:
        with socket.create_connection(
            ("127.0.0.1", info["service_port"]), timeout=DAEMON_RPC_CONNECT_TIMEOUT_S
        ) as sock:
            sock.settimeout(DAEMON_RPC_CALL_TIMEOUT_S)
            request = protocol.build_request(method, params, token=info["auth_token"])
            try:
                protocol.send_frame(sock, request)
                response = protocol.recv_frame(sock)
            except (OSError, protocol.FrameError) as e:
                raise _MidCallFailure(str(e)) from e
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        if not _retry:
            raise DaemonRpcError("CONNECT_FAILED", str(e)) from e
        return call_method(db_path, method, params, _retry=False)
    except _MidCallFailure as e:
        raise DaemonRpcError("MID_CALL_FAILURE", str(e)) from e

    if not response.get("ok"):
        error = response.get("error") or {}
        code = error.get("code", protocol.INTERNAL_ERROR)
        if _retry and code in (protocol.DAEMON_SHUTTING_DOWN, protocol.AUTH_FAILED):
            return call_method(db_path, method, params, _retry=False)
        raise DaemonRpcError(code, error.get("message", ""))
    return response.get("result")


class _MidCallFailure(Exception):
    """Internal marker distinguishing a mid-send/mid-recv failure from a connect-phase one inside
    call_method()'s single try/except -- never escapes this module."""


def call(db_path: str, tool_name: str, kwargs: dict[str, Any]) -> Any:
    """Convenience wrapper: call_method("tool_call", {"tool": tool_name, "kwargs": kwargs})."""
    return call_method(db_path, "tool_call", {"tool": tool_name, "kwargs": kwargs})


class SessionConnection:
    """The long-lived, persistent-socket hello/goodbye connection -- NOT built on call_method().
    Opened once during server_lifespan's startup, held open for the adapter process's entire
    life, closed during lifespan shutdown."""

    def __init__(self, db_path: str):
        self.db_path = discovery.resolve_canonical_db_path(db_path)
        self._sock: socket.socket | None = None
        self._auth_token: str | None = None

    def open(self) -> None:
        info = ensure_daemon_running(self.db_path)
        sock = socket.create_connection(
            ("127.0.0.1", info["service_port"]), timeout=DAEMON_RPC_CONNECT_TIMEOUT_S
        )
        try:
            sock.settimeout(DAEMON_RPC_CALL_TIMEOUT_S)
            protocol.send_frame(
                sock,
                protocol.build_request(
                    "hello", {"pid": os.getpid(), "client_label": "saltmdb-adapter"}, token=info["auth_token"]
                ),
            )
            response = protocol.recv_frame(sock)
            # Codex round-1 finding: the response was previously read and discarded unchecked, so
            # a well-formed AUTH_FAILED/DAEMON_SHUTTING_DOWN error response was silently treated
            # as a successfully-opened session.
            if not response.get("ok"):
                error = response.get("error") or {}
                raise DaemonRpcError(error.get("code", "HELLO_FAILED"), error.get("message", "hello rejected"))
        except Exception:
            # Any failure past this point (framing error, rejected hello) must not leak the
            # socket this function itself opened -- there was previously no cleanup path here.
            sock.close()
            raise
        self._sock = sock
        self._auth_token = info["auth_token"]
        global _current_session
        _current_session = self

    def close(self) -> None:
        if self._sock is not None:
            try:
                protocol.send_frame(self._sock, protocol.build_request("goodbye", {}, token=self._auth_token))
            except (OSError, protocol.FrameError) as e:
                logger.debug("Best-effort goodbye failed (daemon likely already gone): %s", e)
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        global _current_session
        if _current_session is self:
            _current_session = None

    def ensure_fresh(self, db_path: str) -> None:
        """Cheap local comparison (re-reads the discovery file, no network) against the cached
        auth_token -- reconnects and re-hellos on mismatch (daemon restarted since our last hello,
        even if PID/port happen to coincide with the prior instance, round-2 fix)."""
        key = discovery.daemon_key(self.db_path)
        info = discovery.read(key)
        if info is None:
            return
        current_token = info.get("auth_token")
        if current_token and self._auth_token and hmac.compare_digest(current_token, self._auth_token):
            return
        logger.info("Daemon restart detected for %s; reconnecting session.", self.db_path)
        self.close()
        try:
            self.open()
        except Exception as e:
            logger.warning("Session reconnect failed (will retry on next call): %s", e)
