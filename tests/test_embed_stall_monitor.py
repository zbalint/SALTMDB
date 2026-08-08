"""Tests for the H6 periodic stale-pending embedding visibility monitor."""

import os
import shutil
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from saltmdb import config
from saltmdb.daemon.embed_stall_monitor import EmbedStallMonitor
from saltmdb.db.schema import init_db


class EmbedStallMonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_pending_entity(self, entity_id: str, age_seconds: float = 0) -> None:
        created_at = (datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "INSERT INTO entities "
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title, "
            "full_content, content_hash, memory_type, embedding_status) "
            "VALUES (?, ?, ?, ?, 'test_user', 'raw', ?, ?, ?, 'fact', 'pending')",
            (entity_id, created_at, created_at, created_at, entity_id, f"content for {entity_id}", entity_id),
        )
        self.conn.commit()

    def _resolve_entity(self, entity_id: str) -> None:
        self.conn.execute("UPDATE entities SET embedding_status = 'ready' WHERE id = ?", (entity_id,))
        self.conn.commit()


class TestStalePendingQuery(EmbedStallMonitorTestCase):
    def test_ignores_fresh_pending_under_threshold(self):
        with mock.patch.object(config, "EMBED_STALL_PENDING_AGE_THRESHOLD_S", 3600):
            self._insert_pending_entity("fresh")
            count, oldest_id, _ = EmbedStallMonitor(self.db_path)._query_stale_pending()
        self.assertEqual(count, 0)
        self.assertIsNone(oldest_id)

    def test_counts_stale_and_reports_oldest_first(self):
        with mock.patch.object(config, "EMBED_STALL_PENDING_AGE_THRESHOLD_S", 3600):
            self._insert_pending_entity("newer_stale", age_seconds=7200)
            self._insert_pending_entity("older_stale", age_seconds=10800)
            count, oldest_id, _ = EmbedStallMonitor(self.db_path)._query_stale_pending()
        self.assertEqual(count, 2)
        self.assertEqual(oldest_id, "older_stale")

    def test_resolved_entity_is_not_reported(self):
        self._insert_pending_entity("will_resolve", age_seconds=3600)
        monitor = EmbedStallMonitor(self.db_path)
        self.assertEqual(monitor._query_stale_pending()[0], 1)
        self._resolve_entity("will_resolve")
        self.assertEqual(monitor._query_stale_pending(), (0, None, None))

    def test_check_logs_diagnostic_warning_without_requesting_shutdown(self):
        self._insert_pending_entity("stuck", age_seconds=3600)
        monitor = EmbedStallMonitor(self.db_path)
        with self.assertLogs("saltmdb.daemon.embed_stall_monitor", level="WARNING") as logs:
            monitor._check_once()
        self.assertTrue(any("stuck embedding_status='pending'" in line for line in logs.output))
        self.assertTrue(any("oldest=stuck" in line for line in logs.output))


class TestRealTimerThread(EmbedStallMonitorTestCase):
    def test_start_stop_drives_the_real_periodic_loop(self):
        with mock.patch.object(config, "EMBED_STALL_CHECK_INTERVAL_S", 0.05):
            monitor = EmbedStallMonitor(self.db_path)
            ticks = []
            original_check_once = monitor._check_once

            def counting_check_once():
                ticks.append(True)
                original_check_once()

            monitor._check_once = counting_check_once
            monitor.start()
            time.sleep(0.3)
            monitor.stop()
            monitor._thread.join(timeout=2)

        self.assertGreaterEqual(len(ticks), 2)
        self.assertFalse(monitor._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
