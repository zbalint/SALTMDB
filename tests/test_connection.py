import unittest
import tempfile
import os
import shutil
import sqlite3
import threading
import time
import logging

from saltmdb.config import RETRY_MAX_ATTEMPTS
from saltmdb.db.connection import (
    get_connection,
    write_transaction,
    write_transaction_retrying,
    close_connection,
)
from saltmdb.db.schema import init_db


class _FlakyConnWrapper:
    """Duck-typed wrapper around a real sqlite3.Connection that raises a controlled
    sqlite3.OperationalError on the first `n_failures` calls whose sql text contains
    `fail_on_substring`, then delegates to the real connection. Lets us deterministically
    exercise write_transaction_retrying's retry-count logic without depending on real
    timing-based lock contention (sqlite3.Connection is a C type and cannot be monkeypatched
    directly -- see test comments below for why this wrapper approach is used instead).
    """

    def __init__(self, real_conn, n_failures, error_message="database is locked", fail_on_substring="BEGIN IMMEDIATE"):
        self._real = real_conn
        self._remaining_failures = n_failures
        self._error_message = error_message
        self._fail_on_substring = fail_on_substring
        self.call_count = 0

    def execute(self, sql, *params):
        self.call_count += 1
        if self._fail_on_substring in sql and self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise sqlite3.OperationalError(self._error_message)
        return self._real.execute(sql, *params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestGetConnection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_isolation_level_is_none(self):
        # isolation_level=None is required so sqlite3 never implicitly manages its own
        # deferred BEGIN -- see write_transaction's docstring for why that matters for
        # busy_timeout to actually apply to write-lock acquisition.
        self.assertIsNone(self.conn.isolation_level)


class TestWriteTransactionCommitRollback(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_commit_persists_row_visible_to_second_connection(self):
        write_transaction_retrying(
            self.conn,
            lambda c: c.execute(
                "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
                ("evt-commit-1", "agent_qa", "test", "commit-test"),
            )
        )

        # Open a second, independent connection to the same file and confirm the row
        # is actually durable, not just visible within the first connection's own cache.
        second_conn = get_connection(self.db_path)
        try:
            row = second_conn.execute(
                "SELECT id FROM events WHERE id = ?", ("evt-commit-1",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "evt-commit-1")
        finally:
            close_connection(second_conn)

    def test_exception_inside_block_rolls_back(self):
        class _Boom(Exception):
            pass

        def _write(c):
            c.execute(
                "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
                ("evt-rollback-1", "agent_qa", "test", "rollback-test"),
            )
            raise _Boom("simulated failure inside transaction")

        with self.assertRaises(_Boom):
            write_transaction_retrying(self.conn, _write)

        row = self.conn.execute(
            "SELECT id FROM events WHERE id = ?", ("evt-rollback-1",)
        ).fetchone()
        self.assertIsNone(row)


class TestBeginImmediateContention(unittest.TestCase):
    """The core regression test: proves BEGIN IMMEDIATE actually acquires the write lock
    up front (so busy_timeout genuinely applies to the wait), rather than the old deferred-
    transaction behavior where the lock upgrade happens invisibly at first-write time and can
    raise 'database is locked' near-instantly, bypassing busy_timeout's wait entirely.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn_a = init_db(self.db_path)
        self.conn_b = get_connection(self.db_path)

    def tearDown(self):
        self.conn_a.close()
        self.conn_b.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_begin_immediate_blocks_second_writer(self):
        HOLD_SECONDS = 0.7
        lock_held_event = threading.Event()
        thread_error = []

        def hold_lock_on_conn_a():
            try:
                def _write(c):
                    c.execute(
                        "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
                        ("evt-holder", "agent_qa", "test", "held-by-thread-a"),
                    )
                    # Signal the main thread only once the write lock is actually held
                    # (i.e. after BEGIN IMMEDIATE + the INSERT have both succeeded).
                    lock_held_event.set()
                    time.sleep(HOLD_SECONDS)
                write_transaction_retrying(self.conn_a, _write)
            except Exception as e:  # pragma: no cover - defensive, surfaced via thread_error
                thread_error.append(e)

        t = threading.Thread(target=hold_lock_on_conn_a)
        t.start()

        acquired = lock_held_event.wait(timeout=5.0)
        self.assertTrue(acquired, "background thread never signaled that it holds the write lock")

        start = time.monotonic()
        write_transaction_retrying(
            self.conn_b,
            lambda c: c.execute(
                "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
                ("evt-waiter", "agent_qa", "test", "written-by-main-thread"),
            )
        )
        elapsed = time.monotonic() - start

        t.join(timeout=5.0)
        self.assertFalse(thread_error, f"background thread raised: {thread_error}")

        # The whole point: conn_b's write must have genuinely waited for conn_a's held
        # write lock to release (via busy_timeout), not succeeded (or failed) instantly.
        # Allow generous scheduling slack on the low end, but it must be a real, measurable
        # fraction of HOLD_SECONDS -- an instantaneous success would indicate the write lock
        # was never actually contended (i.e. BEGIN IMMEDIATE silently not taking effect).
        self.assertGreater(
            elapsed, HOLD_SECONDS * 0.5,
            f"second writer succeeded too quickly ({elapsed:.3f}s) -- BEGIN IMMEDIATE "
            "may not actually be blocking on the held write lock"
        )

        # Both rows must be present -- the holder's write committed, and the waiter's
        # write succeeded after the wait rather than being silently dropped.
        rows = {
            r[0] for r in self.conn_b.execute(
                "SELECT id FROM events WHERE id IN ('evt-holder', 'evt-waiter')"
            ).fetchall()
        }
        self.assertEqual(rows, {"evt-holder", "evt-waiter"})


class TestWriteTransactionRetryingRetryBehavior(unittest.TestCase):
    """Exercises write_transaction_retrying's retry-count logic deterministically via a
    duck-typed connection wrapper (sqlite3.Connection is an immutable C type and cannot be
    monkeypatched directly -- verified: mock.patch.object(sqlite3.Connection, 'execute', ...)
    raises TypeError: cannot set 'execute' attribute of immutable type 'sqlite3.Connection').
    The wrapper is accepted because write_transaction/write_transaction_retrying only ever
    call conn.execute(...) -- duck typing is sufficient.
    """

    def test_retries_up_to_max_attempts_then_succeeds(self):
        # RETRY_MAX_ATTEMPTS retries beyond the first attempt means up to
        # RETRY_MAX_ATTEMPTS failures can be absorbed and the (RETRY_MAX_ATTEMPTS + 1)-th
        # attempt still succeeds.
        real_conn = sqlite3.connect(":memory:", isolation_level=None)
        real_conn.execute("CREATE TABLE t (x INTEGER)")
        wrapper = _FlakyConnWrapper(real_conn, n_failures=RETRY_MAX_ATTEMPTS)

        write_transaction_retrying(wrapper, lambda c: c.execute("INSERT INTO t VALUES (1)"))

        row = real_conn.execute("SELECT COUNT(*) FROM t").fetchone()
        self.assertEqual(row[0], 1)
        real_conn.close()

    def test_does_not_retry_beyond_max_attempts(self):
        # RETRY_MAX_ATTEMPTS + 1 failures exceeds the retry budget (1 initial attempt +
        # RETRY_MAX_ATTEMPTS retries = RETRY_MAX_ATTEMPTS + 1 total attempts), so the
        # final attempt must still fail and the OperationalError must propagate.
        real_conn = sqlite3.connect(":memory:", isolation_level=None)
        real_conn.execute("CREATE TABLE t (x INTEGER)")
        wrapper = _FlakyConnWrapper(real_conn, n_failures=RETRY_MAX_ATTEMPTS + 1)

        with self.assertRaises(sqlite3.OperationalError) as ctx:
            write_transaction_retrying(wrapper, lambda c: c.execute("INSERT INTO t VALUES (1)"))
        self.assertIn("database is locked", str(ctx.exception).lower())

        # Exactly RETRY_MAX_ATTEMPTS + 1 BEGIN IMMEDIATE attempts should have been made
        # (no extra retries beyond the configured budget).
        begin_attempts = RETRY_MAX_ATTEMPTS + 1 - wrapper._remaining_failures
        self.assertEqual(begin_attempts, RETRY_MAX_ATTEMPTS + 1)

        row = real_conn.execute("SELECT COUNT(*) FROM t").fetchone()
        self.assertEqual(row[0], 0)
        real_conn.close()

    def test_does_not_retry_unrelated_operational_error(self):
        real_conn = sqlite3.connect(":memory:", isolation_level=None)
        wrapper = _FlakyConnWrapper(
            real_conn, n_failures=1, error_message="no such table: ghost_table"
        )

        with self.assertRaises(sqlite3.OperationalError) as ctx:
            write_transaction_retrying(wrapper, lambda c: c.execute("INSERT INTO ghost_table VALUES (1)"))
        self.assertIn("no such table", str(ctx.exception).lower())

        # Only the single, non-retried attempt should have touched BEGIN IMMEDIATE.
        self.assertEqual(wrapper._remaining_failures, 0)
        real_conn.close()

    def test_retries_operational_error_raised_inside_callback(self):
        """Regression test: proves write_transaction_retrying correctly retries when
        OperationalError('database is locked') is raised inside the callback (after BEGIN IMMEDIATE)
        via a fresh BEGIN IMMEDIATE, succeeding on retry instead of raising RuntimeError.
        """
        real_conn = sqlite3.connect(":memory:", isolation_level=None)
        real_conn.execute("CREATE TABLE t (x INTEGER)")
        wrapper = _FlakyConnWrapper(real_conn, n_failures=1, error_message="database is locked", fail_on_substring="INSERT")

        invocations = 0

        def _write(c):
            nonlocal invocations
            invocations += 1
            c.execute("INSERT INTO t VALUES (1)")
            return "success_val"

        result = write_transaction_retrying(wrapper, _write)

        self.assertEqual(result, "success_val")
        self.assertEqual(invocations, 2)
        row = real_conn.execute("SELECT COUNT(*) FROM t").fetchone()
        self.assertEqual(row[0], 1)
        real_conn.close()


class TestCloseConnection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pragma_optimize_is_executed(self):
        conn = init_db(self.db_path)
        executed = []
        conn.set_trace_callback(lambda stmt: executed.append(stmt))
        close_connection(conn)
        self.assertTrue(
            any("PRAGMA optimize" in stmt for stmt in executed),
            f"PRAGMA optimize was not executed during close_connection; saw: {executed}",
        )

    def test_logs_at_debug_level(self):
        conn = init_db(self.db_path)
        with self.assertLogs("saltmdb.db.connection", level="DEBUG") as ctx:
            close_connection(conn)
        self.assertTrue(
            any("WAL checkpoint" in msg for msg in ctx.output),
            f"expected a WAL-checkpoint debug log line; saw: {ctx.output}",
        )

    def test_never_raises_even_if_pragma_fails(self):
        conn = init_db(self.db_path)
        # Force every subsequent conn.execute() call (including PRAGMA optimize and the
        # WAL-checkpoint pragma) to fail by closing the underlying connection out from
        # under close_connection first.
        conn.close()
        try:
            close_connection(conn)  # must not raise, even though conn is already closed
        except Exception as e:
            self.fail(f"close_connection raised unexpectedly on an already-closed connection: {e}")


if __name__ == "__main__":
    unittest.main()
