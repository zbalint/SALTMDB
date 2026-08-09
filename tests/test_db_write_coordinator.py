import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from saltmdb.daemon.db_write_coordinator import CoordinatorUsageError, DbWriteCoordinator


class DbWriteCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "writer.db")
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.close()
        self.coordinator = DbWriteCoordinator(self.path)
        self.coordinator.start()

    def tearDown(self):
        self.coordinator.shutdown(2)
        self.tmp.cleanup()

    def test_one_writer_and_rollback_isolation(self):
        writer_ids = set()

        def write(v):
            def job(conn):
                writer_ids.add(threading.get_ident())
                conn.execute("INSERT INTO t VALUES (?)", (v,))
            return job

        threads = [threading.Thread(target=lambda i=i: self.coordinator.submit("write", write(str(i)))) for i in range(12)]
        for t in threads: t.start()
        for t in threads: t.join()
        with self.assertRaises(sqlite3.OperationalError):
            self.coordinator.submit("bad", lambda c: c.execute("INSERT INTO missing VALUES (1)"))
        self.coordinator.submit("good", write("good"))
        self.assertEqual(writer_ids, {self.coordinator.writer_thread_id})
        self.assertEqual(sqlite3.connect(self.path).execute("SELECT COUNT(*) FROM t").fetchone()[0], 13)

    def test_nested_reuses_transaction_and_lanes_are_fair(self):
        order = []
        gate = threading.Event()

        def blocker(conn):
            gate.wait(1)
        future = self.coordinator.submit("block", blocker, wait=False)
        for i in range(9):
            self.coordinator.submit(f"fg{i}", lambda c, i=i: order.append(f"f{i}"), wait=False)
        self.coordinator.submit("bg", lambda c: order.append("b"), priority="background", wait=False)
        gate.set(); future.result()
        deadline = time.time() + 2
        while len(order) < 10 and time.time() < deadline:
            time.sleep(.01)
        # The already-running blocker counts as the first foreground slot.
        self.assertEqual(order[:7], [f"f{i}" for i in range(7)])
        self.assertEqual(order[7], "b")
        nested = self.coordinator.submit("nested", lambda c: self.coordinator.submit("inner", lambda x: x is c))
        self.assertTrue(nested)

    def test_drain_rejects_foreground_and_telemetry(self):
        self.coordinator.begin_draining()
        with self.assertRaises(CoordinatorUsageError):
            self.coordinator.submit("late", lambda c: None)
        info = self.coordinator.telemetry()
        self.assertIn("writer_thread_id", info)
        self.assertEqual(info["queue_depth"], 0)
