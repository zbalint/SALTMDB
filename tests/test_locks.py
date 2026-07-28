import unittest
import tempfile
import os
import shutil
from datetime import datetime, timedelta, UTC

from saltmdb.config import LIBRARIAN_LOCK_STALE_MINUTES
from saltmdb.db.connection import get_connection, close_connection
from saltmdb.db.locks import acquire_librarian_lock, release_librarian_lock
from saltmdb.db.schema import init_db


class TestLibrarianLock(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_lock_row(self, conn):
        return conn.execute(
            "SELECT locked_at, locked_by_pid, last_run_at FROM _system_locks "
            "WHERE task_name = 'librarian_consolidation'"
        ).fetchone()

    def test_acquire_succeeds_when_unlocked(self):
        self.assertTrue(acquire_librarian_lock(self.conn))
        locked_at, locked_by_pid, _ = self._get_lock_row(self.conn)
        self.assertIsNotNone(locked_at)
        self.assertEqual(locked_by_pid, os.getpid())

    def test_acquire_fails_when_already_locked(self):
        # First connection acquires the lock and does NOT release it.
        self.assertTrue(acquire_librarian_lock(self.conn))

        # A second, independent connection to the same DB must be refused --
        # the lock row is still fresh (locked_at recent, not past the stale window).
        second_conn = get_connection(self.db_path)
        try:
            self.assertFalse(acquire_librarian_lock(second_conn))
        finally:
            close_connection(second_conn)

    def test_acquire_succeeds_after_stale_window_elapsed(self):
        self.assertTrue(acquire_librarian_lock(self.conn))

        # Backdate locked_at to simulate a lock that has gone stale (e.g. the process that
        # held it crashed without releasing), rather than actually waiting real minutes.
        stale_timestamp = (
            datetime.now(UTC) - timedelta(minutes=LIBRARIAN_LOCK_STALE_MINUTES + 1)
        ).isoformat()
        self.conn.execute(
            "UPDATE _system_locks SET locked_at = ? WHERE task_name = 'librarian_consolidation'",
            (stale_timestamp,),
        )

        second_conn = get_connection(self.db_path)
        try:
            self.assertTrue(acquire_librarian_lock(second_conn))
        finally:
            close_connection(second_conn)

    def test_release_clears_lock_and_stamps_last_run_at(self):
        self.assertTrue(acquire_librarian_lock(self.conn))
        release_librarian_lock(self.conn)

        locked_at, locked_by_pid, last_run_at = self._get_lock_row(self.conn)
        self.assertIsNone(locked_at)
        self.assertIsNone(locked_by_pid)
        self.assertIsNotNone(last_run_at)

    def test_acquire_leaves_no_dangling_open_transaction(self):
        acquire_librarian_lock(self.conn)
        # Regression check: proves write_transaction_retrying actually committed
        # (BEGIN IMMEDIATE ... COMMIT) rather than leaving the transaction open.
        self.assertFalse(self.conn.in_transaction)

    def test_release_leaves_no_dangling_open_transaction(self):
        acquire_librarian_lock(self.conn)
        release_librarian_lock(self.conn)
        self.assertFalse(self.conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
