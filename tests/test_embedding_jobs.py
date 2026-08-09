import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import UTC, datetime, timedelta

from saltmdb.db.schema import init_db
from saltmdb.domain.services.embedding_service import (
    cancel_embedding_jobs_for_entity,
    _claim_embedding_job,
    _persist_embedding_if_current,
    _retry_embedding_job,
    EmbedJobScheduler,
    enqueue_embedding_jobs_for_entity,
    entity_source_hash,
    reconcile_embedding_jobs,
)
from saltmdb.utils.text import compute_content_hash


class EmbeddingJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "jobs.db")
        self.conn = init_db(self.path)
        self.conn.execute(
            "INSERT INTO entities (id,created_at,updated_at,last_accessed_at,status,title,full_content,content_hash) "
            "VALUES ('e','2020','2020','2020','raw','Title','Body',?)", (compute_content_hash("Body"),)
        )

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_enqueue_is_atomic_and_replaces_old_source(self):
        content_hash = compute_content_hash("Body")
        enqueue_embedding_jobs_for_entity(self.conn, "e", "Title", "Body", content_hash)
        rows = self.conn.execute("SELECT job_kind,source_hash,state FROM embedding_jobs ORDER BY job_kind").fetchall()
        self.assertEqual(rows, [("chunk", content_hash, "queued"), ("entity", entity_source_hash("Title", "Body"), "queued")])
        enqueue_embedding_jobs_for_entity(self.conn, "e", "New", "Body", content_hash)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embedding_jobs WHERE state='cancelled'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT embedding_status FROM entities WHERE id='e'").fetchone()[0], "pending")

    def test_archive_cancel_and_legacy_reconciliation(self):
        ids = reconcile_embedding_jobs(self.conn)
        self.assertEqual(ids, ["e"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0], 2)
        cancel_embedding_jobs_for_entity(self.conn, "e")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM embedding_jobs WHERE state='cancelled'").fetchone()[0], 2)

    def test_claim_is_unique_then_expired_fifth_lease_becomes_terminal(self):
        enqueue_embedding_jobs_for_entity(self.conn, "e", "Title", "Body", compute_content_hash("Body"))
        first = _claim_embedding_job(self.conn)
        self.assertIsNotNone(first)
        # A second scheduler cannot claim the same running job.
        self.assertIsNotNone(_claim_embedding_job(self.conn))  # the other kind is independently claimable
        self.assertIsNone(_claim_embedding_job(self.conn))
        self.conn.execute(
            "UPDATE embedding_jobs SET attempt_count=5, lease_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), first["id"]),
        )
        _claim_embedding_job(self.conn)
        self.assertEqual(self.conn.execute("SELECT state FROM embedding_jobs WHERE id=?", (first["id"],)).fetchone()[0], "failed")

    def test_retry_backoff_and_terminal_failure(self):
        enqueue_embedding_jobs_for_entity(self.conn, "e", "Title", "Body", compute_content_hash("Body"))
        snapshot = _claim_embedding_job(self.conn)
        _retry_embedding_job(self.conn, snapshot["id"], "temporary")
        state, attempts, due, error = self.conn.execute(
            "SELECT state,attempt_count,next_attempt_at,last_error FROM embedding_jobs WHERE id=?", (snapshot["id"],)
        ).fetchone()
        self.assertEqual((state, attempts, error), ("retry_wait", 1, "temporary"))
        self.assertIsNotNone(due)
        self.conn.execute("UPDATE embedding_jobs SET state='running',attempt_count=5 WHERE id=?", (snapshot["id"],))
        _retry_embedding_job(self.conn, snapshot["id"], "terminal")
        self.assertEqual(self.conn.execute("SELECT state FROM embedding_jobs WHERE id=?", (snapshot["id"],)).fetchone()[0], "failed")

    def test_stale_result_cannot_overwrite_current_source(self):
        old_hash = compute_content_hash("Body")
        enqueue_embedding_jobs_for_entity(self.conn, "e", "Title", "Body", old_hash)
        snapshot = _claim_embedding_job(self.conn)
        # Claim entity job only; title edit makes the captured entity source stale.
        if snapshot["job_kind"] != "entity":
            snapshot = _claim_embedding_job(self.conn)
        self.conn.execute("UPDATE entities SET title='New title' WHERE id='e'")
        _persist_embedding_if_current(self.conn, snapshot, [0.0] * 384)
        self.assertEqual(self.conn.execute("SELECT state FROM embedding_jobs WHERE id=?", (snapshot["id"],)).fetchone()[0], "cancelled")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entity_embeddings WHERE entity_id='e'").fetchone()[0], 0)

    def test_committed_jobs_survive_pre_dispatch_crash_boundary(self):
        enqueue_embedding_jobs_for_entity(self.conn, "e", "Title", "Body", compute_content_hash("Body"))
        self.conn.commit()  # model process has not been dispatched yet
        reopened = sqlite3.connect(self.path)
        self.assertEqual(reopened.execute("SELECT COUNT(*) FROM embedding_jobs WHERE state='queued'").fetchone()[0], 2)
        reopened.close()

    def test_scheduler_capacity_matches_worker_count(self):
        scheduler = EmbedJobScheduler(coordinator=object())
        self.assertTrue(scheduler._capacity.acquire(blocking=False))
        self.assertTrue(scheduler._capacity.acquire(blocking=False))
        self.assertFalse(scheduler._capacity.acquire(blocking=False))
        scheduler._capacity.release()
        scheduler._capacity.release()
