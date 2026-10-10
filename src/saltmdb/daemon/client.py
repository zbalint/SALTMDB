"""Adapter-side daemon client: ensure_daemon_running() (spawn-or-connect handshake, with periodic
re-spawn while draining), call_method()/call() (the short-lived per-call RPC primitive and its
tool_call convenience wrapper), and SessionConnection (the separate, persistent-socket hello/
goodbye connection -- never built on call_method()).

See scratch/plans/track_b_daemon_detailed.md §4/§5/§12/§13 for the full design and 5-round review
trail.
"""

import contextvars
import hmac
import logging
import os
import socket
import subprocess  # nosec B404 -- daemon spawn uses a fixed module argv and never shell input.
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from typing import Any

from saltmdb.config import (
    DAEMON_DISCOVERY_RETRY_ATTEMPTS,
    DAEMON_DISCOVERY_RETRY_DELAY_S,
    DAEMON_LAZY_HELLO_BUDGET_S,
    DAEMON_LAZY_OPEN_CAP_S,
    DAEMON_MALLOC_ARENA_MAX,
    DAEMON_OWNER_PROBE_INTERVAL_S,
    DAEMON_OWNER_PROBE_MISSES,
    DAEMON_RESPAWN_RETRY_INTERVAL,
    DAEMON_RPC_CALL_TIMEOUT_S,
    DAEMON_RPC_CONNECT_TIMEOUT_S,
    DAEMON_SPAWN_MIN_INTERVAL_S,
    DAEMON_STARTUP_PROGRESS_CAP_S,
)
from saltmdb.daemon import discovery, protocol

logger = logging.getLogger(__name__)


class DaemonStartupError(Exception):
    """Raised when ensure_daemon_running() cannot establish a daemon connection."""


class DaemonStartingError(DaemonStartupError):
    """Raised when a matching daemon owner is alive but has not become reachable yet."""


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
_startup_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "startup_deadline", default=None
)
_spawn_lock = threading.Lock()
_last_spawn_at: float | None = None


@contextmanager
def startup_budget(seconds: float | None = None):
    """Share one monotonic startup deadline across nested adapter operations."""
    if _startup_deadline.get() is not None:
        yield
        return
    budget = DAEMON_STARTUP_PROGRESS_CAP_S if seconds is None else seconds
    token = _startup_deadline.set(time.monotonic() + budget)
    try:
        yield
    finally:
        _startup_deadline.reset(token)


def get_current_session() -> "SessionConnection | None":
    """Public accessor for the process's one SessionConnection, which may be adopted before its
    persistent hello connection is established."""
    return _current_session


def adopt_current_session(session: "SessionConnection") -> None:
    """Publish a lazy-start session before its background hello completes."""
    global _current_session
    _current_session = session


def _claim_spawn_slot(db_path: str) -> bool:
    """Claim the cross-process spawn slot for this database, failing open on stamp errors."""
    try:
        import fcntl
    except ImportError as exc:
        # shortcut: no cross-process throttle on win32 (no fcntl); use msvcrt.locking if Windows
        # spawn storms matter.
        logger.warning("Spawn stamp locking unavailable; allowing spawn: %s", exc)
        return True

    fd = -1
    try:
        canonical_db_path = discovery.resolve_canonical_db_path(db_path)
        key = discovery.daemon_key(canonical_db_path)
        stamp_path = discovery.spawn_stamp_path(key)
        fd = os.open(stamp_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        _ = os.lseek(fd, 0, os.SEEK_SET)
        raw_stamp = os.read(fd, 128)
        try:
            previous = float(raw_stamp.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            previous = None
        now = time.time()
        if previous is not None and 0 <= now - previous < DAEMON_SPAWN_MIN_INTERVAL_S:
            return False
        payload = repr(now).encode("utf-8")
        os.ftruncate(fd, 0)
        _ = os.lseek(fd, 0, os.SEEK_SET)
        _ = os.write(fd, payload)
        return True
    except OSError as exc:
        logger.warning("Spawn stamp operation failed; allowing spawn: %s", exc)
        return True
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as exc:
                logger.warning("Spawn stamp descriptor close failed: %s", exc)


def _spawn_if_due(db_path: str) -> bool:
    """Spawn at most once per configured interval in this adapter process."""
    global _last_spawn_at
    now = time.monotonic()
    with _spawn_lock:
        if _last_spawn_at is not None and now - _last_spawn_at < DAEMON_SPAWN_MIN_INTERVAL_S:
            return False
        if not _claim_spawn_slot(db_path):
            return False
        _last_spawn_at = now
        _spawn_daemon_subprocess(db_path)
        return True


def begin_lazy_start(session: "SessionConnection") -> None:
    """Publish a session and start daemon readiness/hello without blocking adapter startup."""
    adopt_current_session(session)

    def _wait_and_open() -> None:
        started_at = time.monotonic()
        if probe_owner(session.db_path) is None:
            _ = _spawn_if_due(session.db_path)
        deadline = started_at + DAEMON_LAZY_OPEN_CAP_S
        startup_logged = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "Lazy daemon startup exceeded %.0f seconds for %s",
                    DAEMON_LAZY_OPEN_CAP_S,
                    session.db_path,
                )
                return
            try:
                ensure_daemon_running(session.db_path, cap_s=remaining)
            except DaemonStartupError as exc:
                if not startup_logged:
                    logger.warning("Lazy daemon startup still pending: %s", exc)
                    startup_logged = True
                time.sleep(min(DAEMON_DISCOVERY_RETRY_DELAY_S, remaining))
                continue

            if get_current_session() is not session:
                return
            try:
                with startup_budget(DAEMON_LAZY_HELLO_BUDGET_S):
                    session.open()
            except (DaemonStartupError, DaemonRpcError, OSError) as exc:
                logger.warning("Lazy session hello failed: %s", exc)
            return

    # shortcut: a close racing this daemon thread can reopen once; add a stop flag if this
    # process ever needs to keep the thread alive after the lifespan exits.
    thread = threading.Thread(
        target=_wait_and_open,
        name="saltmdb-lazy-session-open",
        daemon=True,
    )
    thread.start()


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
    CREATE_NEW_PROCESS_GROUP/CREATE_NO_WINDOW flags as the daemon, for the same console/job
    reasons -- it just also needs to survive long enough to finish spawning its own child."""
    env = dict(os.environ)
    env["SALTMDB_DB_PATH"] = db_path
    args = [sys.executable, "-m", "saltmdb.daemon.client", "--spawn-detached", db_path]
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        # DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP | CREATE_NO_
        # WINDOW -- same combo and same rationale as _spawn_daemon_process's win32 branch below.
        "creationflags": 0x00000008 | 0x01000000 | 0x00000200 | 0x08000000,
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


def _spawn_daemon_process(db_path: str, *, persistent: bool = False) -> None:
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
    env.setdefault("MALLOC_ARENA_MAX", DAEMON_MALLOC_ARENA_MAX)
    env["SALTMDB_DB_PATH"] = db_path
    env["SALTMDB_DAEMON_SPAWNED_AT"] = repr(time.time())
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
        #    to be attached to, which is the only thing that stops CTRL+CLOSE delivery.
        #  - CREATE_NEW_PROCESS_GROUP: kept as defense-in-depth against CTRL_C/CTRL_BREAK
        #    specifically, in case the daemon process ever ends up console-attached again (e.g.
        #    a future AllocConsole/AttachConsole call); harmless, ignored where irrelevant.
        #  - CREATE_NO_WINDOW: previously dropped here on the theory that it's a no-op once
        #    DETACHED_PROCESS is set (Microsoft's docs say DETACHED_PROCESS alone means the child
        #    has no console to pop a window for in the first place). Live evidence on a Win11
        #    machine with Windows Terminal as the default terminal app disproved that in practice
        #    (SALTMDB memory: Win11/Copilot visible-terminal-window bug, 2026-08-27) -- a visible
        #    console window still appeared. Root cause on that machine was actually one level
        #    deeper (a `py.exe` launcher-stub venv silently relaunching an uncontrolled grandchild
        #    our flags never reached), but CREATE_NO_WINDOW is cheap, harmless in combination with
        #    DETACHED_PROCESS, and hardens this spawn against that whole class of environment, so
        #    it's restored as defense-in-depth even though it isn't the primary fix for that bug.
        popen_kwargs["creationflags"] = 0x00000008 | 0x01000000 | 0x00000200 | 0x08000000
    else:
        popen_kwargs["start_new_session"] = True

    args = [sys.executable, "-m", "saltmdb.daemon.server"]
    if persistent:
        args.append("--foreground")
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
                "-- daemon will remain tied to this process's Job Object if one exists",
                e,
            )
            popen_kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
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


def reachable_daemon_info(db_path: str) -> dict[str, Any] | None:
    """Return authenticated discovery information for this canonical database, if reachable."""
    canonical_db_path = discovery.resolve_canonical_db_path(db_path)
    key = discovery.daemon_key(canonical_db_path)
    info = discovery.read(key)
    if info and info.get("db_path") == canonical_db_path and _authenticated_ping_ok(info):
        return info
    return None


def probe_owner(db_path: str) -> str | None:
    """Return a matching daemon's startup state from the lightweight identify probe."""
    canonical_db_path = discovery.resolve_canonical_db_path(db_path)
    key = discovery.daemon_key(canonical_db_path)
    info = _identify_probe(canonical_db_path, key)
    if (
        isinstance(info, dict)
        and info.get("db_path") == canonical_db_path
        and info.get("state") in {"initializing", "ready"}
    ):
        return info["state"]
    return None


def _startup_failure(
    db_path: str, key: str, started_at: float, owner_alive: bool
) -> DaemonStartupError:
    if owner_alive:
        elapsed = time.monotonic() - started_at
        return DaemonStartingError(
            f"the SALTMDB daemon is still starting (initializing after {elapsed:.0f} s)"
        )
    return DaemonStartupError(_classify_startup_failure(db_path, key))


def _startup_deadline_for(started_at: float, cap_s: float | None) -> float:
    if cap_s is not None:
        return started_at + cap_s
    inherited_deadline = _startup_deadline.get()
    if inherited_deadline is not None:
        return inherited_deadline
    return started_at + DAEMON_STARTUP_PROGRESS_CAP_S


def _update_owner_probe(
    owner: str | None,
    owner_alive: bool,
    owner_misses: int,
    legacy_attempts: int,
) -> tuple[bool, int, int]:
    if owner is not None:
        return True, 0, 0
    if not owner_alive:
        return owner_alive, owner_misses, legacy_attempts
    owner_misses += 1
    if owner_misses >= DAEMON_OWNER_PROBE_MISSES:
        owner_alive = False
    return owner_alive, owner_misses, legacy_attempts


def ensure_daemon_running(db_path: str, *, cap_s: float | None = None) -> dict[str, Any]:
    """Connect to a reachable daemon or wait for one with owner-aware progress detection."""
    db_path = discovery.resolve_canonical_db_path(db_path)
    key = discovery.daemon_key(db_path)
    info = reachable_daemon_info(db_path)
    if info is not None:
        logger.debug(
            "ensure_daemon_running: existing daemon reachable (pid=%s port=%s), no spawn needed",
            info.get("daemon_pid"),
            info.get("service_port"),
        )
        return info

    started_at = time.monotonic()
    deadline = _startup_deadline_for(started_at, cap_s)

    owner = probe_owner(db_path)
    if owner is None:
        _spawn_if_due(db_path)
    owner_alive = True
    owner_misses = 0
    legacy_attempts = 0
    last_probe_at = started_at
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _startup_failure(db_path, key, started_at, owner_alive)
        time.sleep(min(DAEMON_DISCOVERY_RETRY_DELAY_S, remaining))
        now = time.monotonic()

        info = reachable_daemon_info(db_path)
        if info is not None:
            logger.info(
                "ensure_daemon_running: daemon reachable after %.2f s (pid=%s port=%s)",
                now - started_at,
                info.get("daemon_pid"),
                info.get("service_port"),
            )
            return info

        if now - last_probe_at >= DAEMON_OWNER_PROBE_INTERVAL_S:
            owner = probe_owner(db_path)
            last_probe_at = now
            owner_alive, owner_misses, legacy_attempts = _update_owner_probe(
                owner, owner_alive, owner_misses, legacy_attempts
            )

        if owner_alive:
            continue

        legacy_attempts += 1
        if legacy_attempts % DAEMON_RESPAWN_RETRY_INTERVAL == 0:
            logger.info(
                "ensure_daemon_running: still no owner at legacy attempt %d; respawn retry",
                legacy_attempts,
            )
            _spawn_if_due(db_path)
        if legacy_attempts >= DAEMON_DISCOVERY_RETRY_ATTEMPTS:
            raise _startup_failure(db_path, key, started_at, False)


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
    with startup_budget():
        params: dict[str, Any] = {"tool": tool_name, "kwargs": kwargs}
        if caller_agent_session_id is not None:
            params["caller_agent_session_id"] = caller_agent_session_id
            if caller_agent_session_capability is not None:
                params["caller_agent_session_capability"] = caller_agent_session_capability
        session = None
        if _current_session is not None:
            # The logical ID is immutable for the adapter lifetime.  Passing the session into
            # call_method lets it acquire the lock before refreshing and deciding which capability
            # to attach; an explicitly mismatched caller ID is never silently rewritten.
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
        agent_id: str | None = None,
    ):
        self.db_path = discovery.resolve_canonical_db_path(db_path)
        self._sock: socket.socket | None = None
        self._auth_token: str | None = None
        self._agent_session_id = session_id
        self._cwd = cwd
        self._agent_id = agent_id
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
                    if self._agent_id is not None:
                        hello_params["agent_id"] = self._agent_id
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
                        logger.info(
                            "Session hello failed; refreshing daemon discovery before retry"
                        )
                        continue
                    raise
                self._sock = sock
                self._auth_token = info["auth_token"]
                self._session_capability = (
                    capability if self._agent_session_id is not None else None
                )
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
            if last_error is None:
                raise RuntimeError("Session hello ended without a connection or error")
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
                        logger.debug(
                            "Best-effort goodbye failed (daemon likely already gone): %s", e
                        )
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
        "Intermediary launcher started: pid=%d ppid=%d -- spawning daemon then exiting immediately",
        os.getpid(),
        os.getppid(),
    )
    _spawn_daemon_process(db_path)
    logger.info("Intermediary launcher exiting: pid=%d", os.getpid())


if __name__ == "__main__":
    _intermediary_main()
