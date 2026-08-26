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
import subprocess  # nosec B404 -- daemon spawn uses a fixed module argv and never shell input.
import sys
import threading
import time
from contextlib import nullcontext
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


def get_current_session() -> "SessionConnection | None":
    """Public accessor for the process's one SessionConnection, if any is currently open.

    Added so a signal handler outside server_lifespan's own closure (see __main__.py's
    SIGTERM/SIGINT handling) can reach the exact same object server_lifespan's `finally` would
    otherwise close, to send a synchronous goodbye before a forced process exit."""
    return _current_session


def _identify_probe(db_path: str, key: str) -> dict[str, Any] | None:
    """Connects to the probe port (never the election/guard port) and asks "identify". Returns
    the parsed response dict, or None on any connection/framing failure (caller treats that as
    "nothing reachable there yet", not a hard error)."""
    port = discovery.probe_port(key)
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=DAEMON_RPC_CONNECT_TIMEOUT_S
        ) as sock:
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
    """Spawn-dispatch chokepoint: on win32, goes through a short-lived intermediary launcher
    (see _spawn_daemon_via_intermediary) instead of spawning the daemon directly, because
    SALTMDB memory 61fc01d0 (live ProcMon capture, 2026-08-26) confirmed the Claude Code CLI
    harness runs `taskkill /PID <session-root> /T /F` on its own session teardown -- a tree-kill
    that walks a LIVE ParentProcessID snapshot at kill time and is completely unaffected by
    DETACHED_PROCESS/CREATE_BREAKAWAY_FROM_JOB/CREATE_NEW_PROCESS_GROUP (alpha.91/94/95, all
    confirmed working for the OS mechanisms they target -- job membership and console
    attachment -- yet the daemon kept dying anyway, because a tree-walk consults neither). On
    POSIX, start_new_session's setsid() below is unaffected by this Windows-specific finding and
    spawns the daemon directly as before."""
    if sys.platform == "win32":
        _spawn_daemon_via_intermediary(db_path)
    else:
        _spawn_daemon_process(db_path)


def _spawn_daemon_via_intermediary(db_path: str) -> None:
    """win32-only: spawns a short-lived launcher (`python -m saltmdb.daemon.client
    --spawn-detached <db_path>`, see _intermediary_main) that itself spawns the real daemon via
    _spawn_daemon_process and then exits immediately. `taskkill /T`'s tree-walk builds its kill
    set from a LIVE process-table snapshot taken at kill time, walking ParentProcessID from the
    terminating session's root PID -- once this intermediary has exited, it is simply absent
    from that snapshot, so the walk cannot traverse through it to discover the daemon as a
    grandchild. This is the Windows analogue of the POSIX double-fork orphaning trick. The
    intermediary itself is spawned with the same DETACHED_PROCESS/CREATE_BREAKAWAY_FROM_JOB/
    CREATE_NEW_PROCESS_GROUP flags as the daemon, for the same console/job reasons -- it just
    also needs to survive long enough to finish spawning its own child."""
    env = dict(os.environ)
    env["SALTMDB_DB_PATH"] = db_path
    args = [sys.executable, "-m", "saltmdb.daemon.client", "--spawn-detached", db_path]
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        # DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP -- same combo
        # and same rationale as _spawn_daemon_process's win32 branch below.
        "creationflags": 0x00000008 | 0x01000000 | 0x00000200,
    }
    try:
        proc = subprocess.Popen(args, **popen_kwargs)  # nosec B603 -- fixed argv, shell=False.
        logger.info(
            "Spawned intermediary launcher: pid=%d (will spawn daemon then exit immediately, "
            "breaking the ParentProcessID chain taskkill /T walks)",
            proc.pid,
        )
    except OSError as e:
        logger.warning(
            "Intermediary launcher spawn failed (%s); falling back to spawning the daemon "
            "directly -- daemon will remain a live descendant of this process, exposed to any "
            "future taskkill /T tree-walk rooted at it",
            e,
        )
        _spawn_daemon_process(db_path)


def _spawn_daemon_process(db_path: str) -> None:
    """Detached, log-redirected daemon spawn -- matches viewer/server.py's start_viewer() existing
    Popen kwargs shape. env carries the CANONICAL db_path explicitly, never a re-derived/raw
    value. A losing contender's own election-bind attempt (daemon/server.py) fails almost
    instantly and exits cleanly, so calling this speculatively/redundantly is cheap. Called
    directly on POSIX; called from inside the short-lived intermediary launcher on win32 (see
    _spawn_daemon_via_intermediary)."""
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
        # DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP -- the combo is
        # the Windows analogue of start_new_session's setsid() below, addressing THREE SEPARATE
        # OS-level cleanup mechanisms that can each kill the daemon early:
        #  - CREATE_BREAKAWAY_FROM_JOB: without it, the daemon stays a member of whatever Job
        #    Object its ancestor belongs to (VS Code/Copilot's extension host commonly assigns
        #    its whole child-process tree to a job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, to
        #    avoid orphaned processes), so the daemon gets force-killed the instant that job
        #    closes. Live testing (SALTMDB memories 7a96d8cb/1774920a) showed the daemon still
        #    dying silently with in_job=False, i.e. NOT a job-object member -- ruling this
        #    mechanism out as the sole cause.
        #  - DETACHED_PROCESS: alpha.94 added CREATE_NEW_PROCESS_GROUP alone and it still died
        #    live on every retest (memory 652ad9ff's fix, disproven the same saga). Per Microsoft's
        #    own docs, that flag only rescopes GenerateConsoleCtrlEvent's CTRL_C/CTRL_BREAK
        #    targeting -- the OS-generated CTRL+CLOSE signal (console window closed) is delivered
        #    to "all processes attached to the console" unconditionally, regardless of process
        #    group (learn.microsoft.com/windows/console/ctrl-close-signal). Without CREATE_NEW_
        #    CONSOLE or DETACHED_PROCESS, a child attaches to its parent's console by default, so
        #    the daemon was never actually detached at all. DETACHED_PROCESS gives it no console
        #    to be attached to, which is the only thing that stops CTRL+CLOSE delivery. (Unlike
        #    CREATE_NEW_CONSOLE, this doesn't pop a visible window, so CREATE_NO_WINDOW -- which
        #    the docs say is ignored when paired with DETACHED_PROCESS anyway -- is dropped here.)
        #  - CREATE_NEW_PROCESS_GROUP: kept as defense-in-depth against CTRL_C/CTRL_BREAK
        #    specifically, in case the daemon process ever ends up console-attached again (e.g.
        #    a future AllocConsole/AttachConsole call); harmless, ignored where irrelevant.
        popen_kwargs["creationflags"] = 0x00000008 | 0x01000000 | 0x00000200
    else:
        popen_kwargs["start_new_session"] = True

    args = [sys.executable, "-m", "saltmdb.daemon.server"]
    logger.info(
        "Spawning daemon subprocess: args=%s platform=%s creationflags=%s parent_pid=%d",
        args,
        sys.platform,
        hex(popen_kwargs.get("creationflags", 0)) if sys.platform == "win32" else "n/a",
        os.getpid(),
    )
    try:
        try:
            proc = subprocess.Popen(  # nosec B603 -- fixed argv, shell=False, and environment contains only the DB path.
                args, **popen_kwargs
            )
            logger.info("Daemon subprocess spawned: child_pid=%d (primary flags)", proc.pid)
        except OSError as e:
            if sys.platform != "win32":
                raise
            # Some job objects explicitly disallow breakaway (no JOB_OBJECT_LIMIT_BREAKAWAY_OK /
            # SILENT_BREAKAWAY_OK) and CreateProcess then fails outright instead of silently
            # ignoring the flag. Retry without just CREATE_BREAKAWAY_FROM_JOB -- the daemon still
            # starts (just remains tied to the parent's job/process tree, same exposure as before
            # that fix), which beats never starting at all. DETACHED_PROCESS/CREATE_NEW_PROCESS_
            # GROUP are unrelated to job-breakaway policy and stay, so console isolation still
            # applies here.
            logger.warning(
                "Primary daemon spawn failed (%s); retrying without CREATE_BREAKAWAY_FROM_JOB "
                "-- daemon will remain tied to this process's Job Object if one exists", e,
            )
            popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
            proc = subprocess.Popen(  # nosec B603 -- see above.
                args, **popen_kwargs
            )
            logger.info(
                "Daemon subprocess spawned: child_pid=%d (fallback flags, still job-tied)", proc.pid
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
        logger.debug(
            "ensure_daemon_running: existing daemon reachable (pid=%s port=%s), no spawn needed",
            info.get("daemon_pid"),
            info.get("service_port"),
        )
        return info

    logger.info(
        "ensure_daemon_running: no reachable daemon for db_path=%s (caller_pid=%d); spawning",
        db_path,
        os.getpid(),
    )
    _spawn_daemon_subprocess(db_path)
    for attempt in range(DAEMON_DISCOVERY_RETRY_ATTEMPTS):
        time.sleep(DAEMON_DISCOVERY_RETRY_DELAY_S)
        info = discovery.read(key)
        if info and info.get("db_path") == db_path and _authenticated_ping_ok(info):
            logger.info(
                "ensure_daemon_running: daemon reachable after %d discovery attempt(s) "
                "(pid=%s port=%s)",
                attempt + 1,
                info.get("daemon_pid"),
                info.get("service_port"),
            )
            return info
        # Round-2 fix: periodically re-attempt a fresh spawn, not just re-poll, so progress
        # doesn't depend on winning a one-shot timing race against an unrelated shutdown.
        if attempt % DAEMON_RESPAWN_RETRY_INTERVAL == 0:
            logger.info(
                "ensure_daemon_running: still no reachable daemon at attempt %d; respawn retry",
                attempt,
            )
            _spawn_daemon_subprocess(db_path)
    logger.warning(
        "ensure_daemon_running: exhausted discovery-retry window for db_path=%s", db_path
    )
    raise DaemonStartupError(_classify_startup_failure(db_path, key))


def call_method(  # noqa: C901, PLR0912 -- retry/auth/session-lock state machine is intentionally centralized
    db_path: str,
    method: str,
    params: dict[str, Any],
    _retry: bool = True,
    *,
    _session: "SessionConnection | None" = None,
    _caller_agent_session_id: str | None = None,
    _caller_agent_session_capability: str | None = None,
) -> Any:
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
    # Adapter-bound calls hold the session state lock from refresh through metadata snapshot and
    # the complete one-shot RPC.  This prevents close() or a concurrent reconnect from replacing
    # the capability between validation and send.  Legacy direct call_method callers retain the
    # old best-effort current-session refresh behavior.
    if _session is not None and not hasattr(_session, "_state_lock"):
        _session._state_lock = threading.RLock()
    session_lock = _session._state_lock if _session is not None else nullcontext()
    with session_lock:
        if _session is not None:
            _session.ensure_fresh(db_path)
            if _caller_agent_session_id == _session._agent_session_id:
                # Always overwrite the envelope from the post-refresh snapshot.  A recursive
                # retry may have re-helloed and minted a new capability after the first request
                # was rejected; retaining the prior local argument would pair new auth with the
                # old capability.  If the daemon did not provide one, remove any stale field.
                _, current_capability = _session.session_metadata
                params = {**params}
                if current_capability is None:
                    params.pop("caller_agent_session_capability", None)
                else:
                    params["caller_agent_session_capability"] = current_capability
        elif _current_session is not None:
            _current_session.ensure_fresh(db_path)
        # Refresh discovery after ensure_fresh(): a reconnect may have replaced the daemon token
        # and service port.  Sending with the pre-refresh snapshot would use stale auth.
        info = ensure_daemon_running(db_path)

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
            return call_method(
                db_path,
                method,
                params,
                _retry=False,
                _session=_session,
                _caller_agent_session_id=_caller_agent_session_id,
                _caller_agent_session_capability=_caller_agent_session_capability,
            )
        except _MidCallFailure as e:
            raise DaemonRpcError("MID_CALL_FAILURE", str(e)) from e

        if not response.get("ok"):
            error = response.get("error") or {}
            code = error.get("code", protocol.INTERNAL_ERROR)
            if _retry and code in (
                protocol.DAEMON_SHUTTING_DOWN,
                protocol.AUTH_FAILED,
                protocol.CALLER_SESSION_INVALID,
            ):
                # Fix (2026-08-26 review finding): CALLER_SESSION_INVALID means the daemon has
                # already dropped our capability -- e.g. the persistent hello socket died
                # silently (network blip, or the daemon's own session loop erroring out and
                # unregistering us) while the daemon process itself kept running. The daemon's
                # auth_token never changed in that case, so ensure_fresh()'s cheap token
                # comparison can never detect it on its own, and the retry below would otherwise
                # resend the exact same stale capability forever. Force a real re-hello first.
                if _session is not None and code == protocol.CALLER_SESSION_INVALID:
                    _session.force_reconnect(db_path)
                return call_method(
                    db_path,
                    method,
                    params,
                    _retry=False,
                    _session=_session,
                    _caller_agent_session_id=_caller_agent_session_id,
                    _caller_agent_session_capability=_caller_agent_session_capability,
                )
            raise DaemonRpcError(code, error.get("message", ""))
        return response.get("result")


class _MidCallFailure(Exception):
    """Internal marker distinguishing a mid-send/mid-recv failure from a connect-phase one inside
    call_method()'s single try/except -- never escapes this module."""


def call(
    db_path: str,
    tool_name: str,
    kwargs: dict[str, Any],
    *,
    caller_agent_session_id: str | None = None,
    caller_agent_session_capability: str | None = None,
) -> Any:
    """Call one daemon tool, with optional adapter-only session metadata."""
    params: dict[str, Any] = {"tool": tool_name, "kwargs": kwargs}
    if caller_agent_session_id is not None:
        params["caller_agent_session_id"] = caller_agent_session_id
        if caller_agent_session_capability is not None:
            params["caller_agent_session_capability"] = caller_agent_session_capability
    session = None
    if _current_session is not None:
        # The logical ID is immutable for the adapter lifetime.  Passing the session into
        # call_method lets it acquire the lock before refreshing and deciding which capability to
        # attach; an explicitly mismatched caller ID is never silently rewritten.
        if caller_agent_session_id == _current_session._agent_session_id:
            session = _current_session
    return call_method(
        db_path,
        "tool_call",
        params,
        _session=session,
        _caller_agent_session_id=caller_agent_session_id,
        _caller_agent_session_capability=caller_agent_session_capability,
    )


class SessionConnection:
    """The long-lived, persistent-socket hello/goodbye connection -- NOT built on call_method().
    Opened once during server_lifespan's startup, held open for the adapter process's entire
    life, closed during lifespan shutdown."""

    def __init__(
        self,
        db_path: str,
        session_id: str | None = None,
        cwd: str | None = None,
        owner_id: str | None = None,
    ):
        self.db_path = discovery.resolve_canonical_db_path(db_path)
        self._sock: socket.socket | None = None
        self._auth_token: str | None = None
        self._agent_session_id = session_id
        self._cwd = cwd
        self._owner_id = owner_id
        self._session_capability: str | None = None
        self._state_lock = threading.RLock()

    @property
    def session_metadata(self) -> tuple[str | None, str | None]:
        """Return the current logical session ID and daemon-minted capability atomically."""
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.RLock()
        with self._state_lock:
            return self._agent_session_id, getattr(self, "_session_capability", None)

    def open(self) -> None:  # noqa: C901, PLR0912 -- bounded two-attempt hello state machine
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.RLock()
        if not hasattr(self, "_session_capability"):
            self._session_capability = None
        with self._state_lock:
            if self._sock is not None and self._auth_token is not None:
                return
            last_error: Exception | None = None
            for attempt in range(2):
                info = ensure_daemon_running(self.db_path)
                sock: socket.socket | None = None
                try:
                    sock = socket.create_connection(
                        ("127.0.0.1", info["service_port"]),
                        timeout=DAEMON_RPC_CONNECT_TIMEOUT_S,
                    )
                    sock.settimeout(DAEMON_RPC_CALL_TIMEOUT_S)
                    hello_params = {"pid": os.getpid(), "client_label": "saltmdb-adapter"}
                    if self._agent_session_id is not None:
                        hello_params["agent_session_id"] = self._agent_session_id
                    if self._cwd is not None:
                        hello_params["cwd"] = self._cwd
                    if self._owner_id is not None:
                        hello_params["owner_id"] = self._owner_id
                    protocol.send_frame(
                        sock,
                        protocol.build_request("hello", hello_params, token=info["auth_token"]),
                    )
                    response = protocol.recv_frame(sock)
                    if not response.get("ok"):
                        error = response.get("error") or {}
                        raise DaemonRpcError(
                            error.get("code", "HELLO_FAILED"),
                            error.get("message", "hello rejected"),
                        )
                    result = response.get("result") or {}
                    capability = result.get("caller_agent_session_capability")
                    if self._agent_session_id is not None and (
                        not isinstance(capability, str) or not capability
                    ):
                        raise DaemonRpcError(
                            protocol.INTERNAL_ERROR,
                            "daemon hello omitted caller session capability",
                        )
                except (OSError, protocol.FrameError, DaemonRpcError) as exc:
                    last_error = exc
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                    retryable = not isinstance(exc, DaemonRpcError) or exc.code in (
                        protocol.AUTH_FAILED,
                        protocol.DAEMON_SHUTTING_DOWN,
                    )
                    if attempt == 0 and retryable:
                        logger.info("Session hello failed; refreshing daemon discovery before retry")
                        continue
                    raise
                self._sock = sock
                self._auth_token = info["auth_token"]
                self._session_capability = capability if self._agent_session_id is not None else None
                global _current_session
                _current_session = self
                logger.info(
                    "Session opened: agent_session_id=%s adapter_pid=%d daemon_pid=%s "
                    "service_port=%s",
                    self._agent_session_id,
                    os.getpid(),
                    info.get("daemon_pid"),
                    info.get("service_port"),
                )
                return
            assert last_error is not None
            raise last_error

    def close(self, *, send_goodbye: bool = True) -> None:
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.RLock()
        if not hasattr(self, "_session_capability"):
            self._session_capability = None
        with self._state_lock:
            sock = self._sock
            logger.info(
                "Session closing: agent_session_id=%s adapter_pid=%d send_goodbye=%s",
                self._agent_session_id,
                os.getpid(),
                send_goodbye,
            )
            if sock is not None:
                if send_goodbye:
                    try:
                        protocol.send_frame(
                            sock, protocol.build_request("goodbye", {}, token=self._auth_token)
                        )
                        # The daemon persists ended_at (foreground, synchronous) before sending
                        # this acknowledgement, so reading it back is a genuine confirmation, not
                        # a courtesy. Fix (2026-08-26 review finding): close() previously sent
                        # goodbye and tore down the socket without ever attempting to read the
                        # response, so a server-side persistence failure -- reported back as an
                        # INTERNAL_ERROR ack -- was silently indistinguishable from success.
                        # Bounded by the socket's existing call timeout; deliberately not
                        # retried here, since this is exit-time cleanup, not a call worth
                        # blocking shutdown over.
                        ack = protocol.recv_frame(sock)
                        if not ack.get("ok"):
                            ack_error = ack.get("error") or {}
                            logger.warning(
                                "Daemon failed to durably close agent session %s: %s",
                                self._agent_session_id,
                                ack_error.get("message", ack_error.get("code", "unknown error")),
                            )
                    except (OSError, protocol.FrameError) as e:
                        logger.debug("Best-effort goodbye failed (daemon likely already gone): %s", e)
                try:
                    sock.close()
                except OSError:
                    pass
            self._sock = None
            self._auth_token = None
            self._session_capability = None
            global _current_session
            if _current_session is self:
                _current_session = None

    def ensure_fresh(self, db_path: str) -> None:
        """Cheap local comparison (re-reads the discovery file, no network) against the cached
        auth_token -- reconnects and re-hellos on mismatch (daemon restarted since our last hello,
        even if PID/port happen to coincide with the prior instance, round-2 fix)."""
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.RLock()
        if not hasattr(self, "_session_capability"):
            self._session_capability = None
        with self._state_lock:
            key = discovery.daemon_key(self.db_path)
            info = discovery.read(key)
            if info is None:
                # Missing discovery is a stale/starting daemon, not permission to use the old
                # socket.  Refreshing here also makes the next call retryable after a failed open.
                info = ensure_daemon_running(self.db_path)
            current_token = info.get("auth_token")
            if (
                self._sock is not None
                and current_token
                and self._auth_token
                and hmac.compare_digest(current_token, self._auth_token)
            ):
                return
            logger.info("Daemon restart detected for %s; reconnecting session.", self.db_path)
            # A daemon-token change is a transport reconnect, not the end of the logical agent
            # session.  Keep its ID/cwd/owner while discarding only stale transport state.
            self._close_transport_locked()
            try:
                self.open()
            except Exception:
                # Deliberately propagate: callers must not dispatch with stale authentication.
                # The logical identity remains intact, so the next call can retry the refresh.
                raise

    def force_reconnect(self, db_path: str) -> None:
        """Unconditionally close the transport and re-hello, bypassing ensure_fresh()'s cheap
        auth_token comparison entirely.

        Fix (2026-08-26 review finding): a raw disconnect of the persistent hello socket -- a
        network blip, or the daemon's own session loop erroring out on recv and unregistering us
        in its `finally` -- leaves the daemon process (and therefore its auth_token) completely
        unchanged, so ensure_fresh()'s token check can never observe it. The client would
        otherwise keep believing the session is fresh and resend a capability the daemon has
        already dropped, failing every subsequent tool_call with CALLER_SESSION_INVALID forever.
        call_method() calls this specifically on that error code to force a real re-hello and
        mint a fresh capability before retrying.
        """
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.RLock()
        with self._state_lock:
            logger.info(
                "Forcing session reconnect for %s after CALLER_SESSION_INVALID.", self.db_path
            )
            self._close_transport_locked()
            self.open()

    def _close_transport_locked(self) -> None:
        """Close only the socket/token, retaining the logical session for reconnect."""
        sock = self._sock
        self._sock = None
        self._auth_token = None
        self._session_capability = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _intermediary_main(argv: list[str] | None = None) -> None:
    """Entry point for `python -m saltmdb.daemon.client --spawn-detached <db_path>` -- the
    short-lived win32 intermediary launcher process spawned by _spawn_daemon_via_intermediary.
    This process has no logging handlers configured by default when run standalone, so it
    configures a minimal file logger onto the same daemon.log the daemon itself writes to,
    keeping the pid handoff traceable. Spawns the real daemon, then returns so __main__ can exit
    -- that exit is the entire point: it removes this process from the live process table before
    any later taskkill /T tree-walk could traverse through it to reach the daemon."""
    argv = sys.argv if argv is None else argv
    if len(argv) < 3 or argv[1] != "--spawn-detached":
        raise SystemExit("Usage: python -m saltmdb.daemon.client --spawn-detached <db_path>")
    db_path = argv[2]
    logging.basicConfig(
        filename=_daemon_log_path(db_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Intermediary launcher started: pid=%d ppid=%d -- spawning daemon then exiting "
        "immediately",
        os.getpid(),
        os.getppid(),
    )
    _spawn_daemon_process(db_path)
    logger.info("Intermediary launcher exiting: pid=%d", os.getpid())


if __name__ == "__main__":
    _intermediary_main()
