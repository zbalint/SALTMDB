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
from typing import Optional, Tuple

from saltmdb.daemon import discovery


class TestAdapterSignalShutdown(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
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
        adapter_env["SALTMDB_OWNER_ID"] = "test_owner"
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
                        "SELECT ended_at FROM _agent_sessions WHERE owner_id = 'test_owner' ORDER BY started_at DESC LIMIT 1"
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
            self.assertIsNone(session_ended_at_before, "ended_at should be None while session is active")

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
                "SELECT ended_at FROM _agent_sessions WHERE owner_id = 'test_owner' ORDER BY started_at DESC LIMIT 1"
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


if __name__ == "__main__":
    unittest.main()
