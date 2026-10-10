"""SPEC-DB-SNAPSHOT-CLI (BL-027): db/backup.py create_snapshot on temp databases only."""

import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from saltmdb.db import backup


def _make_wal_db(path: str, rows: int = 3) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, title TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO entities VALUES (?, ?)", (f"e{i}", f"title {i}"))
    conn.close()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class TestCreateSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.db_path = os.path.join(self.tmp, "live.db")
        _make_wal_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_t1_snapshot_is_self_contained_private_copy(self):
        path = backup.create_snapshot(self.db_path)

        self.assertTrue(os.path.isabs(path))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.dirname(path), os.path.realpath(os.path.join(self.tmp, "backups")))
        self.assertRegex(os.path.basename(path), r"^saltmdb_snapshot_\d{8}_\d{6}\.db$")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(os.path.exists(path + "-wal"))
        self.assertFalse(os.path.exists(path + "-shm"))
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 3)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            conn.close()

    def test_t2_committed_rows_present_open_transaction_rows_absent(self):
        writer = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            writer.execute("INSERT INTO entities VALUES ('committed', 'c')")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO entities VALUES ('pending', 'p')")
            path = backup.create_snapshot(self.db_path, os.path.join(self.tmp, "out"))
        finally:
            writer.execute("ROLLBACK")
            writer.close()

        conn = sqlite3.connect(path)
        try:
            ids = {row[0] for row in conn.execute("SELECT id FROM entities")}
        finally:
            conn.close()
        self.assertIn("committed", ids)
        self.assertNotIn("pending", ids)

    def test_t3_existing_target_raises_and_is_untouched(self):
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        fixed = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        existing = os.path.join(dest, "saltmdb_snapshot_20261010_120000.db")
        with open(existing, "wb") as f:
            f.write(b"existing snapshot bytes")

        with patch("saltmdb.db.backup.datetime") as fake_dt:
            fake_dt.now.return_value = fixed
            with self.assertRaises(FileExistsError):
                backup.create_snapshot(self.db_path, dest)

        self.assertEqual(_read_bytes(existing), b"existing snapshot bytes")

    def test_t4_mid_copy_failure_removes_target_and_journal_and_reraises(self):
        dest = os.path.join(self.tmp, "out")

        class _FailingSource:
            def backup(self, target, **_kw):
                target_path = target.execute("PRAGMA database_list").fetchone()[2]
                with open(target_path + "-journal", "wb") as f:
                    f.write(b"partial")
                raise sqlite3.OperationalError("simulated mid-copy failure")

            def close(self):
                pass

        with patch("saltmdb.db.backup.open_read_connection", return_value=_FailingSource()):
            with self.assertRaises(sqlite3.OperationalError):
                backup.create_snapshot(self.db_path, dest)

        self.assertEqual(os.listdir(dest), [])

    def test_t4_failure_before_create_removes_nothing(self):
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        fixed = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        existing = os.path.join(dest, "saltmdb_snapshot_20261010_120000.db")
        with open(existing, "wb") as f:
            f.write(b"keep me")

        with patch("saltmdb.db.backup.datetime") as fake_dt:
            fake_dt.now.return_value = fixed
            with patch("saltmdb.db.backup.open_read_connection") as opener:
                with self.assertRaises(FileExistsError):
                    backup.create_snapshot(self.db_path, dest)
        opener.assert_not_called()
        self.assertEqual(_read_bytes(existing), b"keep me")
        self.assertEqual(os.listdir(dest), ["saltmdb_snapshot_20261010_120000.db"])

    def test_t5_missing_dest_dir_created_private(self):
        dest = os.path.join(self.tmp, "fresh")
        path = backup.create_snapshot(self.db_path, dest)
        self.assertEqual(os.path.dirname(path), os.path.realpath(dest))
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)

    def test_t5_existing_dest_dir_keeps_its_mode(self):
        dest = os.path.join(self.tmp, "shared")
        os.makedirs(dest)
        os.chmod(dest, 0o755)
        path = backup.create_snapshot(self.db_path, dest)
        self.assertEqual(os.path.dirname(path), os.path.realpath(dest))
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o755)

    def test_t6_target_equal_to_live_db_raises_and_writes_nothing(self):
        fixed = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        live = os.path.join(self.tmp, "saltmdb_snapshot_20261010_120000.db")
        _make_wal_db(live)
        before = _read_bytes(live)
        listing = sorted(os.listdir(self.tmp))

        with patch("saltmdb.db.backup.datetime") as fake_dt:
            fake_dt.now.return_value = fixed
            with self.assertRaises(ValueError):
                backup.create_snapshot(live, self.tmp)

        self.assertTrue(os.path.isfile(live))
        self.assertEqual(_read_bytes(live), before)
        self.assertEqual(sorted(os.listdir(self.tmp)), listing)


if __name__ == "__main__":
    unittest.main()
