"""Focused retrieval-text lifecycle and SQLite-only compatibility tests."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import sqlite_vec

from saltmdb.daemon.dispatch import _dispatch_search_memory
from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.embedding_service import (
    _claim_retrieval_embedding_job,
    _persist_retrieval_embedding_if_current,
    _retry_retrieval_embedding_job,
    reconcile_retrieval_embedding_jobs,
)
from saltmdb.domain.services.memory_service import (
    _run_retrieval_fts_search,
    retrieval_vector_search,
    search_memory,
    store_memory,
)
from saltmdb.mcp import tools

_UNSET = object()


class RetrievalTextTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "retrieval.db")
        self.conn = init_db(self.db_path)
        self.librarian = patch("saltmdb.domain.services.librarian_service.trigger_librarian")
        self.librarian.start()

    def tearDown(self):
        self.librarian.stop()
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, *, entity_id=None, title="Retrieval Entity", retrieval_text=_UNSET, **kwargs):
        args = {
            "content": kwargs.pop(
                "content", "An authoritative body that remains independent from retrieval text."
            ),
            "title": title,
            "owner_id": "retrieval-owner",
            "db_connection": self.conn,
            "db_path": self.db_path,
            "skip_duplicate_check": True,
        }
        if entity_id is not None:
            args["entity_id"] = entity_id
        if retrieval_text is not _UNSET:
            args["retrieval_text"] = retrieval_text
        args.update(kwargs)
        return store_memory(**args)

    @staticmethod
    def _id(result: str) -> str:
        return result.split("ID: ", 1)[1].strip()

    def test_tri_state_normalization_redaction_cap_and_hash(self):
        secret = "sk_test_" + ("a" * 20)
        result = self._store(retrieval_text=f"  ＡＢＣ {secret}  ")
        entity_id = self._id(result)
        text, text_hash, content_hash = self.conn.execute(
            "SELECT retrieval_text,retrieval_text_hash,content_hash FROM entities WHERE id=?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(text, "ABC [REDACTED_SECRET]")
        self.assertEqual(text_hash, hashlib.sha256(text.encode()).hexdigest())
        self.assertTrue(content_hash)

        preserved = self._store(entity_id=entity_id)
        self.assertIn("stored successfully", preserved)
        self.assertEqual(
            self.conn.execute(
                "SELECT retrieval_text FROM entities WHERE id=?", (entity_id,)
            ).fetchone()[0],
            text,
        )
        self.assertIn(
            "ambiguous",
            self._store(entity_id=entity_id, retrieval_text=None),
        )
        self._store(entity_id=entity_id, retrieval_text="")
        self.assertIsNone(
            self.conn.execute(
                "SELECT retrieval_text,retrieval_text_hash FROM entities WHERE id=?",
                (entity_id,),
            ).fetchone()[0]
        )
        too_long = self._store(entity_id=entity_id, retrieval_text="x" * 4001)
        self.assertIn("maximum length", too_long)
        unicode_too_long = self._store(entity_id=entity_id, retrieval_text="🙂" * 4001)
        self.assertIn("maximum length", unicode_too_long)

    def test_retrieval_update_preserves_authoritative_hash_and_base_jobs(self):
        result = self._store(retrieval_text="initial retrieval phrase")
        entity_id = self._id(result)
        self.conn.execute("UPDATE entities SET embedding_status='ready' WHERE id=?", (entity_id,))
        self.conn.execute(
            "UPDATE embedding_jobs SET state='succeeded',completed_at=CURRENT_TIMESTAMP "
            "WHERE entity_id=?",
            (entity_id,),
        )
        before = self.conn.execute(
            "SELECT full_content,content_hash,embedding_status FROM entities WHERE id=?",
            (entity_id,),
        ).fetchone()
        base_jobs = self.conn.execute(
            "SELECT job_kind,source_hash,state FROM embedding_jobs WHERE entity_id=? "
            "ORDER BY job_kind",
            (entity_id,),
        ).fetchall()

        self._store(entity_id=entity_id, retrieval_text="replacement phrase")
        after = self.conn.execute(
            "SELECT full_content,content_hash,embedding_status FROM entities WHERE id=?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(
            self.conn.execute(
                "SELECT job_kind,source_hash,state FROM embedding_jobs WHERE entity_id=? "
                "ORDER BY job_kind",
                (entity_id,),
            ).fetchall(),
            base_jobs,
        )

    def test_fts_vector_job_lifecycle_and_stale_exclusion(self):
        result = self._store(retrieval_text="needle phrase only in retrieval text")
        entity_id = self._id(result)
        text_hash = self.conn.execute(
            "SELECT retrieval_text_hash FROM entities WHERE id=?", (entity_id,)
        ).fetchone()[0]
        self.assertEqual(
            _run_retrieval_fts_search(
                self.conn,
                "needle phrase",
                ["e.status != 'archived'"],
                [],
                10,
                0,
            )[0][0],
            entity_id,
        )
        with patch.object(embedding_service, "embed_text", return_value=[1.0] + [0.0] * 383):
            self.assertEqual(
                retrieval_vector_search("needle", ["e.status != 'archived'"], [], 10, self.db_path),
                [],
            )
        snapshot = _claim_retrieval_embedding_job(self.conn)
        _persist_retrieval_embedding_if_current(self.conn, snapshot, [1.0] + [0.0] * 383)
        with patch.object(embedding_service, "embed_text", return_value=[1.0] + [0.0] * 383):
            fresh = retrieval_vector_search(
                "needle", ["e.status != 'archived'"], [], 10, self.db_path
            )
        self.assertEqual(fresh[0][0], entity_id)

        # The old vector/job remains physically present, but hash equality and succeeded-job
        # matching synchronously exclude it after the caller replaces the source.
        new_hash = hashlib.sha256(b"new retrieval source").hexdigest()
        self.conn.execute(
            "UPDATE entities SET retrieval_text='new retrieval source',retrieval_text_hash=? "
            "WHERE id=?",
            (new_hash, entity_id),
        )
        self.conn.commit()
        with patch.object(embedding_service, "embed_text", return_value=[1.0] + [0.0] * 383):
            self.assertEqual(
                retrieval_vector_search("needle", ["e.status != 'archived'"], [], 10, self.db_path),
                [],
            )
        self.assertEqual(text_hash != new_hash, True)

    def test_archive_and_delete_maintain_fts_vector_and_jobs(self):
        result = self._store(retrieval_text="archive then delete")
        entity_id = self._id(result)
        snapshot = _claim_retrieval_embedding_job(self.conn)
        _persist_retrieval_embedding_if_current(self.conn, snapshot, [1.0] + [0.0] * 383)
        from saltmdb.domain.services.memory_service import archive_memory

        archive_memory(entity_id=entity_id, owner_id="retrieval-owner", db_connection=self.conn)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM retrieval_fts WHERE id=?", (entity_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM retrieval_embeddings WHERE entity_id=?", (entity_id,)
            ).fetchone()[0],
            0,
        )
        self.conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM retrieval_fts WHERE id=?", (entity_id,)
            ).fetchone()[0],
            0,
        )

    def test_job_lease_retry_failure_and_reconciliation(self):
        result = self._store(retrieval_text="job lifecycle text")
        entity_id = self._id(result)
        snapshot = _claim_retrieval_embedding_job(self.conn)
        self.conn.execute(
            "UPDATE retrieval_embedding_jobs SET attempt_count=5,lease_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), snapshot["id"]),
        )
        self.assertIsNone(_claim_retrieval_embedding_job(self.conn))
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM retrieval_embedding_jobs WHERE id=?", (snapshot["id"],)
            ).fetchone()[0],
            "failed",
        )

        self._store(entity_id=entity_id, retrieval_text="retry lifecycle text")
        retry_snapshot = _claim_retrieval_embedding_job(self.conn)
        _retry_retrieval_embedding_job(self.conn, retry_snapshot["id"], "temporary")
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM retrieval_embedding_jobs WHERE id=?",
                (retry_snapshot["id"],),
            ).fetchone()[0],
            "retry_wait",
        )
        self.conn.execute(
            "UPDATE retrieval_embedding_jobs SET state='running',attempt_count=5 WHERE id=?",
            (retry_snapshot["id"],),
        )
        _retry_retrieval_embedding_job(self.conn, retry_snapshot["id"], "terminal")
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM retrieval_embedding_jobs WHERE id=?",
                (retry_snapshot["id"],),
            ).fetchone()[0],
            "failed",
        )

        # A successful vector/job is preserved by bounded startup reconciliation.
        self._store(entity_id=entity_id, retrieval_text="successful reconciliation text")
        current = _claim_retrieval_embedding_job(self.conn)
        _persist_retrieval_embedding_if_current(self.conn, current, [1.0] + [0.0] * 383)
        self.assertEqual(reconcile_retrieval_embedding_jobs(self.conn), [entity_id])
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM retrieval_embedding_jobs WHERE id=?", (current["id"],)
            ).fetchone()[0],
            "succeeded",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM retrieval_embeddings WHERE entity_id=?", (entity_id,)
            ).fetchone()[0],
            1,
        )

    def test_search_diagnostics_exclude_retrieval_text_and_forwarding(self):
        result = self._store(retrieval_text="diagnostic hidden retrieval phrase")
        entity_id = self._id(result)
        snapshot = _claim_retrieval_embedding_job(self.conn)
        _persist_retrieval_embedding_if_current(self.conn, snapshot, [1.0] + [0.0] * 383)
        with (
            patch("saltmdb.domain.services.memory_service.semantic_search", return_value=[]),
            patch.object(embedding_service, "embed_text", return_value=[1.0] + [0.0] * 383),
        ):
            result = search_memory(
                query_keywords="diagnostic hidden retrieval phrase",
                use_retrieval_text_candidates=True,
                return_diagnostics=True,
                db_connection=self.conn,
                db_path=self.db_path,
                limit=5,
            )
        self.assertEqual(result["results"][0]["id"], entity_id)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("diagnostic hidden retrieval phrase", serialized)
        self.assertIn("candidate_evidence", serialized)

        backend = MagicMock()
        backend.call.return_value = []
        with patch("saltmdb.mcp.tools._backend_or_raise", return_value=backend):
            tools.search_memory(
                query_keywords="q",
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=0.5,
                retrieval_vector_weight=1.5,
            )
        payload = backend.call.call_args.args[1]
        self.assertTrue(payload["use_retrieval_text_candidates"])
        self.assertEqual(payload["retrieval_fts_weight"], 0.5)
        self.assertEqual(payload["retrieval_vector_weight"], 1.5)

        with patch(
            "saltmdb.daemon.dispatch.memory_service.search_memory", return_value=[]
        ) as search:
            _dispatch_search_memory(
                query_keywords="q",
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=0.5,
                retrieval_vector_weight=1.5,
            )
        self.assertTrue(search.call_args.kwargs["use_retrieval_text_candidates"])

    def test_sqlite_only_vector_init_keeps_store_update_archive_delete_working(self):
        path = os.path.join(self.temp_dir, "sqlite-only.db")
        unavailable = sqlite3.OperationalError("no such module: vec0")
        with (
            patch("saltmdb.db.vector_schema.init_vector_schema", side_effect=unavailable),
            patch(
                "saltmdb.db.vector_schema.init_entity_chunk_vector_schema", side_effect=unavailable
            ),
            patch("saltmdb.db.vector_schema.init_retrieval_vector_schema", side_effect=unavailable),
        ):
            conn = init_db(path)
        try:
            result = store_memory(
                content="A sufficiently descriptive SQLite-only body for testing.",
                title="SQLite Only Retrieval",
                owner_id="sqlite-owner",
                retrieval_text="sqlite-only candidate",
                db_connection=conn,
                db_path=path,
                skip_duplicate_check=True,
            )
            entity_id = self._id(result)
            self.assertIn(
                "stored successfully",
                store_memory(
                    content="A sufficiently descriptive SQLite-only body for testing.",
                    title="SQLite Only Retrieval",
                    owner_id="sqlite-owner",
                    entity_id=entity_id,
                    retrieval_text="updated candidate",
                    db_connection=conn,
                    db_path=path,
                    skip_duplicate_check=True,
                ),
            )
            from saltmdb.domain.services.memory_service import archive_memory

            self.assertIn(
                "successfully archived",
                archive_memory(entity_id, owner_id="sqlite-owner", db_connection=conn),
            )
            conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))
            conn.commit()
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='retrieval_embeddings'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_production_init_has_retrieval_vec_table_and_self_opened_reads_load_extension(self):
        table = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='retrieval_embeddings'"
        ).fetchone()
        self.assertIsNotNone(table)
        self.assertIn("vec0", (table[0] or "").lower())
        reopened = sqlite3.connect(self.db_path)
        try:
            reopened.enable_load_extension(True)
            sqlite_vec.load(reopened)
            reopened.enable_load_extension(False)
            self.assertEqual(
                reopened.execute(
                    "SELECT name FROM sqlite_master WHERE name='retrieval_embeddings'"
                ).fetchone()[0],
                "retrieval_embeddings",
            )
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
