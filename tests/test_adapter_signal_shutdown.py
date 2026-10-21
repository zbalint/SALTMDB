"""Tests for SALTMDB MCP adapter signal-handling during shutdown."""

import os
import sys
import time
import signal
import shutil
import sqlite3
import tempfile
import unittest
import subprocess
from unittest.mock import patch
from typing import Optional, Tuple

from saltmdb.daemon import discovery


class TestAdapterSignalShutdown(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.home_patch = patch.dict(
            os.environ, {"HOME": self.temp_dir, "USERPROFILE": self.temp_dir}
        )
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.canonical = discovery.resolve_canonical_db_path(self.db_path)
        self.key = discovery.daemon_key(self.canonical)
        self.discovery_file = discovery.discovery_path(self.key)
        try:
            os.remove(self.discovery_file)
        except OSError:
            pass

    def tearDown(self):
        try:
            os.remove(self.discovery_file)
        except OSError:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _start_daemon(self) -> Tuple[subprocess.Popen, dict]:
        env = dict(os.environ)
        env["SALTMDB_DB_PATH"] = self.db_path
        env["SALTMDB_VIEWER_ENABLED"] = "false"

        daemon_proc = subprocess.Popen(
            [sys.executable, "-m", "saltmdb.daemon.server", "--foreground"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(self.discovery_file):
            if daemon_proc.poll() is not None:
                out = daemon_proc.stdout.read().decode("utf-8", errors="replace")
                self.fail(
                    f"daemon exited early (code {daemon_proc.returncode}) before publishing discovery:\n{out}"
                )
            time.sleep(0.1)

        self.assertTrue(
            os.path.exists(self.discovery_file), "daemon never published its discovery file in time"
        )
        return daemon_proc, env

    def _start_adapter(self, env: dict) -> subprocess.Popen:
        adapter_env = dict(env)
        adapter_env["SALTMDB_AGENT_ID"] = "test_owner"
        return subprocess.Popen(
            [sys.executable, "-m", "saltmdb"],
            env=adapter_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _wait_for_session(self, adapter_proc: subprocess.Popen) -> Optional[str]:
        db_deadline = time.time() + 15
        while time.time() < db_deadline:
            if adapter_proc.poll() is not None:
                out = adapter_proc.stdout.read().decode("utf-8", errors="replace")
                self.fail(f"adapter exited early (code {adapter_proc.returncode}):\n{out}")
            if os.path.exists(self.db_path):
                try:
                    conn = sqlite3.connect(self.db_path)
                    cursor = conn.execute(
                        "SELECT ended_at FROM _agent_sessions WHERE agent_id = 'test_owner' ORDER BY started_at DESC LIMIT 1"
                    )
                    row = cursor.fetchone()
                    conn.close()
                    if row is not None:
                        return row[0]
                except sqlite3.OperationalError:
                    pass
            time.sleep(0.2)
        self.fail("adapter session row was not recorded in _agent_sessions")

    def _cleanup_procs(
        self, daemon_proc: Optional[subprocess.Popen], adapter_proc: Optional[subprocess.Popen]
    ) -> None:
        if adapter_proc is not None:
            if adapter_proc.poll() is None:
                adapter_proc.kill()
                adapter_proc.wait(timeout=5)
            if adapter_proc.stdin:
                try:
                    adapter_proc.stdin.close()
                except OSError:
                    pass
            if adapter_proc.stdout:
                try:
                    adapter_proc.stdout.close()
                except OSError:
                    pass

        if daemon_proc is not None:
            if daemon_proc.poll() is None:
                daemon_proc.kill()
                daemon_proc.wait(timeout=5)
            if daemon_proc.stdout:
                try:
                    daemon_proc.stdout.close()
                except OSError:
                    pass

        try:
            os.remove(self.discovery_file)
        except OSError:
            pass

    def _run_signal_test(self, sig: signal.Signals) -> None:
        daemon_proc = None
        adapter_proc = None
        try:
            daemon_proc, env = self._start_daemon()
            adapter_proc = self._start_adapter(env)
            session_ended_at_before = self._wait_for_session(adapter_proc)
            self.assertIsNone(
                session_ended_at_before, "ended_at should be None while session is active"
            )

            adapter_proc.send_signal(sig)
            try:
                exit_code = adapter_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                adapter_proc.kill()
                adapter_proc.wait(timeout=5)
                self.fail(f"adapter did not exit within 15s of signal {sig}")

            self.assertEqual(exit_code, 0)

            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "SELECT ended_at FROM _agent_sessions WHERE agent_id = 'test_owner' ORDER BY started_at DESC LIMIT 1"
            )
            row_after = cursor.fetchone()
            conn.close()
            self.assertIsNotNone(row_after, "session row missing after shutdown")
            self.assertIsNotNone(
                row_after[0],
                f"ended_at is still None after adapter received signal {sig} -- goodbye RPC failed or did not run",
            )
        finally:
            self._cleanup_procs(daemon_proc, adapter_proc)

    def test_sigterm_triggers_goodbye_and_records_ended_at(self):
        self._run_signal_test(signal.SIGTERM)

    def test_sigint_also_triggers_goodbye_and_records_ended_at(self):
        self._run_signal_test(signal.SIGINT)


class TestMainDispatch(unittest.TestCase):
    def test_rpc_precedence_and_output(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from saltmdb import __main__ as entry

        for argv, method, params in (
            (
                ["saltmdb", "--backfill-chunk-embeddings", "--librarian"],
                "run_backfill_chunk_embeddings_now",
                {},
            ),
            (["saltmdb", "--librarian"], "run_librarian_now", {"force": True}),
        ):
            with (
                self.subTest(argv=argv),
                patch.object(sys, "argv", argv),
                patch.object(entry, "get_db_path", return_value="/tmp/fictional.db"),
                patch("saltmdb.daemon.client.call_method", return_value={"done": 2}) as call,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertIsNone(entry.main())
                self.assertEqual(output.getvalue(), "{'done': 2}\n")
                call.assert_called_once_with("/tmp/fictional.db", method, params)

    def test_rpc_failure_exits_one_without_success_output(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch
        from saltmdb import __main__ as entry

        for flag in ("--backfill-chunk-embeddings", "--librarian"):
            with (
                self.subTest(flag=flag),
                patch.object(sys, "argv", ["saltmdb", flag]),
                patch.object(entry, "get_db_path", return_value="/tmp/fictional.db"),
                patch("saltmdb.daemon.client.call_method", side_effect=RuntimeError("offline")),
                redirect_stderr(io.StringIO()) as error,
                redirect_stdout(io.StringIO()) as output,
                self.assertRaises(SystemExit) as raised,
            ):
                entry.main()
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(
                error.getvalue(),
                "Error: could not reach a SALTMDB daemon for /tmp/fictional.db: offline\n",
            )

    def test_adapter_identity_and_backend_setup_order(self):
        from unittest.mock import patch
        from saltmdb import __main__ as entry

        events = []
        with (
            patch.object(sys, "argv", ["saltmdb"]),
            patch.object(entry, "get_agent_id", return_value="configured-agent"),
            patch(
                "saltmdb.mcp.identity.SESSION_IDENTITY.configure_agent_id",
                side_effect=lambda agent_id: events.append(("identity", agent_id)),
            ),
            patch(
                "saltmdb.mcp.tools.configure_backend",
                side_effect=lambda backend: events.append(("backend", type(backend).__name__)),
            ),
            patch(
                "anyio.run", side_effect=lambda function, *args: events.append(("run", function))
            ),
        ):
            self.assertIsNone(entry.main())

        self.assertEqual(events[0], ("identity", "configured-agent"))
        self.assertEqual(events[1][0], "backend")
        self.assertEqual(events[2][0], "run")


if __name__ == "__main__":
    unittest.main()
