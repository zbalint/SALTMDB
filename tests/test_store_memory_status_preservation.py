import unittest
import tempfile
import os
import shutil
from unittest.mock import patch
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, archive_memory
from saltmdb.domain.services.relation_service import commit_consolidation


class TestStoreMemoryStatusPreservation(unittest.TestCase):
    """Regression coverage for the 'status resurrection' landmine.

    entities' ON CONFLICT upsert used to hardcode `status = excluded.status` and
    `valid_to = NULL` unconditionally. Since the INSERT half's literal status is always
    'raw', ANY store_memory call against an existing consolidated/archived entity (even
    just to patch is_core) silently reset it back to status='raw' and cleared valid_to,
    corrupting consolidation/archival lineage. Known and documented but deliberately left
    unfixed in a prior session; fixed here via preserving entities.status/valid_to on update.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_status_preservation.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        # See test_consolidation_is_core_inheritance.py for why this is patched out:
        # avoids leaking real background embedding jobs into unrelated tests' mock counts.
        self._embed_patcher = patch(
            "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"
        )
        self._embed_patcher.start()
        self.addCleanup(self._embed_patcher.stop)

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _row(self, entity_id):
        return self.conn.execute(
            "SELECT status, valid_to, is_core FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()

    def test_patching_is_core_on_archived_entity_preserves_status_and_valid_to(self):
        p1 = store_memory(
            title="Archived Candidate",
            content="Content that will be archived and then metadata-patched.",
            owner_id="agent_c",
            db_connection=self.conn,
        ).split("ID: ")[1]

        archive_memory(entity_id=p1, owner_id="agent_c", db_connection=self.conn)
        status, valid_to, _ = self._row(p1)
        self.assertEqual(status, "archived")
        self.assertIsNotNone(valid_to)

        store_memory(
            entity_id=p1,
            title="Archived Candidate",
            content="Content that will be archived and then metadata-patched.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
        )

        status, valid_to, is_core = self._row(p1)
        self.assertEqual(status, "archived")
        self.assertIsNotNone(valid_to)
        self.assertTrue(is_core)

    def test_patching_is_core_on_consolidated_entity_preserves_status_and_valid_to(self):
        p1 = store_memory(
            title="Consolidation Source",
            content="Raw source memory that will be folded into a consolidation.",
            owner_id="agent_c",
            db_connection=self.conn,
        ).split("ID: ")[1]

        res = commit_consolidation(
            parent_ids=[p1],
            title="Consolidated Result",
            content="Synthesized content produced from the raw source memory.",
            owner_id="agent_c",
            db_connection=self.conn,
        )
        consolidated_id = res.split("ID: ")[1].strip()
        status, valid_to, _ = self._row(consolidated_id)
        self.assertEqual(status, "consolidated")

        store_memory(
            entity_id=consolidated_id,
            title="Consolidated Result",
            content="Synthesized content produced from the raw source memory.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
        )

        status, valid_to, is_core = self._row(consolidated_id)
        self.assertEqual(status, "consolidated")
        self.assertTrue(is_core)

    def test_raw_entity_update_is_unaffected(self):
        p1 = store_memory(
            title="Raw Entry",
            content="Original raw content.",
            owner_id="agent_c",
            db_connection=self.conn,
        ).split("ID: ")[1]

        store_memory(
            entity_id=p1,
            title="Raw Entry Updated",
            content="Updated raw content.",
            owner_id="agent_c",
            db_connection=self.conn,
        )

        status, valid_to, _ = self._row(p1)
        self.assertEqual(status, "raw")
        self.assertIsNone(valid_to)


if __name__ == "__main__":
    unittest.main()
