"""The Track B backend daemon: the sole process that opens SQLite for a given database.

See scratch/plans/track_b_daemon_detailed.md §3/§6/§7 for the full design and 5-round Codex review
trail this implementation follows. `main()` here is the `saltmdb-daemon` console-script entrypoint
(explicit foreground launch, `--foreground`) and also what a spawned background daemon subprocess
runs (daemon/client.py's ensure_daemon_running()).
"""

import argparse
import hmac
import logging
import os
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from saltmdb.domain.services.embedding_service import EmbedJobScheduler

from saltmdb import config
from saltmdb.daemon import discovery, platform_paths, protocol
from saltmdb.daemon.dispatch import DISPATCH_TABLE, dispatch_tool
from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.db_write_coordinator import CoordinatorUsageError
from saltmdb.daemon.embed_stall_monitor import EmbedStallMonitor
from saltmdb.db.schema import init_db

logger = logging.getLogger(__name__)


@dataclass
class _AgentSession:
    agent_session_id: str
    capability: str
    closing: bool = False
    leases: int = 0


class _DaemonState:
    """Shared, thread-safe daemon state: session bookkeeping, the grace-period shutdown timer,
    the draining flag, and RPC request dispatch. One instance per daemon process."""

    def __init__(self, db_path: str, key: str, foreground: bool):
        self.db_path = db_path
        self.daemon_key = key
        self.foreground = foreground
        self.auth_token: str | None = None
        self.service_port: int | None = None
        self.viewer_port: int | None = None  # set by main() after the Viewer thread decision; None
        # means the Viewer is disabled for this daemon -- viewer_status() reports it authoritatively.

        self._lock = threading.RLock()
        self._leases_changed = threading.Condition(self._lock)
        self._sessions: set[int] = set()
        self._agent_sessions: dict[int, _AgentSession] = {}
        self._inflight = 0  # one-shot RPC dispatches currently in progress (not hello sessions --
        # see _acquire_inflight/_release_inflight); the grace timer must not fire while this is
        # nonzero, closing the "long backfill/librarian call killed by the 30s grace timer" gap.
        self._draining = False
        self._shutdown_timer: threading.Timer | None = None
        self._shutdown_callback = None
        self._started_at = time.monotonic()
        self.coordinator: DbWriteCoordinator | None = None
        self.embedding_scheduler: "EmbedJobScheduler | None" = None

    def viewer_snapshot(self) -> dict[str, Any]:
        """Return the daemon-owned fields safe to expose to the local Viewer."""
        with self._lock:
            return {
                "ready": self.service_port is not None and not self._draining,
                "uptime_s": round(time.monotonic() - self._started_at, 3),
                "viewer": {"enabled": self.viewer_port is not None, "port": self.viewer_port},
                "active_hello_sessions": len(self._sessions),
                # Fix (2026-08-26 review finding): a session already fenced by begin_goodbye()
                # (record.closing) is mid-shutdown, not "active" -- excluding it keeps the
                # Viewer's liveness overlay from showing a session as active for however long its
                # goodbye takes to drain (bounded by DAEMON_GOODBYE_LEASE_DRAIN_TIMEOUT_S above).
                "active_agent_session_ids": [
                    record.agent_session_id
                    for record in self._agent_sessions.values()
                    if not record.closing
                ],
                "inflight_rpc_dispatches": self._inflight,
                "db_writer": self.coordinator.telemetry() if self.coordinator else None,
            }

    def set_shutdown_callback(self, callback) -> None:
        self._shutdown_callback = callback

    def start_initial_grace_period(self) -> None:
        """Implementation-grounding addendum (self-caught, not from a Codex review round): a
        freshly-spawned background daemon starts with ZERO sessions -- if the grace-period timer
        only ever started on a session-count transition TO zero (unregister_session -> 0), a
        daemon that never receives any "hello" at all (e.g. spawned only to service a one-shot
        cli.py call or a --librarian/--backfill-chunk-embeddings RPC client, neither of which opens
        a SessionConnection) would run forever, since no such transition would ever occur. The
        grace period must also begin at startup itself, treated identically to "just transitioned
        to zero sessions" -- handle_request()'s atomic hello-admission already cancels the timer
        on arrival if a real session shows up before this fires."""
        if not self.foreground:
            self._start_grace_timer()

    def unregister_session(self, session_id: int) -> None:
        with self._leases_changed:
            self._sessions.discard(session_id)
            self._agent_sessions.pop(session_id, None)
            self._leases_changed.notify_all()
            if (
                not self._sessions
                and not self._inflight
                and not self._draining
                and not self.foreground
            ):
                self._start_grace_timer()

    def close_agent_session(self, session_id: int) -> None:
        """Persist a definitive normal goodbye before removing its live mapping."""
        with self._lock:
            record = self._agent_sessions.get(session_id)
            agent_session_id = record.agent_session_id if record else None
        if agent_session_id and self.coordinator:
            import datetime
            from saltmdb.db import agent_sessions

            self.coordinator.submit(
                "close_agent_session",
                lambda conn: agent_sessions.close_session(
                    conn, agent_session_id, datetime.datetime.now(datetime.timezone.utc).isoformat()
                ),
                priority="foreground",
            )

    def begin_goodbye(self, session_id: int) -> None:
        """Fence a hello session and wait for every already-accepted call to finish.

        Bounded (fix for a 2026-08-26 review finding): an unbounded ``Condition.wait()`` here
        used to hang this thread -- and the client-facing goodbye response with it -- forever if
        a lease was ever left unreleased (a hung ``dispatch_tool`` call, or a future bug that
        skips ``_release_caller_lease``'s ``finally``). Give up after
        ``DAEMON_GOODBYE_LEASE_DRAIN_TIMEOUT_S`` and log a warning instead: the session is already
        marked ``closing`` (further calls are rejected regardless), so proceeding just means the
        stuck lease's tool_call is no longer waited on before goodbye completes.
        """
        with self._leases_changed:
            record = self._agent_sessions.get(session_id)
            if record is None:
                return
            record.closing = True
            deadline = time.monotonic() + config.DAEMON_GOODBYE_LEASE_DRAIN_TIMEOUT_S
            while record.leases:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Goodbye for agent session %s proceeding with %d lease(s) still held "
                        "after %.0fs; a tool_call dispatch may be stuck.",
                        record.agent_session_id,
                        record.leases,
                        config.DAEMON_GOODBYE_LEASE_DRAIN_TIMEOUT_S,
                    )
                    return
                self._leases_changed.wait(timeout=remaining)

    def touch_agent_session(self, session_id: str, received_at: str) -> None:
        """Queue a best-effort receipt-time activity update through the sole DB writer.

        Activity is intentionally non-blocking: a tool call must not wait for this low-value
        telemetry row.  The coordinator still serializes it with every other mutation, and its
        monotonic SQL predicate makes delayed jobs harmless.  Retaining a callback on the future
        makes failures visible in the daemon log without changing the tool response.
        """
        if self.coordinator is None:
            return
        from saltmdb.db import agent_sessions

        try:
            future = self.coordinator.submit(
                "touch_agent_session",
                lambda conn: agent_sessions.touch_session(conn, session_id, received_at),
                priority="background",
                wait=False,
            )
        except Exception:
            logger.warning("Could not submit agent-session activity update", exc_info=True)
            return
        add_done_callback = getattr(future, "add_done_callback", None)
        if add_done_callback is not None:
            add_done_callback(self._log_activity_touch_failure)

    @staticmethod
    def _log_activity_touch_failure(future) -> None:
        try:
            future.result()
        except Exception:
            logger.warning("Agent-session activity update failed", exc_info=True)

    def _start_grace_timer(self) -> None:
        timer = threading.Timer(config.DAEMON_SHUTDOWN_GRACE_PERIOD_S, self._grace_fire)
        timer.daemon = True
        self._shutdown_timer = timer
        timer.start()

    def _grace_fire(self) -> None:
        with self._lock:
            if self._sessions or self._inflight:  # re-check under the same lock -- closes the
                # hello-arrives/RPC-in-flight-right-as-timer-fires races. Don't shut down, but the
                # timer is one-shot and has already fired -- clear it so _release_inflight/
                # unregister_session know to re-arm once things actually go quiet.
                self._shutdown_timer = None
                return
            self._draining = True
        logger.info("Grace period elapsed with zero sessions; shutting down.")
        if self._shutdown_callback:
            self._shutdown_callback()

    def _acquire_inflight(self) -> bool:
        """Returns False (without incrementing) if the daemon is already draining. Codex round-2
        finding: this must be atomic with _grace_fire()'s own transition to draining, under the
        SAME lock -- otherwise a request that passed the top-of-handle_request draining fast-check
        can still reach dispatch after _grace_fire() has already committed to shutdown in the gap
        between that check and this call, and get killed mid-flight by _shutdown_sequence()'s
        os._exit(0). Mirrors the hello-admission fix's shape exactly."""
        with self._lock:
            if self._draining:
                return False
            self._inflight += 1
            if self._shutdown_timer is not None:
                self._shutdown_timer.cancel()
                self._shutdown_timer = None
            return True

    def _release_inflight(self) -> None:
        with self._lock:
            self._inflight -= 1
            if (
                not self._sessions
                and not self._inflight
                and not self._draining
                and not self.foreground
            ):
                self._start_grace_timer()

    def begin_draining(self) -> None:
        with self._lock:
            self._draining = True

    def identify_response(self) -> dict[str, Any]:
        if self.service_port is None:
            return {"state": "initializing", "daemon_key": self.daemon_key, "db_path": self.db_path}
        return {
            "state": "ready",
            "daemon_key": self.daemon_key,
            "db_path": self.db_path,
            "service_port": self.service_port,
        }

    def handle_request(  # noqa: PLR0911
        self, request: dict[str, Any], session_id: int | None = None
    ) -> dict[str, Any]:
        request_id: str | None = request.get("id")
        if self._draining:
            return protocol.build_error_response(
                request_id, protocol.DAEMON_SHUTTING_DOWN, "daemon is shutting down"
            )
        token = request.get("token")
        if not (token and self.auth_token and hmac.compare_digest(token, self.auth_token)):
            return protocol.build_error_response(
                request_id, protocol.AUTH_FAILED, "invalid or missing token"
            )

        method = request.get("method")
        params = request.get("params")
        if params is None:
            params = {}  # absent or explicitly null -- the only values that mean "no params"
        elif not isinstance(params, dict):
            # Codex round-2 finding: `request.get("params") or {}` previously let ANY falsy
            # wrong-type value ([], "", 0, False) silently pass through as {} without ever
            # reaching this check, since `or` short-circuits before isinstance() even runs.
            return protocol.build_error_response(
                request_id, protocol.MALFORMED_REQUEST, "params must be an object"
            )

        if method in ("hello", "goodbye", "ping", "viewer_status"):
            return self._handle_session_method(method, request_id, session_id, params)

        if not self._acquire_inflight():
            return protocol.build_error_response(
                request_id, protocol.DAEMON_SHUTTING_DOWN, "daemon is shutting down"
            )
        try:
            if method == "tool_call":
                return self._handle_tool_call(request_id, params, session_id)
            elif method == "run_librarian_now":
                return self._handle_run_librarian(request_id, params)
            elif method == "run_backfill_chunk_embeddings_now":
                return self._handle_run_backfill(request_id)
            else:
                return protocol.build_error_response(
                    request_id, protocol.UNKNOWN_METHOD, f"unknown method: {method}"
                )
        except Exception as e:
            logger.exception("Error handling RPC method %s", method)
            return protocol.build_error_response(request_id, protocol.INTERNAL_ERROR, str(e))
        finally:
            self._release_inflight()

    def _handle_session_method(
        self, method: str, request_id: str | None, session_id: int | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle session-lifecycle (hello/goodbye/ping) and status methods.
        These never acquire the inflight counter -- they are exempt from the shutdown gate."""
        if method == "hello" and session_id is not None:
            # Atomic with _grace_fire's own lock: closes the exact race where a hello is
            # acknowledged "ok" to the caller in the gap before the session is actually
            # registered, during which the grace timer could observe zero sessions and start
            # draining a connection the caller just believes it successfully opened.
            with self._lock:
                if self._draining:
                    return protocol.build_error_response(
                        request_id, protocol.DAEMON_SHUTTING_DOWN, "daemon is shutting down"
                    )
                self._sessions.add(session_id)
                if self._shutdown_timer is not None:
                    self._shutdown_timer.cancel()
                    self._shutdown_timer = None
            # Registration is a foreground write: once hello succeeds, its provenance row exists.
            capability: str | None = None
            if params.get("agent_session_id") and self.coordinator is not None:
                try:
                    from saltmdb.db import agent_sessions
                    import datetime

                    cwd = params.get("cwd") or None

                    self.coordinator.submit(
                        "record_agent_session",
                        lambda conn: agent_sessions.record_session(
                            conn,
                            params["agent_session_id"],
                            cwd,
                            datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            params.get("owner_id"),
                        ),
                        priority="foreground",
                    )
                    capability = secrets.token_urlsafe(32)
                    with self._lock:
                        self._agent_sessions[session_id] = _AgentSession(
                            params["agent_session_id"], capability
                        )
                except Exception as exc:
                    self.unregister_session(session_id)
                    logger.exception("Failed to record agent session")
                    code = (
                        protocol.DAEMON_SHUTTING_DOWN
                        if str(exc) == protocol.DAEMON_SHUTTING_DOWN
                        or (
                            isinstance(exc, CoordinatorUsageError)
                            and getattr(exc, "code", None) == protocol.DAEMON_SHUTTING_DOWN
                        )
                        else protocol.INTERNAL_ERROR
                    )
                    return protocol.build_error_response(
                        request_id,
                        code,
                        f"failed to record agent session: {exc}",
                    )
            elif params.get("agent_session_id"):
                self.unregister_session(session_id)
                return protocol.build_error_response(
                    request_id, protocol.INTERNAL_ERROR, "database writer unavailable"
                )
            result: dict[str, Any] = {"status": "ok"}
            if capability is not None:
                result["caller_agent_session_capability"] = capability
            return protocol.build_ok_response(request_id, result)
        if method == "goodbye" and session_id is not None:
            # Marking closing is done before the session-loop persists ended_at and sends the
            # acknowledgement, so no late call can acquire a lease during that window.
            self.begin_goodbye(session_id)
        if method == "viewer_status":
            snapshot = self.viewer_snapshot()
            snapshot.update({"enabled": self.viewer_port is not None, "port": self.viewer_port})
            return protocol.build_ok_response(request_id, snapshot)
        return protocol.build_ok_response(request_id, {"status": "ok"})

    def _validate_caller_session(  # noqa: PLR0911 -- explicit malformed/inactive outcomes
        self,
        request_id: str | None,
        params: dict[str, Any],
        transport_session_id: int | None,  # noqa: ARG002 -- kept for call-site stability; see note below
    ) -> tuple[_AgentSession | None, dict[str, Any] | None]:  # noqa: PLR0911
        # Fix (2026-08-26 second-pass review finding): a `transport_session_id in self._sessions`
        # branch used to sit here, meant to require caller metadata "for an active adapter
        # session". It was dead code: `self._sessions` only ever gains an entry for the socket
        # that carried a `hello` (see `_handle_session_method`), while every `tool_call` arrives
        # on a brand-new one-shot socket opened by daemon/client.py's `call_method()` -- it never
        # reuses the persistent hello socket -- so `transport_session_id` (that one-shot socket's
        # own id()) could never equal a live hello session's id. The branch never fired for any
        # real caller, and even where a test forced it to fire (by reusing a hello's session_id
        # for a later `handle_request` call), the very next check below already produces the same
        # MALFORMED_REQUEST outcome whenever caller metadata is present-but-invalid. Real
        # enforcement is exactly what remains: caller metadata is optional (metadata-free
        # one-shot CLI calls stay allowed), but when supplied it must be a well-formed pair whose
        # capability actually matches a live, non-closing `_agent_sessions` record (checked below).
        caller_field_present = "caller_agent_session_id" in params
        capability_field_present = "caller_agent_session_capability" in params
        caller_session_id = params.get("caller_agent_session_id")
        capability = params.get("caller_agent_session_capability")
        if caller_field_present != capability_field_present:
            return None, protocol.build_error_response(
                request_id,
                protocol.MALFORMED_REQUEST,
                "caller_agent_session_id and caller_agent_session_capability must be supplied together",
            )
        if caller_field_present and (
            not isinstance(caller_session_id, str)
            or not caller_session_id
            or not isinstance(capability, str)
            or not capability
        ):
            return None, protocol.build_error_response(
                request_id,
                protocol.MALFORMED_REQUEST,
                "caller session metadata must be non-empty strings",
            )
        if caller_session_id is not None:
            with self._leases_changed:
                record = next(
                    (
                        candidate
                        for candidate in self._agent_sessions.values()
                        if candidate.agent_session_id == caller_session_id
                        and hmac.compare_digest(candidate.capability, capability)
                    ),
                    None,
                )
                if record is None or record.closing:
                    return None, protocol.build_error_response(
                        request_id,
                        protocol.CALLER_SESSION_INVALID,
                        "caller session is inactive, mismatched, or closing",
                    )
                record.leases += 1
                return record, None
        return None, None

    def _release_caller_lease(self, record: _AgentSession | None) -> None:
        if record is None:
            return
        with self._leases_changed:
            record.leases = max(0, record.leases - 1)
            self._leases_changed.notify_all()

    def _handle_tool_call(
        self,
        request_id: str | None,
        params: dict[str, Any],
        transport_session_id: int | None = None,
    ) -> dict[str, Any]:
        """Dispatch a tool_call RPC method."""
        tool = params.get("tool")
        if tool not in DISPATCH_TABLE:
            return protocol.build_error_response(
                request_id, protocol.UNKNOWN_TOOL, f"unknown tool: {tool}"
            )
        if self.coordinator is None:
            return protocol.build_error_response(
                request_id, protocol.INTERNAL_ERROR, "database writer unavailable"
            )
        kwargs = params.get("kwargs") or {}
        if not isinstance(kwargs, dict):
            return protocol.build_error_response(
                request_id, protocol.MALFORMED_REQUEST, "tool kwargs must be an object"
            )
        caller_record, validation_error = self._validate_caller_session(
            request_id, params, transport_session_id
        )
        if validation_error is not None:
            return validation_error
        if caller_record is not None:
            import datetime

            received_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.touch_agent_session(caller_record.agent_session_id, received_at)
        try:
            result = dispatch_tool(tool, kwargs, self.coordinator)
            return protocol.build_ok_response(request_id, result)
        finally:
            self._release_caller_lease(caller_record)

    def _handle_run_librarian(
        self, request_id: str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Run an on-demand librarian maintenance pass."""
        from saltmdb.domain.services import librarian_service

        if self.coordinator is None:
            raise RuntimeError("database writer unavailable")
        result = librarian_service.run_librarian_now(
            db_path=self.db_path, force=params.get("force", True), coordinator=self.coordinator
        )
        return protocol.build_ok_response(request_id, result)

    def _handle_run_backfill(self, request_id: str | None) -> dict[str, Any]:
        """Trigger a full chunk-embedding backfill pass over all active entities."""
        from saltmdb.domain.services.embedding_service import reconcile_embedding_jobs

        if self.coordinator is None:
            raise RuntimeError("database writer unavailable")
        after_id: str | None = None
        total = 0
        while True:
            # submit() with wait=True (default) always returns T directly, never Future[T].
            def _reconcile(conn, cursor=after_id) -> list[str]:
                return reconcile_embedding_jobs(conn, limit=100, after_id=cursor)

            ids = cast(
                list[str],
                self.coordinator.submit(
                    "run_backfill_chunk_embeddings_now",
                    _reconcile,
                    priority="background",
                ),
            )
            if not ids:
                break
            total += len(ids)
            after_id = ids[-1]
        return protocol.build_ok_response(
            request_id, f"{total} entities queued for durable embedding."
        )


class _ThreadingRpcServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False  # service port is OS-assigned fresh each start; no reuse needed
    daemon_state: _DaemonState


class _RpcRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        state: _DaemonState = self.server.daemon_state  # type: ignore[attr-defined]
        session_id = id(sock)  # computed upfront so a hello can be admitted atomically inside
        # handle_request() itself -- see its own comment on the hello-vs-grace-timer race.
        try:
            request = protocol.recv_frame(sock)
        except (OSError, protocol.FrameError):
            return
        response = state.handle_request(request, session_id=session_id)
        try:
            protocol.send_frame(sock, response)
        except (OSError, protocol.FrameError):
            if request.get("method") == "hello" and response.get("ok"):
                state.unregister_session(session_id)
            return
        if request.get("method") == "hello" and response.get("ok"):
            self._session_loop(sock, state, session_id)

    def _session_loop(self, sock: socket.socket, state: _DaemonState, session_id: int) -> None:
        """A hello connection stays open for the adapter process's entire life -- this loop
        blocks on recv (potentially for a very long time) waiting for exactly one more frame:
        "goodbye", or the socket closing/erroring, which is treated identically (§5). The session
        was already registered atomically inside handle_request()'s own hello handling -- this
        loop only ever needs to unregister it, in `finally`, on the way out."""
        try:
            while True:
                try:
                    request = protocol.recv_frame(sock)
                except (OSError, protocol.FrameError):
                    return
                response = state.handle_request(request, session_id=session_id)
                if request.get("method") == "goodbye" and response.get("ok"):
                    # Persist the definitive lifecycle transition before acknowledging goodbye.
                    # A client is allowed to exit immediately after receiving this response, so
                    # sending it first would make the supposedly synchronous close observable as
                    # merely best effort.
                    try:
                        state.close_agent_session(session_id)
                    except Exception as exc:
                        logger.exception("Failed to close agent session on goodbye")
                        response = protocol.build_error_response(
                            request.get("id"),
                            protocol.INTERNAL_ERROR,
                            f"failed to close agent session: {exc}",
                        )
                try:
                    protocol.send_frame(sock, response)
                except (OSError, protocol.FrameError):
                    return
                if request.get("method") == "goodbye":
                    return
        finally:
            state.unregister_session(session_id)


def _probe_accept_loop(probe_sock: socket.socket, state: _DaemonState) -> None:
    semaphore = threading.Semaphore(config.DAEMON_IDENTIFY_MAX_CONCURRENT)
    while True:
        try:
            conn, _addr = probe_sock.accept()
        except OSError:
            return  # socket closed during shutdown
        if not semaphore.acquire(blocking=False):
            conn.close()  # over capacity -- drop immediately, never queue unboundedly
            continue
        threading.Thread(
            target=_handle_probe_connection, args=(conn, state, semaphore), daemon=True
        ).start()


def _handle_probe_connection(
    conn: socket.socket, state: _DaemonState, semaphore: threading.Semaphore
) -> None:
    try:
        conn.settimeout(config.DAEMON_IDENTIFY_READ_TIMEOUT_S)
        try:
            request = protocol.recv_frame(conn)
        except (OSError, protocol.FrameError):
            return
        if request.get("method") != "identify":
            return
        try:
            protocol.send_frame(conn, state.identify_response())
        except (OSError, protocol.FrameError):
            pass
    finally:
        semaphore.release()
        try:
            conn.close()
        except OSError:
            pass


def _probe_identify(port: int) -> dict[str, Any] | None:
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=config.DAEMON_IDENTIFY_READ_TIMEOUT_S
        ) as sock:
            sock.settimeout(config.DAEMON_IDENTIFY_READ_TIMEOUT_S)
            protocol.send_frame(sock, {"method": "identify"})
            return protocol.recv_frame(sock)
    except (OSError, protocol.FrameError):
        return None


def _classify_and_report_loser(db_path: str, key: str) -> bool:
    """Called after a guard-bind failure. Returns True if this is a quiet, expected loss (a
    legitimate existing owner for this exact db_path already won) -- False (with a logged error)
    for a foreign occupant or a genuine cross-db collision, per §4/§7."""
    info = _probe_identify(discovery.probe_port(key))
    if info is None:
        logger.error(
            "Election port %d is held by an unrelated process -- not a SALTMDB daemon. "
            "Check what's listening (e.g. lsof -i :%d / netstat) before retrying.",
            discovery.election_port(key),
            discovery.election_port(key),
        )
        return False
    other_db_path = info.get("db_path")
    if other_db_path and other_db_path != db_path:
        logger.error(
            "Election port %d is already owned by a SALTMDB daemon for a different database "
            "(%s) -- this is a rare hash collision against %s. Move one database to a different "
            "path to change its derived port.",
            discovery.election_port(key),
            other_db_path,
            db_path,
        )
        return False
    return True  # same db_path -- a legitimate existing owner, quiet exit


def _configure_stdio_logging() -> None:
    log_level = os.environ.get("SALTMDB_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )


def _daemon_log_redirect(db_path: str) -> None:
    """Redirects stdout/stderr to daemon.log (same directory as the DB), matching the
    viewer.log/librarian.log redirection precedent, so a spawned background daemon's output is
    visible for debugging rather than lost."""
    log_dir = os.path.dirname(db_path) or os.path.expanduser("~/.saltmdb")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "daemon.log")
    log_file = open(log_path, "a", encoding="utf-8")
    sys.stdout = log_file
    sys.stderr = log_file


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(prog="saltmdb-daemon")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Explicit foreground launch (ops/debugging) -- disables the grace-period "
        "auto-shutdown timer; runs until SIGINT/SIGTERM regardless of session count.",
    )
    args = parser.parse_args()

    db_path = discovery.resolve_canonical_db_path()

    if not args.foreground:
        _daemon_log_redirect(db_path)
    _configure_stdio_logging()

    for _blas_var in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(_blas_var, "1")

    # Step 1-2: cross-OS mount refusal check, first -- cheapest, most safety-critical.
    classification = platform_paths.classify_db_path(db_path)
    if classification != "local":
        logger.error(
            "Refusing to start: database path %s classified as %r (cross-OS shared-DB mount or "
            "undetermined) -- see reconciliation doc §2.4 for why this is unsupported.",
            db_path,
            classification,
        )
        sys.exit(1)

    key = discovery.daemon_key(db_path)
    e_port = discovery.election_port(key)
    p_port = discovery.probe_port(key)

    # Step 3: guard-socket bind (the ownership mutex). Bind-only, never accept()s, for the
    # daemon's entire lifetime -- matches reconciliation §2.1 exactly.
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        guard.bind(("127.0.0.1", e_port))
        guard.listen(1)
    except OSError:
        guard.close()
        legitimate_owner = _classify_and_report_loser(db_path, key)
        sys.exit(0 if legitimate_owner else 1)

    # Probe-port bind, immediately after winning the guard. Rollback on failure (round-4 fix):
    # close the guard rather than leaving a half-started daemon holding it.
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe_sock.bind(("127.0.0.1", p_port))
        probe_sock.listen(config.DAEMON_IDENTIFY_MAX_CONCURRENT)
    except OSError as e:
        logger.error("Failed to bind probe port %d: %s", p_port, e)
        probe_sock.close()
        guard.close()
        sys.exit(1)

    state = _DaemonState(db_path, key, args.foreground)

    # Start the probe accept-loop thread immediately -- before init_db or anything else -- so the
    # "initializing" identify state is accurate for the whole remainder of startup.
    probe_thread = threading.Thread(
        target=_probe_accept_loop, args=(probe_sock, state), daemon=True
    )
    probe_thread.start()

    # Step 4: the bootstrap connection is the only writer before the coordinator.
    conn = init_db(db_path)
    conn.close()
    try:
        from saltmdb.domain.services.embedding_service import (
            EmbedJobScheduler,
            reconcile_embedding_jobs,
            reconcile_retrieval_embedding_jobs,
        )

        state.coordinator = DbWriteCoordinator(db_path)
        state.coordinator.start()
        from saltmdb.db.connection import enable_daemon_connection_boundary

        enable_daemon_connection_boundary()
        # One deliberate, conservative recovery generation for every active
        # legacy row.  Each page is a bounded background transaction.
        after_id = None
        reconciled = 0
        while True:

            def _reconcile_page(c, cursor=after_id) -> list[str]:
                return reconcile_embedding_jobs(c, limit=100, after_id=cursor)

            page = cast(
                list[str],
                state.coordinator.submit(
                    "reconcile_embedding_jobs",
                    _reconcile_page,
                    priority="background",
                ),
            )
            if not page:
                break
            reconciled += len(page)
            after_id = page[-1]
        retrieval_after_id = None
        retrieval_reconciled = 0
        while True:

            def _reconcile_retrieval_page(c, cursor=retrieval_after_id) -> list[str]:
                return reconcile_retrieval_embedding_jobs(c, limit=100, after_id=cursor)

            page = cast(
                list[str],
                state.coordinator.submit(
                    "reconcile_retrieval_embedding_jobs",
                    _reconcile_retrieval_page,
                    priority="background",
                ),
            )
            if not page:
                break
            retrieval_reconciled += len(page)
            retrieval_after_id = page[-1]
        logger.info(
            "Reconciled durable embedding jobs for %d active entities (%d retrieval-text rows).",
            reconciled,
            retrieval_reconciled,
        )
        state.embedding_scheduler = EmbedJobScheduler(state.coordinator)
        state.embedding_scheduler.start()
    except Exception as e:
        logger.exception("Startup durable embedding recovery failed: %s", e)
        if state.coordinator:
            state.coordinator.shutdown(timeout=config.DAEMON_SHUTDOWN_DRAIN_TIMEOUT_S)
        probe_sock.close()
        guard.close()
        sys.exit(1)

    # Step 5: service-port TCP listener, OS-assigned port.
    service_server = _ThreadingRpcServer(("127.0.0.1", 0), _RpcRequestHandler)
    service_server.daemon_state = state
    service_port = service_server.server_address[1]
    state.service_port = service_port

    # Step 6: Viewer thread, BEFORE discovery-file publication (round-5 fix -- a Viewer bind
    # failure must never leave a broken daemon already advertised as ready). Librarian's
    # integration is just its existing trigger_librarian()/run_librarian_now() call sites, already
    # wired to the single-worker _librarian_trigger_pool -- nothing additional to "start" here.
    viewer_httpd = None
    viewer_thread = None
    if config.is_viewer_enabled():
        from saltmdb.viewer.routes import SALTMDBHandler
        from saltmdb.viewer.server import SALTMDBTCPServer
        from saltmdb.viewer.context import ViewerReadGateway

        viewer_port = config.get_viewer_port()
        # Round-4 fix: explicit, since the daemon constructs this directly and bypasses
        # viewer/server.py:main(), where allow_reuse_address was previously set.
        SALTMDBTCPServer.allow_reuse_address = True
        try:
            viewer_httpd = SALTMDBTCPServer(("127.0.0.1", viewer_port), SALTMDBHandler)
            viewer_httpd.daemon_state = state
            viewer_httpd.viewer_gateway = ViewerReadGateway(db_path, state)
        except OSError as e:
            logger.error("Viewer bind failed on port %d: %s", viewer_port, e)
            service_server.server_close()
            if state.embedding_scheduler is not None:
                state.embedding_scheduler.stop()
            if state.coordinator is not None:
                state.coordinator.shutdown(timeout=config.DAEMON_SHUTDOWN_DRAIN_TIMEOUT_S)
            probe_sock.close()
            guard.close()
            sys.exit(1)
        viewer_thread = threading.Thread(target=viewer_httpd.serve_forever, daemon=True)
        viewer_thread.start()
        state.viewer_port = viewer_port  # daemon-authoritative for the viewer_status RPC (Codex
        # round-1 finding: saltmdb-viewer must report the daemon's own state, never its own
        # client-local env, since the two can legitimately differ).

    # Step 7: generate the auth token and publish the discovery file -- the final startup step,
    # so nothing is ever advertised as ready until every other listener has already bound.
    auth_token = secrets.token_urlsafe(32)
    state.auth_token = auth_token
    discovery.write(key, db_path, os.getpid(), service_port, auth_token)
    logger.info(
        "SALTMDB daemon ready: db=%s service_port=%d election_port=%d probe_port=%d",
        db_path,
        service_port,
        e_port,
        p_port,
    )

    # Codex round-1 finding (confirmed against Python's own documented constraint: "[shutdown]
    # must be called while serve_forever() is running in a different thread, or it will
    # deadlock" -- socketserver.BaseServer.shutdown()): a signal handler runs on the MAIN thread
    # regardless of which OS thread actually received the signal, so a handler that called
    # _shutdown_sequence() (and therefore service_server.shutdown()) directly would deadlock
    # against the very serve_forever() loop it's trying to stop, whenever SIGTERM/SIGINT hit a
    # daemon whose main thread was inside serve_forever() -- exactly the common case. The grace-
    # period timer never hit this because threading.Timer already runs its callback on its own
    # thread, genuinely separate from serve_forever()'s.
    #
    # Fix: a dedicated shutdown-watcher thread is the only thing that ever calls
    # _shutdown_sequence(). Signal handlers (and the grace timer, and the KeyboardInterrupt
    # fallback below) do nothing but flip a threading.Event -- an operation safe to perform from
    # any thread/signal-handler context. This also makes shutdown single-entry/idempotent for
    # free: Event.set() is idempotent and .wait() only ever unblocks the watcher once, so
    # concurrent triggers (e.g. a grace-period fire racing a SIGTERM) can never run
    # _shutdown_sequence() more than once.
    shutdown_requested = threading.Event()

    def _request_shutdown(*_args) -> None:
        shutdown_requested.set()

    # Construct before the watcher thread starts: the watcher closes over this object to stop it
    # during shutdown, so it must be bound even if a signal lands immediately at startup.
    stall_monitor = EmbedStallMonitor(db_path)

    def _shutdown_watcher() -> None:
        shutdown_requested.wait()
        _shutdown_sequence(
            state, service_server, probe_sock, guard, viewer_httpd, key, stall_monitor
        )

    watcher_thread = threading.Thread(target=_shutdown_watcher, daemon=True)
    watcher_thread.start()

    # H6: periodic stale-pending visibility. Deliberately no monitor-owned termination path: the
    # existing grace shutdown already handles a genuinely idle daemon, while terminating a daemon
    # with a live session needs an explicit future lifecycle policy.
    stall_monitor.start()

    state.set_shutdown_callback(_request_shutdown)
    state.start_initial_grace_period()

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    try:
        service_server.serve_forever()
    except KeyboardInterrupt:
        _request_shutdown()

    # serve_forever() only returns once the watcher thread has called service_server.shutdown()
    # from within _shutdown_sequence() -- i.e. shutdown is already fully underway. If this (main)
    # thread were allowed to fall off the end of main() here, normal Python interpreter shutdown
    # would begin immediately and could kill the watcher thread mid-sequence, before it ever
    # reaches discovery.remove()/guard.close()/os._exit(0) (self-caught during implementation, a
    # real smoke-test find). Block here indefinitely instead: the only way this process ever
    # actually exits past this point is _shutdown_sequence()'s own os._exit(0), on the watcher
    # thread.
    watcher_thread.join()


def _shutdown_sequence(
    state, service_server, probe_sock, guard, viewer_httpd, key, stall_monitor=None
) -> None:
    """Ordered shutdown -- latency/resource hygiene, NOT a data-safety mechanism (see
    scratch/plans/track_b_daemon_detailed.md §6's reframing: SQLite's own WAL+busy_timeout+retry
    concurrency machinery, already relied on throughout this codebase, is what actually makes
    brief overlap between an outgoing daemon and its successor safe)."""
    state.begin_draining()

    if state.embedding_scheduler is not None:
        state.embedding_scheduler.stop()
    if state.coordinator is not None:
        state.coordinator.begin_draining()

    if stall_monitor is not None:
        stall_monitor.stop()  # daemon=True thread, dies with the process regardless -- stopped
        # here purely to avoid a stray check firing mid-drain, not for correctness.

    service_server.shutdown()  # stops serve_forever's accept loop
    service_server.server_close()  # round-5 fix: shutdown() alone does not release the port
    try:
        probe_sock.close()
    except OSError:
        pass

    if viewer_httpd is not None:
        viewer_httpd.shutdown()
        viewer_httpd.server_close()  # round-5 fix, same reasoning

    from saltmdb.domain.services import librarian_service, memory_service

    for pool in (
        memory_service._embed_pool,
        memory_service._search_pool,
        librarian_service._librarian_trigger_pool,
    ):
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.debug("Executor pool shutdown failed (non-fatal): %s", e)

    if state.coordinator is not None:
        # An active foreground transaction is allowed to finish.  Queued
        # foreground work was resolved during begin_draining; durable
        # background jobs remain in SQLite for restart recovery.
        state.coordinator.shutdown()

    discovery.remove(key)

    try:
        guard.close()
    except OSError:
        pass

    logger.info("SALTMDB daemon shutdown complete.")
    # Forceful exit rather than a normal interpreter shutdown: ThreadPoolExecutor's worker
    # threads are non-daemon, so a hung/slow background task would otherwise keep this process
    # alive well past the guard release -- an accepted, bounded resource-retention tail per §6's
    # reframing, not something worth blocking process exit on.
    os._exit(0)


if __name__ == "__main__":
    main()
