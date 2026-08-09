import tempfile
import unittest
from pathlib import Path

from saltmdb.db.schema import init_db
from saltmdb.domain.services.embedding_service import (
    enqueue_embedding_jobs_for_entity,
    reconcile_embedding_jobs,
)
from saltmdb.utils.text import compute_content_hash


class EmbeddingJobReconciliationTests(unittest.TestCase):
    def test_reconcile_preserves_current_succeeded_jobs_and_ready_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = init_db(str(Path(tmp) / "db.sqlite"))
            content = "durable body"
            content_hash = compute_content_hash(content)
            conn.execute(
                "INSERT INTO entities (id,created_at,updated_at,last_accessed_at,status,title,full_content,content_hash,embedding_status) "
                "VALUES ('e','2020','2020','2020','raw','Durable title',?,?, 'ready')",
                (content, content_hash),
            )
            enqueue_embedding_jobs_for_entity(conn, "e", "Durable title", content, content_hash)
            conn.execute("UPDATE embedding_jobs SET state='succeeded'")
            conn.execute("UPDATE entities SET embedding_status='ready' WHERE id='e'")
            reconcile_embedding_jobs(conn)
            self.assertEqual(conn.execute("SELECT embedding_status FROM entities WHERE id='e'").fetchone()[0], "ready")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embedding_jobs WHERE state='succeeded'").fetchone()[0], 2)
            conn.close()
