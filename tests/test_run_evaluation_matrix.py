"""Automated tests for scripts/benchmarking/run_evaluation_matrix.py -- the plan §3 matrix
runner. Uses a tiny synthetic DB (init_db + real memory_service.store_memory calls, same
convention as test_build_diverse_test_db.py) so this suite runs in seconds against real
search_memory() calls, not the full frozen corpus.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
import copy
from pathlib import Path
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"
sys.path.insert(0, str(_SCRIPTS_DIR))

_MODULE_PATH = _SCRIPTS_DIR / "run_evaluation_matrix.py"
_spec = importlib.util.spec_from_file_location("run_evaluation_matrix", _MODULE_PATH)
rem = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rem)

import eval_configs  # noqa: E402


class TestRefuseUnsafeDbPath(unittest.TestCase):
    def test_refuses_live_default_path(self):
        from saltmdb.config import get_db_path

        with self.assertRaises(RuntimeError):
            rem._refuse_unsafe_db_path(get_db_path())

    def test_refuses_shared_fixture_path(self):
        with self.assertRaises(RuntimeError):
            rem._refuse_unsafe_db_path(str(rem.SHARED_FIXTURE_PATH))

    def test_refuses_symlink_to_live_path(self):
        # Never touches the REAL live default DB path -- SALTMDB_DB_PATH is redirected to a fake
        # throwaway "live path" for the duration of this test only, so get_db_path() (and thus
        # _refuse_unsafe_db_path's comparison target) resolves to something harmless, per the
        # standing "never smoke-test against the live default DB path" dev rule.
        temp_dir = tempfile.mkdtemp()
        original_env = os.environ.get("SALTMDB_DB_PATH")
        try:
            fake_live_path = os.path.join(temp_dir, "fake_live.db")
            Path(fake_live_path).touch()
            os.environ["SALTMDB_DB_PATH"] = fake_live_path

            link_path = os.path.join(temp_dir, "sneaky_link.db")
            os.symlink(fake_live_path, link_path)
            with self.assertRaises(RuntimeError):
                rem._refuse_unsafe_db_path(link_path)
        finally:
            if original_env is None:
                os.environ.pop("SALTMDB_DB_PATH", None)
            else:
                os.environ["SALTMDB_DB_PATH"] = original_env
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_allows_an_ordinary_throwaway_copy(self):
        temp_dir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(temp_dir, "throwaway.db")
            init_db(db_path).close()
            rem._refuse_unsafe_db_path(db_path)  # must not raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_matrix_validation_rejects_missing_provenance_by_default(self):
        artifact = {
            "meta": {"queries_fingerprint": "q", "configs_fingerprint": "c"},
            "resume_meta": {"queries_fingerprint": "q", "configs_fingerprint": "c"},
        }
        artifact["artifact_fingerprint"] = rem._fingerprint(artifact)
        with self.assertRaisesRegex(ValueError, "provenance"):
            rem.validate_matrix_artifact(artifact)


class TestRunMatrixForQueries(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

        # A real, findable entity.
        result = memory_service.store_memory(
            content="The distributed cache invalidation protocol propagates updates "
            "asynchronously across all replica nodes in the cluster.",
            title="Distributed Cache Invalidation Protocol",
            owner_id="test",
            skip_duplicate_check=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.findable_id = self._extract_id(result)

        # A second entity, then archived -- used to test that force-include correctly skips
        # archived entities (matches search_memory's own candidate-pool exclusion).
        archived_result = memory_service.store_memory(
            content="A raft consensus leader election algorithm prevents split-brain scenarios "
            "during a network partition in a distributed system.",
            title="Raft Consensus Leader Election",
            owner_id="test",
            skip_duplicate_check=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.archived_id = self._extract_id(archived_result)
        self.conn.execute(
            "UPDATE entities SET status = 'archived' WHERE id = ?", (self.archived_id,)
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _extract_id(store_result) -> str:
        # store_memory's return is documented elsewhere in this repo's own benchmark scripts as
        # containing "Knowledge stored successfully with ID: <uuid>" -- extract it the same way.
        import re

        m = re.search(r"ID:\s*([0-9a-fA-F-]{36})", store_result)
        assert m, f"could not extract entity id from store_memory result: {store_result!r}"
        return m.group(1)

    def test_smoke_run_finds_the_real_entity(self):
        configs = [
            c
            for c in eval_configs._build_evaluation_configs()
            if c["name"] == eval_configs.CURRENT_DEFAULT_CONFIG_NAME
        ]
        queries = [
            {
                "id": "q1",
                "query": "distributed cache invalidation protocol replica nodes",
                "source_entity_ids": [self.findable_id],
                "category": "exact_title",
            }
        ]
        result = rem.run_matrix_for_queries(
            self.conn, self.db_path, queries, configs, limit=10, progress_every=0
        )
        self.assertEqual(result["errors"], [])
        ranking = result["config_rankings"]["q1"][eval_configs.CURRENT_DEFAULT_CONFIG_NAME]
        self.assertIn(self.findable_id, ranking)
        self.assertIn(self.findable_id, result["pools"]["q1"])
        self.assertFalse(result["pools"]["q1"][self.findable_id]["ground_truth_forced_include"])

    def test_force_include_when_not_retrieved(self):
        # Query has NOTHING to do with the archived entity's content, and points its
        # source_entity_ids at an entity that (a) search_memory would never surface anyway
        # (archived) and (b) isn't semantically related to the query text. Per §0b item 17 the
        # force-include path should look it up directly -- but since it's ARCHIVED, the direct
        # lookup itself must also skip it (an archived "ground truth" is not real ground truth
        # any config could ever have retrieved), landing in errors instead of a fabricated pool
        # entry.
        configs = [
            c
            for c in eval_configs._build_evaluation_configs()
            if c["name"] == eval_configs.CURRENT_DEFAULT_CONFIG_NAME
        ]
        queries = [
            {
                "id": "q2",
                "query": "completely unrelated query text about weather patterns",
                "source_entity_ids": [self.archived_id],
                "category": "current_vs_superseded",
            }
        ]
        result = rem.run_matrix_for_queries(
            self.conn, self.db_path, queries, configs, limit=10, progress_every=0
        )
        self.assertNotIn(self.archived_id, result["pools"]["q2"])
        self.assertTrue(
            any(
                e["query_id"] == "q2" and "not found/archived" in e["error"]
                for e in result["errors"]
            )
        )

    def test_force_include_fetches_stub_when_entity_exists_but_unretrieved(self):
        # A query engineered to NOT surface the findable entity via keyword match, but whose
        # source_entity_ids still names it -- force-include must fetch it directly (title/
        # snippet populated) rather than silently omitting it.
        configs = [
            c
            for c in eval_configs._build_evaluation_configs()
            if c["name"] == eval_configs.CURRENT_DEFAULT_CONFIG_NAME
        ]
        queries = [
            {
                "id": "q3",
                "query": "zzz9 qqq7 nonsense token unrelated to anything stored",
                "source_entity_ids": [self.findable_id],
                "category": "paraphrase",
            }
        ]
        result = rem.run_matrix_for_queries(
            self.conn, self.db_path, queries, configs, limit=10, progress_every=0
        )
        ranking = result["config_rankings"]["q3"][eval_configs.CURRENT_DEFAULT_CONFIG_NAME]
        forced = self.findable_id not in ranking
        pool_entry = result["pools"]["q3"].get(self.findable_id)
        self.assertIsNotNone(pool_entry)
        if forced:
            self.assertTrue(pool_entry["ground_truth_forced_include"])
            self.assertIsNotNone(pool_entry["title"])
            self.assertIsNotNone(pool_entry["snippet"])

    def test_no_crash_on_empty_query_set(self):
        result = rem.run_matrix_for_queries(self.conn, self.db_path, [], [], progress_every=0)
        self.assertEqual(result["config_rankings"], {})
        self.assertEqual(result["pools"], {})

    def test_non_list_search_result_is_recorded_as_a_structured_matrix_error(self):
        """A service-level error object must never reach item[\"id\"] dereferencing.

        This is the regression for the initial system-Python matrix attempt, where a missing
        sqlite_vec dependency made ``search_memory`` return a dict and the runner crashed with
        ``KeyError('id')`` before it could persist a diagnostic checkpoint.
        """
        config = next(
            c
            for c in eval_configs._build_evaluation_configs()
            if c["name"] == eval_configs.CURRENT_DEFAULT_CONFIG_NAME
        )
        query = {
            "id": "q-non-list",
            "query": "distributed cache invalidation",
            "source_entity_ids": [],
            "category": "exact_title",
        }
        with patch.object(rem, "search_memory", return_value={"unexpected": "service shape"}):
            result = rem.run_matrix_for_queries(
                self.conn, self.db_path, [query], [config], progress_every=0
            )

        self.assertEqual(result["config_rankings"][query["id"]][config["name"]], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["query_id"], query["id"])
        self.assertEqual(result["errors"][0]["config_name"], config["name"])
        self.assertIn("unexpected result type", result["errors"][0]["error"])

    def test_resume_does_not_repeat_completed_query(self):
        config = next(
            c
            for c in eval_configs._build_evaluation_configs()
            if c["name"] == eval_configs.CURRENT_DEFAULT_CONFIG_NAME
        )
        queries = [
            {
                "id": "q-resume",
                "query": "distributed cache invalidation",
                "source_entity_ids": [],
                "category": "exact_title",
            }
        ]
        checkpoint = Path(self.temp_dir) / "checkpoint.json"
        meta = {"queries_fingerprint": "q", "configs_fingerprint": "c", "limit": 10}
        first = rem.run_matrix_for_queries(
            self.conn,
            self.db_path,
            queries,
            [config],
            limit=10,
            progress_every=0,
            checkpoint_path=checkpoint,
            checkpoint_every=1,
            resume_meta=meta,
        )
        resumed = rem.run_matrix_for_queries(
            self.conn,
            self.db_path,
            queries,
            [config],
            limit=10,
            progress_every=0,
            resume_result=first,
            resume_meta=meta,
        )
        self.assertEqual(first["config_rankings"], resumed["config_rankings"])
        self.assertEqual(first["latencies_ms"], resumed["latencies_ms"])

    def test_resume_rejects_unknown_completed_query(self):
        config = next(
            c
            for c in eval_configs._build_evaluation_configs()
            if c["name"] == eval_configs.CURRENT_DEFAULT_CONFIG_NAME
        )
        with self.assertRaises(ValueError):
            rem.run_matrix_for_queries(
                self.conn,
                self.db_path,
                [{"id": "q", "query": "cache", "source_entity_ids": [], "category": "exact_title"}],
                [config],
                progress_every=0,
                resume_result={
                    "completed_query_ids": ["not-in-manifest"],
                    "config_rankings": {},
                    "pools": {},
                },
            )

    def test_blind_matrix_gate_rejects_missing_or_invalid_shortlist(self):
        with self.assertRaisesRegex(RuntimeError, "signed --dev-shortlist"):
            rem.require_frozen_dev_shortlist(None)
        invalid = Path(self.temp_dir) / "invalid-shortlist.json"
        invalid.write_text("{}")
        with self.assertRaises(ValueError):
            rem.require_frozen_dev_shortlist(invalid)

    def test_cross_encoder_preflight_is_required_and_finite(self):
        configs = [{"name": "ce", "use_cross_encoder": True}]
        ready = {"ready": True, "scores": [1.0, 0.0], "diagnostics": {"finite_and_sized": True}}
        with patch.object(
            rem.reranker_service, "score_pairs_preflight", return_value=ready
        ) as probe:
            self.assertEqual(rem.preflight_cross_encoder_configs(configs), ready)
        probe.assert_called_once()
        with patch.object(
            rem.reranker_service,
            "score_pairs_preflight",
            return_value={"ready": False, "scores": None, "diagnostics": {"reason": "malformed"}},
        ):
            with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                rem.preflight_cross_encoder_configs(configs)

    def test_cross_encoder_zero_execution_invalidates_experiment(self):
        configs = [{"name": "ce", "use_cross_encoder": True}]
        zero = {"execution_diagnostics": {"q": {"ce": {"cross_encoder": {"executed": False}}}}}
        with self.assertRaisesRegex(RuntimeError, "zero successful executions"):
            rem.summarize_cross_encoder_execution(zero, configs)
        ran = copy.deepcopy(zero)
        ran["execution_diagnostics"]["q"]["ce"]["cross_encoder"] = {
            "executed": True,
            "execution_count": 1,
        }
        summary = rem.summarize_cross_encoder_execution(ran, configs)
        self.assertEqual(summary["execution_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
