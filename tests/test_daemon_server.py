"""Track B (scratch/plans/track_b_daemon_detailed.md): daemon/server.py's concurrency logic.

Written in response to a Codex round-1 diff review that found the daemon package had NO
dedicated test coverage at all -- only a manual smoke test. Two layers here:

1. TestDaemonStateConcurrency -- pure-Python unit tests against _DaemonState directly (no real
   sockets/subprocess), covering the session/inflight/draining state machine the round-1 review's
   claims #3 and #4 were about (grace-timer-vs-inflight-RPC race, hello-vs-grace-timer admission
   race).
2. TestDaemonSignalShutdown -- one real subprocess regression test proving round-1 claim #1 (the
   SIGTERM/SIGINT shutdown deadlock) is actually fixed end-to-end, not just in theory. This is the
   single highest-value test in this file: the prior manual smoke test only ever exercised the
   grace-period shutdown path, which was never actually broken (threading.Timer already runs its
   callback on its own thread) -- it never once sent the daemon a real signal.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from saltmdb.daemon import discovery, protocol
from saltmdb.daemon.server import _DaemonState


class TestDaemonStateConcurrency(unittest.TestCase):
    def _state(self, foreground: bool = False) -> _DaemonState:
        state = _DaemonState(
            db_path="/tmp/saltmdb_test_daemon_state.db", key="testkey", foreground=foreground
        )
        state.auth_token = "tok"
        return state

    def test_hello_registers_session_atomically(self):
        state = self._state()
        resp = state.handle_request(protocol.build_request("hello", {}, token="tok"), session_id=1)
        self.assertTrue(resp["ok"])
        self.assertIn(1, state._sessions)

    def test_hello_rejected_while_draining_does_not_register(self):
        """Codex round-1 claim #4: hello approval and session registration must be atomic with
        the draining check, closing the gap where a hello could be acknowledged "ok" right as the
        grace timer transitions to draining."""
        state = self._state()
        state.begin_draining()
        resp = state.handle_request(protocol.build_request("hello", {}, token="tok"), session_id=1)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], protocol.DAEMON_SHUTTING_DOWN)
        self.assertNotIn(1, state._sessions)

    def test_grace_fire_defers_while_session_active(self):
        state = self._state()
        state.handle_request(protocol.build_request("hello", {}, token="tok"), session_id=1)
        fired = []
        state.set_shutdown_callback(lambda: fired.append(True))
        state._grace_fire()
        self.assertFalse(state._draining)
        self.assertEqual(fired, [])

    def test_grace_fire_defers_while_rpc_inflight(self):
        """Codex round-1 claim #3: the grace timer must not shut down while a long one-shot RPC
        (e.g. run_backfill_chunk_embeddings_now) is in progress, even though no hello session was
        ever opened for it."""
        state = self._state()
        state._acquire_inflight()
        try:
            fired = []
            state.set_shutdown_callback(lambda: fired.append(True))
            state._grace_fire()
            self.assertFalse(state._draining)
            self.assertEqual(fired, [])
        finally:
            state._release_inflight()

    def test_grace_fire_shuts_down_when_idle(self):
        state = self._state()
        fired = []
        state.set_shutdown_callback(lambda: fired.append(True))
        state._grace_fire()
        self.assertTrue(state._draining)
        self.assertEqual(fired, [True])

    def test_release_inflight_rearms_timer_once_clear(self):
        state = self._state()
        state._acquire_inflight()
        self.assertIsNone(state._shutdown_timer)
        state._release_inflight()
        self.assertIsNotNone(state._shutdown_timer)
        state._shutdown_timer.cancel()

    def test_foreground_daemon_never_arms_a_timer(self):
        state = self._state(foreground=True)
        state._acquire_inflight()
        state._release_inflight()
        self.assertIsNone(state._shutdown_timer)

    def test_malformed_params_returns_malformed_request(self):
        request = protocol.build_request("tool_call", {}, token="tok")
        request["params"] = "not-a-dict"
        resp = self._state().handle_request(request)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], protocol.MALFORMED_REQUEST)

    def test_malformed_falsy_params_also_returns_malformed_request(self):
        """Codex round-2 finding: `request.get("params") or {}` let falsy wrong-type values
        ([], "", 0, False) silently pass through as {} without ever reaching the isinstance
        check. Only an absent key or explicit null may mean "no params"."""
        for bad_params in ([], "", 0, False):
            with self.subTest(bad_params=bad_params):
                request = protocol.build_request("tool_call", {}, token="tok")
                request["params"] = bad_params
                resp = self._state().handle_request(request)
                self.assertFalse(resp["ok"])
                self.assertEqual(resp["error"]["code"], protocol.MALFORMED_REQUEST)

    def test_null_or_absent_params_default_to_empty_dict(self):
        # tool_call with normalized-empty params fails fast (no real DB access) at the
        # "tool not in DISPATCH_TABLE" check for a None `tool` lookup -- reaching UNKNOWN_TOOL
        # (rather than MALFORMED_REQUEST) proves params was normalized to {} and dispatch was
        # actually entered, for both an explicit null and an entirely absent "params" key.
        request_null = protocol.build_request("tool_call", {}, token="tok")
        request_null["params"] = None
        resp_null = self._state().handle_request(request_null)
        self.assertFalse(resp_null["ok"])
        self.assertEqual(resp_null["error"]["code"], protocol.UNKNOWN_TOOL)

        request_absent = protocol.build_request("tool_call", {}, token="tok")
        del request_absent["params"]
        resp_absent = self._state().handle_request(request_absent)
        self.assertFalse(resp_absent["ok"])
        self.assertEqual(resp_absent["error"]["code"], protocol.UNKNOWN_TOOL)

    def test_viewer_status_reports_enabled_daemon_state(self):
        state = self._state()
        state.viewer_port = 8080
        resp = state.handle_request(protocol.build_request("viewer_status", {}, token="tok"))
        self.assertEqual(resp["result"], {"enabled": True, "port": 8080})

    def test_viewer_status_reports_disabled_daemon_state(self):
        resp = self._state().handle_request(
            protocol.build_request("viewer_status", {}, token="tok")
        )
        self.assertEqual(resp["result"], {"enabled": False, "port": None})

    def test_acquire_inflight_rejected_once_draining(self):
        """Codex round-2 finding: _acquire_inflight() must itself atomically check draining --
        a request that reaches dispatch after the daemon has already committed to shutdown must
        be rejected outright, never silently admitted to run past the pending os._exit(0)."""
        state = self._state()
        state.begin_draining()
        self.assertFalse(state._acquire_inflight())
        self.assertEqual(state._inflight, 0)


class TestDaemonStateRealConcurrency(unittest.TestCase):
    """Codex round-2 critique of the round-1 tests: they only called private state methods
    serially, proving semantic equivalence but never actually exercising a real race under
    genuine thread contention. These use threading.Barrier to line up two real threads at the
    exact contention point and run many iterations, asserting the safety invariant holds on
    every single trial regardless of which thread's lock acquisition wins."""

    ITERATIONS = 200

    def _state(self) -> _DaemonState:
        state = _DaemonState(
            db_path="/tmp/saltmdb_test_daemon_state.db", key="testkey", foreground=False
        )
        state.auth_token = "tok"
        return state

    def test_concurrent_inflight_admission_and_grace_fire_are_mutually_exclusive(self):
        """Regression test for round-2 finding #3. Codex round-3 correction to this test's first
        draft: asserting on _acquire_inflight()'s own return value doesn't discriminate against a
        reverted/broken implementation that always returns a truthy-by-coincidence value (e.g. the
        pre-round-3 code had no return statement at all, so an old-code run through this exact
        loop wouldn't reliably fail for the right reason). Checking the raw internal state instead
        -- _draining and _inflight can never both be "true" at once -- is correct regardless of
        what the return value claims, since it's the actual invariant that matters: the daemon
        must never be both committed to shutdown AND carrying a live admitted RPC.

        Discriminating power verified empirically (not just asserted): a throwaway script that
        monkeypatched _acquire_inflight() back to its pre-round-3 unconditional-increment shape
        and ran this exact barrier loop 500 times found the invariant violated in 500/500 trials
        against the broken version -- a real, deterministic discriminator, not a lucky
        coincidence of timing."""
        for _ in range(self.ITERATIONS):
            state = self._state()
            barrier = threading.Barrier(2)

            def acquire():
                barrier.wait(timeout=5)
                state._acquire_inflight()

            def fire():
                barrier.wait(timeout=5)
                state._grace_fire()

            t1 = threading.Thread(target=acquire)
            t2 = threading.Thread(target=fire)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            self.assertFalse(
                state._draining and state._inflight > 0,
                "an RPC was admitted (inflight>0) AND the daemon committed to shutdown simultaneously",
            )
            if state._inflight > 0:
                state._release_inflight()

    def test_concurrent_hello_and_grace_fire_are_mutually_exclusive(self):
        """Regression test for round-1 finding #4, checking raw state (was the session actually
        registered) rather than trusting handle_request()'s response shape -- same style as the
        inflight test above, but an honest caveat unlike that one: this test's own discriminating
        power against the ORIGINAL pre-round-1-fix bug was verified empirically to be weak (0/500
        synthetic trials reproducing the old two-step "respond ok, then register" shape actually
        caught it), because the real production race required a genuine socket I/O gap
        (protocol.send_frame between the response and the old separate register_session() call)
        that a synchronous in-process call sequence can't reliably reproduce -- confirming Codex
        round-3's own "probabilistic, not deterministic" characterization rather than contradicting
        it. What this test DOES still provide: proof that the CURRENT atomic-under-one-lock
        structure holds under real thread contention on every trial it runs, and a trip-wire
        against a future regression that reintroduces a large enough window to matter. The
        structural guarantee itself (registration inside the same critical section _grace_fire()
        uses) was independently confirmed correct by code reading in both round 1 and round 2."""
        for _ in range(self.ITERATIONS):
            state = self._state()
            barrier = threading.Barrier(2)

            def hello():
                barrier.wait(timeout=5)
                state.handle_request(protocol.build_request("hello", {}, token="tok"), session_id=1)

            def fire():
                barrier.wait(timeout=5)
                state._grace_fire()

            t1 = threading.Thread(target=hello)
            t2 = threading.Thread(target=fire)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            self.assertFalse(
                state._draining and 1 in state._sessions,
                "hello registered a session AND the daemon committed to shutdown simultaneously",
            )
            state._sessions.discard(1)


class TestDaemonSignalShutdown(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "sigterm_test.db")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_sigterm_triggers_clean_shutdown_without_deadlock(self):
        canonical = discovery.resolve_canonical_db_path(self.db_path)
        key = discovery.daemon_key(canonical)
        discovery_file = discovery.discovery_path(key)
        # Guard against debris from an unrelated earlier crash on this exact derived path.
        try:
            os.remove(discovery_file)
        except OSError:
            pass

        env = dict(os.environ)
        env["SALTMDB_DB_PATH"] = self.db_path
        # Irrelevant to this test and actively conflicts with the real daemon this dev machine
        # already runs on the default Viewer port -- disable it so only the signal-shutdown path
        # under test is exercised.
        env["SALTMDB_VIEWER_ENABLED"] = "false"
        proc = subprocess.Popen(
            [sys.executable, "-m", "saltmdb.daemon.server", "--foreground"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.time() + 30
            while time.time() < deadline and not os.path.exists(discovery_file):
                if proc.poll() is not None:
                    out = proc.stdout.read().decode("utf-8", errors="replace")
                    self.fail(
                        f"daemon exited early (code {proc.returncode}) before publishing discovery:\n{out}"
                    )
                time.sleep(0.1)
            self.assertTrue(
                os.path.exists(discovery_file), "daemon never published its discovery file in time"
            )

            # The actual regression test: SIGTERM must not deadlock against serve_forever()'s own
            # thread (Codex round-1 claim #1, confirmed against socketserver.BaseServer.shutdown()'s
            # own documented "must be called from a different thread or it will deadlock" constraint).
            # A second SIGTERM sent immediately after is a best-effort smoke addition, not a strong
            # proof of single-entry shutdown -- Codex round-3 correctly noted POSIX signals can
            # coalesce, so this doesn't guarantee two independent deliveries. The real proof of
            # idempotency is the Event-based design itself (Event.set() is textbook-idempotent,
            # confirmed by code reading in round 2); this just checks the daemon still exits
            # cleanly rather than erroring under a rapid repeat signal.
            proc.send_signal(signal.SIGTERM)
            proc.send_signal(signal.SIGTERM)
            try:
                exit_code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                self.fail(
                    "daemon did not exit within 15s of SIGTERM -- shutdown deadlock regression"
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse(
                os.path.exists(discovery_file), "discovery file not removed on clean shutdown"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
            try:
                os.remove(discovery_file)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
