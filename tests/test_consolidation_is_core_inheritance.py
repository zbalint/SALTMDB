import unittest
import tempfile
import os
import shutil
from unittest.mock import patch
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation, bulk_commit_consolidation


class TestConsolidationIsCoreInheritance(unittest.TestCase):
    """Regression coverage for commit_consolidation silently dropping is_core=1 on parents.

    Previously the INSERT hardcoded is_core=0, so consolidating a core memory with any other
    memory archived the core parent and replaced it with a non-core consolidated entity --
    losing core status with no way to set it back short of a manual DB edit.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_cons_is_core.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        # Prevent this test's store_memory/commit_consolidation calls from queuing real
        # background embedding jobs on the shared module-level thread pool -- those jobs
        # can still be draining when an unrelated later test mocks embed_text and counts
        # calls, causing order-dependent flakes elsewhere in the suite.
        self._embed_patcher = patch("saltmdb.domain.services.embedding_service.embed_entity_async")
        self._embed_patcher.start()
        self.addCleanup(self._embed_patcher.stop)

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _is_core(self, entity_id):
        row = self.conn.execute(
            "SELECT is_core FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return bool(row[0]) if row else None

    def _has_core_tag(self, entity_id):
        row = self.conn.execute(
            """
            SELECT 1 FROM entity_tags et JOIN tags t ON et.tag_id = t.id
            WHERE et.entity_id = ? AND t.name = '#core'
            """,
            (entity_id,),
        ).fetchone()
        return row is not None

    def test_core_parent_status_is_inherited_by_default(self):
        core_parent = store_memory(
            title="Core Rule Parent",
            content="A core operational rule that must survive consolidation intact.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
        ).split("ID: ")[1]
        self.assertTrue(self._is_core(core_parent))

        plain_parent = store_memory(
            title="Plain Fact Parent",
            content="An ordinary non-core fact used alongside the core rule.",
            owner_id="agent_c",
            db_connection=self.conn,
        ).split("ID: ")[1]

        res = commit_consolidation(
            parent_ids=[core_parent, plain_parent],
            title="Consolidated Core+Plain",
            content="Synthesized content merging the core rule with the plain fact.",
            owner_id="agent_c",
            db_connection=self.conn,
            # Real cosine similarity between these two parents' content sits close to the
            # cohesion gate's threshold (~0.68 vs a 0.60 placeholder pending benchmark lock) --
            # this test cares about is_core inheritance, not the cohesion gate, so an explicit
            # override keeps it from becoming fragile once the real threshold is locked in.
            override_justification="pre-existing test fixture, not exercising the cohesion gate",
        )
        self.assertIn("Successfully committed consolidated memory with ID:", res)
        consolidated_id = res.split("ID: ")[1].strip()

        self.assertTrue(self._is_core(consolidated_id))
        self.assertTrue(self._has_core_tag(consolidated_id))

    def test_non_core_parents_stay_non_core(self):
        # skip_duplicate_check=True: these two fixtures are deliberately near-identical
        # templated content (this test is about is_core inheritance, not dedup behavior) and
        # would otherwise trip Track A's store-time disposition preflight against each other.
        p1 = store_memory(
            title="Plain Fact One",
            content="An ordinary non-core fact, part one.",
            owner_id="agent_c",
            skip_duplicate_check=True,
            db_connection=self.conn,
        ).split("ID: ")[1]
        p2 = store_memory(
            title="Plain Fact Two",
            content="An ordinary non-core fact, part two.",
            owner_id="agent_c",
            skip_duplicate_check=True,
            db_connection=self.conn,
        ).split("ID: ")[1]

        res = commit_consolidation(
            parent_ids=[p1, p2],
            title="Consolidated Plain",
            content="Synthesized content merging two plain, non-core facts.",
            owner_id="agent_c",
            db_connection=self.conn,
        )
        consolidated_id = res.split("ID: ")[1].strip()
        self.assertFalse(self._is_core(consolidated_id))
        self.assertFalse(self._has_core_tag(consolidated_id))

    def test_explicit_is_core_overrides_inheritance(self):
        core_parent = store_memory(
            title="Core Rule Parent Two",
            content="Another core operational rule for the override test.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
        ).split("ID: ")[1]

        res = commit_consolidation(
            parent_ids=[core_parent],
            title="Deliberately Demoted Consolidation",
            content="Synthesized content where the caller explicitly demotes core status.",
            owner_id="agent_c",
            is_core=False,
            db_connection=self.conn,
        )
        consolidated_id = res.split("ID: ")[1].strip()
        self.assertFalse(self._is_core(consolidated_id))

    def test_bulk_consolidation_inherits_is_core(self):
        core_parent = store_memory(
            title="Bulk Core Rule Parent",
            content="A core rule consolidated via the bulk pathway.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
        ).split("ID: ")[1]

        results = bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [core_parent],
                    "title": "Bulk Consolidated Core",
                    "content": "Synthesized content for the bulk core consolidation test.",
                }
            ],
            db_connection=self.conn,
        )
        self.assertEqual(results[0]["status"], "success")
        self.assertTrue(self._is_core(results[0]["entity_id"]))


if __name__ == "__main__":
    unittest.main()
