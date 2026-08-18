import unittest
import tempfile
import os
import shutil
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation, bulk_commit_consolidation


class TestConsolidationIsCoreInheritance(unittest.TestCase):
    """Core-memory bootstrap governance rewrite: commit_consolidation must NEVER silently inherit
    or drop is_core from parents (see plans/core_memory_bootstrap_governance_detailed.md rules
    52-53 and core_governance_service.resolve_consolidation_core_state). This supersedes the old
    regression coverage for the opposite bug (is_core=1 silently dropped to 0) -- that bug's own
    fix is now baked into resolve_consolidation_core_state's explicit-is_core-required behavior,
    so a bare inheritance test would just be asserting the wrong contract."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_cons_is_core.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        # Prevent this test's store_memory/commit_consolidation calls from queuing real
        # background embedding jobs on the shared module-level thread pool -- those jobs
        # can still be draining when an unrelated later test mocks embed_text and counts
        # calls, causing order-dependent flakes elsewhere in the suite.
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

    def _store_core_parent(self, title, content):
        res = store_memory(
            title=title,
            content=content,
            owner_id="agent_c",
            is_core=True,
            core_reason="Test fixture core reason for consolidation-governance regression coverage.",
            core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Knowledge stored successfully"), res)
        return res.split("ID: ")[1]

    def test_omitted_is_core_with_active_core_parent_is_rejected(self):
        core_parent = self._store_core_parent(
            "Core Rule Parent",
            "A core operational rule that must survive consolidation intact.",
        )
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
            override_justification="pre-existing test fixture, not exercising the cohesion gate",
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("is_core was omitted", res)

        # Side-effect-free: the core parent must still be exactly as it was before the call.
        self.assertTrue(self._is_core(core_parent))
        row = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (core_parent,)
        ).fetchone()
        self.assertEqual(row[0], "raw")

    def test_non_core_parents_stay_non_core_when_is_core_omitted(self):
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
        self.assertIn("Successfully committed consolidated memory with ID:", res)
        consolidated_id = res.split("ID: ")[1].strip()
        self.assertFalse(self._is_core(consolidated_id))
        self.assertFalse(self._has_core_tag(consolidated_id))

    def test_explicit_is_core_true_requires_lifecycle_fields(self):
        core_parent = self._store_core_parent(
            "Core Rule Parent Two", "Another core operational rule for the explicit-True test."
        )

        rejected = commit_consolidation(
            parent_ids=[core_parent],
            title="Explicit Core Consolidation Missing Fields",
            content="Synthesized content that tries to stay core without a reason/exit condition.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
        )
        self.assertTrue(rejected.startswith("Error"), rejected)

        accepted = commit_consolidation(
            parent_ids=[core_parent],
            title="Explicit Core Consolidation With Fields",
            content="Synthesized content that stays core with a complete lifecycle declaration.",
            owner_id="agent_c",
            is_core=True,
            core_reason="Test fixture core reason for the explicit-is_core consolidation test.",
            core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed consolidated memory with ID:", accepted)
        consolidated_id = accepted.split("ID: ")[1].strip()
        self.assertTrue(self._is_core(consolidated_id))
        self.assertTrue(self._has_core_tag(consolidated_id))

    def test_explicit_is_core_false_demotes_core_parent(self):
        core_parent = self._store_core_parent(
            "Core Rule Parent Three", "A third core operational rule for the override test."
        )

        res = commit_consolidation(
            parent_ids=[core_parent],
            title="Deliberately Demoted Consolidation",
            content="Synthesized content where the caller explicitly demotes core status.",
            owner_id="agent_c",
            is_core=False,
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed consolidated memory with ID:", res)
        consolidated_id = res.split("ID: ")[1].strip()
        self.assertFalse(self._is_core(consolidated_id))

    def test_bulk_consolidation_also_rejects_omitted_is_core_against_core_parent(self):
        core_parent = self._store_core_parent(
            "Bulk Core Rule Parent", "A core rule consolidated via the bulk pathway."
        )

        results = bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [core_parent],
                    "title": "Bulk Consolidated Core Omitted",
                    "content": "Synthesized content for the bulk core consolidation test.",
                }
            ],
            db_connection=self.conn,
        )
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("is_core was omitted", results[0]["error"])
        # All-or-nothing: the core parent must remain untouched.
        self.assertTrue(self._is_core(core_parent))

    def test_bulk_consolidation_honors_explicit_is_core(self):
        core_parent = self._store_core_parent(
            "Bulk Core Rule Parent Two", "A second core rule consolidated via the bulk pathway."
        )

        results = bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [core_parent],
                    "title": "Bulk Consolidated Core Explicit",
                    "content": "Synthesized content for the bulk core consolidation explicit test.",
                    "is_core": True,
                    "core_reason": "Test fixture core reason for the bulk explicit-is_core test.",
                    "core_exit_condition": "Test fixture exit condition: this regression test tears down its temp DB.",
                }
            ],
            db_connection=self.conn,
        )
        self.assertEqual(results[0]["status"], "success")
        self.assertTrue(self._is_core(results[0]["entity_id"]))


class TestConsolidationOverdueBoundary(unittest.TestCase):
    """Resolved review finding #2: core-producing consolidation previously never checked the
    overdue-write boundary at all. See core_governance_service.enforce_overdue_boundary and its
    new `exclude_ids` parameter."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_cons_overdue.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
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

    def _store_core(self, title, content):
        res = store_memory(
            title=title,
            content=content,
            owner_id="agent_c",
            is_core=True,
            core_reason="Test fixture core reason for consolidation-overdue regression coverage.",
            core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Knowledge stored successfully"), res)
        return res.split("ID: ")[1].strip()

    def _store_plain(self, title, content):
        res = store_memory(
            title=title,
            content=content,
            owner_id="agent_c",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Knowledge stored successfully"), res)
        return res.split("ID: ")[1].strip()

    def _make_overdue(self, entity_id):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        self.conn.execute(
            "UPDATE entities SET core_review_after = ? WHERE id = ?", (past, entity_id)
        )

    def _core_reason_kwargs(self):
        return {
            "core_reason": "Test fixture core reason for consolidation-overdue regression coverage.",
            "core_exit_condition": "Test fixture exit condition: this regression test tears down its temp DB.",
        }

    def test_single_parent_core_consolidation_rejected_while_unrelated_core_overdue(self):
        overdue_id = self._store_core(
            "Unrelated Overdue Core", "Body of the unrelated overdue core."
        )
        self._make_overdue(overdue_id)
        parent_id = self._store_plain(
            "Plain Parent For Single Consolidation", "Plain parent body text."
        )

        res = commit_consolidation(
            parent_ids=[parent_id],
            title="Single Parent Core Consolidation While Overdue",
            content="Synthesized single-parent content that tries to become core while overdue.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
            **self._core_reason_kwargs(),
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("overdue", res)
        # Zero side effects: the plain parent must be untouched.
        row = self.conn.execute("SELECT status FROM entities WHERE id = ?", (parent_id,)).fetchone()
        self.assertEqual(row[0], "raw")

    def test_multi_parent_core_consolidation_rejected_while_unrelated_core_overdue(self):
        overdue_id = self._store_core(
            "Unrelated Overdue Core Two", "Body of another unrelated overdue core."
        )
        self._make_overdue(overdue_id)
        p1 = self._store_plain(
            "Multi Parent A", "First plain parent for multi-parent consolidation."
        )
        p2 = self._store_plain(
            "Multi Parent B", "Second plain parent for multi-parent consolidation."
        )

        res = commit_consolidation(
            parent_ids=[p1, p2],
            title="Multi Parent Core Consolidation While Overdue",
            content="Synthesized multi-parent content that tries to become core while overdue.",
            owner_id="agent_c",
            is_core=True,
            override_justification="pre-existing test fixture, not exercising the cohesion gate",
            db_connection=self.conn,
            **self._core_reason_kwargs(),
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("overdue", res)

    def test_bulk_core_consolidation_rejected_atomically_while_unrelated_core_overdue(self):
        overdue_id = self._store_core(
            "Bulk Unrelated Overdue Core", "Body of the bulk unrelated overdue core."
        )
        self._make_overdue(overdue_id)
        parent_id = self._store_plain("Bulk Plain Parent", "Bulk plain parent body text.")

        results = bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [parent_id],
                    "title": "Bulk Core Consolidation While Overdue",
                    "content": "Synthesized bulk content that tries to become core while overdue.",
                    "is_core": True,
                    **self._core_reason_kwargs(),
                }
            ],
            db_connection=self.conn,
        )
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("overdue", results[0]["error"])
        row = self.conn.execute("SELECT status FROM entities WHERE id = ?", (parent_id,)).fetchone()
        self.assertEqual(row[0], "raw")

    def test_demoting_overdue_parent_to_non_core_remains_allowed(self):
        overdue_id = self._store_core(
            "Overdue Parent Demoted", "Body of the overdue parent being demoted."
        )
        self._make_overdue(overdue_id)

        res = commit_consolidation(
            parent_ids=[overdue_id],
            title="Demote Overdue Parent Via Consolidation",
            content="Synthesized content where the overdue parent becomes a plain memory.",
            owner_id="agent_c",
            is_core=False,
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed consolidated memory with ID:", res)

    def test_core_result_replacing_sole_overdue_parent_rejected(self):
        # Reversed (follow-up review finding #2): a core-producing consolidation must never
        # replace its own sole overdue parent. Excluding resolved parents about to be archived
        # from the overdue scan let a core-producing consolidation silently reset an overdue
        # core's lifecycle without ever recording review provenance through review_core_memory --
        # that exception was never approved. The sanctioned recovery paths remain an explicit
        # non-core consolidation (test_demoting_overdue_parent_to_non_core_remains_allowed) or a
        # prior review_core_memory call (see
        # test_retrying_after_review_of_overdue_parent_admits_core_consolidation below).
        overdue_id = self._store_core(
            "Sole Overdue Parent Not Replaced", "Body of the sole overdue parent staying put."
        )
        self._make_overdue(overdue_id)

        res = commit_consolidation(
            parent_ids=[overdue_id],
            title="Core Result Replacing Sole Overdue Parent Rejected",
            content="Synthesized content that must not be allowed to replace the sole overdue parent.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
            **self._core_reason_kwargs(),
        )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("overdue", res)

        # Zero side effects: the overdue parent remains active core and untouched, and no child
        # entity/relations/events were created for the rejected consolidation.
        parent_row = self.conn.execute(
            "SELECT is_core, status FROM entities WHERE id = ?", (overdue_id,)
        ).fetchone()
        self.assertTrue(parent_row[0])
        self.assertNotEqual(parent_row[1], "archived")
        child_row = self.conn.execute(
            "SELECT id FROM entities WHERE title = ?",
            ("Core Result Replacing Sole Overdue Parent Rejected",),
        ).fetchone()
        self.assertIsNone(child_row)

    def test_retrying_after_review_of_overdue_parent_admits_core_consolidation(self):
        # After an explicit review_core_memory resolves the overdue state, retrying the same
        # core-producing consolidation follows normal admission.
        from saltmdb.domain.services.core_governance_service import review_core_memory

        overdue_id = self._store_core(
            "Overdue Parent Reviewed Then Consolidated",
            "Body of the parent that gets reviewed before being replaced.",
        )
        self._make_overdue(overdue_id)

        review_msg = review_core_memory(
            self.conn,
            entity_id=overdue_id,
            outcome="retain",
            review_rationale="Reviewed during regression test to clear the overdue state before retry.",
            owner_id="agent_c",
        )
        self.assertIn("retained as core", review_msg)

        res = commit_consolidation(
            parent_ids=[overdue_id],
            title="Core Result Replacing Reviewed Parent",
            content="Synthesized content replacing the now-reviewed, no-longer-overdue parent.",
            owner_id="agent_c",
            is_core=True,
            db_connection=self.conn,
            **self._core_reason_kwargs(),
        )
        self.assertIn("Successfully committed consolidated memory with ID:", res)
        consolidated_id = res.split("ID: ")[1].strip()
        row = self.conn.execute(
            "SELECT is_core, core_review_after FROM entities WHERE id = ?", (consolidated_id,)
        ).fetchone()
        self.assertTrue(row[0])
        self.assertGreater(datetime.fromisoformat(row[1]), datetime.now(UTC))
        parent_row = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (overdue_id,)
        ).fetchone()
        self.assertEqual(parent_row[0], "archived")


class TestConsolidationSingleParentToctou(unittest.TestCase):
    """Resolved review finding #8: single-parent consolidation never ran the cohesion gate, so it
    never got a TOCTOU observed_state snapshot either -- state revalidation must not be coupled
    to whether the cohesion gate ran."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_cons_toctou.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
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

    def _store_plain(self, title, content):
        res = store_memory(
            title=title,
            content=content,
            owner_id="agent_c",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Knowledge stored successfully"), res)
        return res.split("ID: ")[1].strip()

    def _consolidated_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE status = 'consolidated'"
        ).fetchone()[0]

    def test_single_parent_content_change_between_observe_and_commit_aborts(self):
        parent_id = self._store_plain(
            "Race Content Parent", "Original content for the content-race test."
        )

        # check_duplicate_memories runs AFTER commit_consolidation's own pre-transaction observe
        # snapshot but BEFORE _do_commit's authoritative recheck -- mutating the parent's
        # content_hash from inside it simulates a real concurrent write landing in that window.
        from saltmdb.domain.services import relation_service as rs_module

        real_check = rs_module.check_duplicate_memories

        def _racing_check(*args, **kwargs):
            self.conn.execute(
                "UPDATE entities SET content_hash = 'raced-hash-change' WHERE id = ?",
                (parent_id,),
            )
            return real_check(*args, **kwargs)

        with patch.object(rs_module, "check_duplicate_memories", side_effect=_racing_check):
            res = commit_consolidation(
                parent_ids=[parent_id],
                title="Raced Single Parent Content Change",
                content="Synthesized content for the single-parent content-race test.",
                owner_id="agent_c",
                db_connection=self.conn,
            )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("state changed", res)
        self.assertEqual(self._consolidated_count(), 0)
        row = self.conn.execute("SELECT status FROM entities WHERE id = ?", (parent_id,)).fetchone()
        self.assertEqual(row[0], "raw")

    def test_single_parent_archival_between_observe_and_commit_aborts(self):
        parent_id = self._store_plain(
            "Race Archive Parent", "Original content for the archive-race test."
        )

        from saltmdb.domain.services import relation_service as rs_module
        from saltmdb.domain.services.memory_service import archive_memory

        real_check = rs_module.check_duplicate_memories

        def _racing_check(*args, **kwargs):
            archive_memory(entity_id=parent_id, db_connection=self.conn, _in_transaction=True)
            return real_check(*args, **kwargs)

        with patch.object(rs_module, "check_duplicate_memories", side_effect=_racing_check):
            res = commit_consolidation(
                parent_ids=[parent_id],
                title="Raced Single Parent Archival",
                content="Synthesized content for the single-parent archival-race test.",
                owner_id="agent_c",
                db_connection=self.conn,
            )
        self.assertTrue(res.startswith("Error"), res)
        self.assertEqual(self._consolidated_count(), 0)

    def test_single_parent_promoted_to_core_between_observe_and_commit_is_reevaluated(self):
        # Not a TOCTOU content/status mismatch -- resolve_consolidation_core_state re-reads
        # is_core fresh inside _do_commit regardless, so a parent that becomes core mid-call is
        # still correctly caught by the existing "is_core was omitted" rule.
        parent_id = self._store_plain(
            "Race Promote Parent", "Original content for the promotion-race test."
        )

        from saltmdb.domain.services import relation_service as rs_module

        real_check = rs_module.check_duplicate_memories

        def _racing_check(*args, **kwargs):
            future = (datetime.now(UTC) + timedelta(days=14)).isoformat()
            self.conn.execute(
                "UPDATE entities SET is_core = 1, core_reason = ?, core_exit_condition = ?, "
                "core_review_after = ? WHERE id = ?",
                (
                    "Race fixture core reason, twenty-plus characters long for validation.",
                    "Race fixture exit condition, twenty-plus characters long for validation.",
                    future,
                    parent_id,
                ),
            )
            return real_check(*args, **kwargs)

        with patch.object(rs_module, "check_duplicate_memories", side_effect=_racing_check):
            res = commit_consolidation(
                parent_ids=[parent_id],
                title="Raced Single Parent Promotion",
                content="Synthesized content for the single-parent promotion-race test.",
                owner_id="agent_c",
                db_connection=self.conn,
            )
        self.assertTrue(res.startswith("Error"), res)
        self.assertIn("is_core was omitted", res)
        self.assertEqual(self._consolidated_count(), 0)


if __name__ == "__main__":
    unittest.main()
