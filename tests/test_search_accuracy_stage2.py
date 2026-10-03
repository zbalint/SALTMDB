"""Focused Stage 2-4 retrieval controls and propagation tests.

These fixtures use ``init_db``'s production vector DDL, but patch model inference so the tests
remain deterministic and do not download or depend on a reranker.  The larger existing retrieval
tests cover legacy topic/supersession behavior; this file targets only the newly added opt-ins.
"""

import os
import importlib.util
import json
import pathlib
import shutil
import sqlite_vec
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from saltmdb.db.schema import init_db
from saltmdb.daemon.dispatch import _dispatch_search_memory
from saltmdb.domain.services import embedding_service, reranker_service
from saltmdb.domain.services.memory_service import (
    _build_cross_encoder_candidate_texts,
    _collapse_supersedes_families,
    chunk_candidate_search,
    reciprocal_rank_fusion,
    search_memory,
    weighted_reciprocal_rank_fusion,
    get_last_search_diagnostics,
    orchestrator,
)


_BENCHMARK_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmarking"
    / "run_evaluation_matrix.py"
)
sys.path.insert(0, str(_BENCHMARK_PATH.parent))
_BENCHMARK_SPEC = importlib.util.spec_from_file_location(
    "stage2_run_evaluation_matrix", _BENCHMARK_PATH
)
benchmark_matrix = importlib.util.module_from_spec(_BENCHMARK_SPEC)
sys.modules[_BENCHMARK_SPEC.name] = benchmark_matrix
_BENCHMARK_SPEC.loader.exec_module(benchmark_matrix)


DIM = 384


def _axis(index: int) -> list[float]:
    value = [0.0] * DIM
    value[index] = 1.0
    return value


class _DbFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "stage2.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def entity(
        self,
        entity_id: str,
        *,
        owner: str = "owner",
        status: str = "raw",
        content_hash: str | None = None,
        content: str | None = None,
    ):
        content_hash = content_hash or f"hash-{entity_id}"
        content = content or f"leading content for {entity_id}"
        self.conn.execute(
            "INSERT INTO entities (id,created_at,updated_at,last_accessed_at,agent_id,scope,status,title,full_content,content_hash) "
            "VALUES (?,datetime('now'),datetime('now'),datetime('now'),?,'private',?,?,?,?)",
            (entity_id, owner, status, entity_id, content, content_hash),
        )
        self.conn.commit()

    def chunk(
        self,
        entity_id: str,
        index: int,
        vector: list[float],
        *,
        content_hash: str,
        start: int = 0,
        end: int = 20,
        chunk_id: str | None = None,
    ):
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings (id,entity_id,embedding,chunk_index,char_start,char_end,content_hash) VALUES (?,?,?,?,?,?,?)",
            (
                chunk_id or f"{entity_id}::{index}",
                entity_id,
                sqlite_vec.serialize_float32(vector),
                index,
                start,
                end,
                content_hash,
            ),
        )
        self.conn.commit()

    def relation(self, source: str, target: str, **kwargs):
        values = {
            "valid_from": None,
            "valid_to": None,
            "valid_at": None,
            "invalid_at": None,
        }
        values.update(kwargs)
        self.conn.execute(
            "INSERT INTO relations (id,source_id,target_id,predicate,valid_from,valid_to,valid_at,invalid_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                f"rel-{source}-{target}-{values['valid_from'] or 'now'}",
                source,
                target,
                "supersedes",
                values["valid_from"],
                values["valid_to"],
                values["valid_at"],
                values["invalid_at"],
            ),
        )
        self.conn.commit()


class TestChunkCandidateRuntime(_DbFixture):
    def test_fresh_hash_filter_dedup_shortfall_and_filter(self):
        self.entity("fresh", owner="alice", content_hash="fresh-hash")
        self.entity("stale", owner="alice", content_hash="current-hash")
        self.entity("archived", owner="alice", status="archived", content_hash="archived-hash")
        self.entity("other-owner", owner="bob", content_hash="other-hash")
        # The duplicate fresh rows must collapse to one entity using the minimum distance.
        self.chunk("fresh", 0, _axis(0), content_hash="fresh-hash")
        self.chunk("fresh", 1, _axis(1), content_hash="fresh-hash")
        self.chunk("stale", 0, _axis(0), content_hash="stale-hash")
        self.chunk("archived", 0, _axis(0), content_hash="archived-hash")
        self.chunk("other-owner", 0, _axis(0), content_hash="other-hash")

        with patch.object(embedding_service, "embed_text", return_value=_axis(0)):
            rows, diagnostics = chunk_candidate_search(
                "query",
                ["e.status != 'archived'", "e.agent_id = ?"],
                ["alice"],
                candidate_window=4,
                oversampling_multiplier=4,
                db_path=self.db_path,
            )

        self.assertEqual([row[0] for row in rows], ["fresh"])
        self.assertEqual(diagnostics["requested_chunk_rows"], 16)
        self.assertEqual(diagnostics["unique_fresh_entities"], 1)
        self.assertEqual(diagnostics["candidate_shortfall"], 3)

    def test_disabled_weighted_fusion_is_byte_equivalent_to_legacy(self):
        fts = [
            ("a", "", "", 1, 0, 0, 0, "", "", "", "", None, "fact", 0, None),
            ("b", "", "", 1, 0, 0, 0, "", "", "", "", None, "fact", 0, None),
        ]
        semantic = [("b", 0.1), ("a", 0.2)]
        self.assertEqual(
            reciprocal_rank_fusion(fts, semantic, 2),
            weighted_reciprocal_rank_fusion(fts, semantic, [], 2, chunk_weight=1.5),
        )

    def test_weighted_chunk_channel_keeps_deterministic_ties(self):
        fts = [("a",), ("b",)]
        semantic = []
        chunk = [("b", 0.1)]
        result = weighted_reciprocal_rank_fusion(fts, semantic, chunk, 2, chunk_weight=0.5)
        # The chunk contribution intentionally breaks this otherwise close pair in favor of b;
        # Python's stable sort still preserves deterministic channel insertion order for exact
        # score ties (the disabled/legacy equality above covers that byte-for-byte contract).
        self.assertEqual(list(result), ["b", "a"])


class TestSupersedesFamilyCollapse(_DbFixture):
    def test_chain_emits_existing_head_at_first_member_position(self):
        for eid in ("old", "middle", "new", "other"):
            self.entity(eid)
        self.relation("middle", "old")
        self.relation("new", "middle")
        self.assertEqual(
            _collapse_supersedes_families(["old", "other", "middle", "new"], self.conn),
            ["new", "other"],
        )

    def test_fork_cycle_future_invalid_archived_absent_and_filter_are_not_collapsed(self):
        # Fork: two live heads target one old member.
        for eid in ("old", "head-a", "head-b"):
            self.entity(eid)
        self.relation("head-a", "old")
        self.relation("head-b", "old")
        self.assertEqual(
            _collapse_supersedes_families(["old", "head-a", "head-b"], self.conn),
            ["old", "head-a", "head-b"],
        )

        # Cycle has no unique head.
        self.entity("cycle-a")
        self.entity("cycle-b")
        self.relation("cycle-a", "cycle-b")
        self.relation("cycle-b", "cycle-a")
        self.assertEqual(
            _collapse_supersedes_families(["cycle-a", "cycle-b"], self.conn),
            ["cycle-a", "cycle-b"],
        )

        # Future/invalid edges are not active families.
        self.entity("future-old")
        self.entity("future-new")
        self.relation("future-new", "future-old", valid_from="2099-01-01T00:00:00+00:00")
        self.entity("invalid-old")
        self.entity("invalid-new")
        self.relation("invalid-new", "invalid-old", valid_to="2020-01-01T00:00:00+00:00")
        self.assertEqual(
            _collapse_supersedes_families(
                ["future-old", "future-new", "invalid-old", "invalid-new"], self.conn
            ),
            ["future-old", "future-new", "invalid-old", "invalid-new"],
        )

        # Archived or absent head fails the all-members-in-pool/live requirement.
        self.entity("archived-old")
        self.entity("archived-head", status="archived")
        self.relation("archived-head", "archived-old")
        self.entity("absent-old")
        self.entity("absent-head")
        self.relation("absent-head", "absent-old")
        self.assertEqual(
            _collapse_supersedes_families(["archived-old"], self.conn), ["archived-old"]
        )
        self.assertEqual(_collapse_supersedes_families(["absent-old"], self.conn), ["absent-old"])

    def test_collapse_is_rejected_outside_broad_mode(self):
        with self.assertRaisesRegex(ValueError, "broad mode"):
            search_memory(
                query_keywords="mode validation",
                mode="history",
                collapse_supersedes_families=True,
                db_path=self.db_path,
            )


class TestSearchRankingDiagnostics(_DbFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(embedding_service, "embed_text", return_value=_axis(0)))
        self.enterContext(
            patch.object(reranker_service, "is_cross_encoder_enabled", return_value=False)
        )
        for eid in ("alpha", "beta", "gamma"):
            self.entity(eid, content=f"diagnosticquery evidence for {eid}")

    def test_broad_candidate_records_describe_full_result_order(self):
        result = search_memory(
            query_keywords="diagnosticquery",
            limit=10,
            return_diagnostics=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        assert isinstance(result, dict)
        self.assertEqual(set(result), {"results", "diagnostics"})
        candidates = result["diagnostics"]["candidates"]
        expected_keys = {
            "entity_id",
            "provenance",
            "rrf_score",
            "in_fts",
            "in_fts_and",
            "in_fts_or_only",
            "fts_rank",
            "fts_bm25",
            "in_semantic",
            "semantic_rank",
            "semantic_distance",
            "dual_channel",
            "topic_score",
            "semantic_verdict",
            "is_resolved_head",
            "predecessor_grounded",
            "cross_encoder_score",
            "pool_rank",
            "final_rank",
        }
        self.assertEqual({row["id"] for row in result["results"]}, {"alpha", "beta", "gamma"})
        self.assertEqual(result["diagnostics"]["candidate_pool_size"], 3)
        for index, candidate in enumerate(candidates):
            self.assertEqual(set(candidate), expected_keys)
            self.assertEqual(candidate["pool_rank"], index)
            self.assertEqual(candidate["fts_rank"], index)
        surviving = sorted(
            (candidate for candidate in candidates if candidate["final_rank"] is not None),
            key=lambda candidate: candidate["final_rank"],
        )
        self.assertEqual(
            [candidate["entity_id"] for candidate in surviving],
            [row["id"] for row in result["results"]],
        )
        json.dumps(result)

    def test_flag_off_leaves_candidate_diagnostics_empty(self):
        result = search_memory(
            query_keywords="diagnosticquery",
            limit=10,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        self.assertIsInstance(result, list)
        diagnostics = get_last_search_diagnostics()
        self.assertEqual(diagnostics["candidates"], [])
        self.assertEqual(diagnostics["candidate_pool_size"], 0)

    def test_candidate_diagnostics_are_capped_but_pool_count_is_uncapped(self):
        with patch.object(orchestrator, "SEARCH_DIAGNOSTICS_MAX_CANDIDATES", 2):
            result = search_memory(
                query_keywords="diagnosticquery",
                limit=10,
                return_diagnostics=True,
                db_connection=self.conn,
                db_path=self.db_path,
            )
        assert isinstance(result, dict)
        diagnostics = result["diagnostics"]
        self.assertEqual(len(diagnostics["candidates"]), 2)
        self.assertGreater(diagnostics["candidate_pool_size"], 2)

    def test_candidate_diagnostics_contain_no_memory_text(self):
        phrase = "diagnostics-must-not-leak-this-distinctive-text"
        self.entity("secret", content=f"diagnosticquery {phrase}")
        result = search_memory(
            query_keywords="diagnosticquery",
            limit=10,
            return_diagnostics=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        assert isinstance(result, dict)
        self.assertNotIn(phrase, json.dumps(result["diagnostics"]))

    def test_cross_encoder_scores_are_recorded_in_pool_order(self):
        scores = [0.1, 0.3, 0.2]
        with (
            patch.object(reranker_service, "is_cross_encoder_enabled", return_value=True),
            patch.object(reranker_service, "score_pairs", return_value=scores),
            patch.object(reranker_service, "get_last_score_diagnostics", return_value={}),
        ):
            result = search_memory(
                query_keywords="diagnosticquery",
                limit=10,
                return_diagnostics=True,
                db_connection=self.conn,
                db_path=self.db_path,
            )
        assert isinstance(result, dict)
        candidates = result["diagnostics"]["candidates"]
        self.assertEqual(
            [candidate["cross_encoder_score"] for candidate in candidates],
            sorted(scores, reverse=True),
        )
        self.assertEqual(
            [candidate["pool_rank"] for candidate in candidates],
            list(range(len(candidates))),
        )

    def test_strict_diagnostics_include_every_returned_id(self):
        result = search_memory(
            query_keywords="diagnosticquery",
            limit=10,
            mode="strict",
            return_diagnostics=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        assert isinstance(result, dict)
        candidate_ids = {
            candidate["entity_id"] for candidate in result["diagnostics"]["candidates"]
        }
        self.assertTrue({row["id"] for row in result["results"]} <= candidate_ids)

    def test_history_diagnostics_include_pool_records(self):
        result = search_memory(
            query_keywords="diagnosticquery",
            limit=10,
            mode="history",
            return_diagnostics=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        assert isinstance(result, dict)
        self.assertTrue(result["diagnostics"]["candidates"])

    def test_collapsed_member_keeps_evidence_with_no_final_rank(self):
        self.entity("old", content="diagnosticquery superseded member")
        self.entity("new", content="diagnosticquery superseding head")
        self.relation("new", "old")
        result = search_memory(
            query_keywords="diagnosticquery",
            limit=10,
            collapse_supersedes_families=True,
            return_diagnostics=True,
            db_connection=self.conn,
            db_path=self.db_path,
        )
        assert isinstance(result, dict)
        by_id = {
            candidate["entity_id"]: candidate for candidate in result["diagnostics"]["candidates"]
        }
        self.assertIsInstance(by_id["new"]["final_rank"], int)
        self.assertIsNone(by_id["old"]["final_rank"])


class TestCrossEncoderControls(_DbFixture):
    def setUp(self):
        super().setUp()
        self._old_model = os.environ.get("SALTMDB_RERANKER_MODEL")
        os.environ["SALTMDB_RERANKER_MODEL"] = "Xenova/ms-marco-MiniLM-L-6-v2"

    def tearDown(self):
        if self._old_model is None:
            os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        else:
            os.environ["SALTMDB_RERANKER_MODEL"] = self._old_model
        super().tearDown()

    def test_custom_caps_and_preflight_are_finite_and_sized(self):
        model = MagicMock()
        model.rerank.side_effect = [[1.0] * 15, [0.5, 0.25]]
        with patch.object(reranker_service, "get_model", return_value=model):
            result = reranker_service.score_pairs(
                "query", ["x" * 3000] * 15, candidate_cap=15, text_cap_chars=2000
            )
            preflight = reranker_service.score_pairs_preflight(
                candidates=["a", "b"], candidate_cap=10, text_cap_chars=1000
            )
        self.assertEqual(len(result), 15)
        self.assertEqual(len(model.rerank.call_args_list[0].args[1]), 15)
        self.assertEqual(len(model.rerank.call_args_list[0].args[1][0]), 2000)
        self.assertTrue(preflight["ready"])
        self.assertTrue(preflight["diagnostics"]["finite_and_sized"])

    def test_best_fresh_query_matching_chunk_beats_leading_content_and_stale_falls_back(self):
        self.entity(
            "chunked",
            content_hash="chunked-hash",
            content="LEADING " + ("x" * 40) + " MATCHING CHUNK",
        )
        self.chunk("chunked", 0, _axis(1), content_hash="chunked-hash", start=0, end=48)
        self.chunk("chunked", 1, _axis(0), content_hash="chunked-hash", start=48, end=64)
        self.entity("stale", content_hash="current-hash", content="STALE FALLBACK")
        self.chunk("stale", 0, _axis(0), content_hash="old-hash", start=0, end=20)
        with patch.object(embedding_service, "embed_text", return_value=_axis(0)):
            texts = _build_cross_encoder_candidate_texts(
                "query", ["chunked", "stale"], self.conn, self.db_path
            )
        self.assertIn("MATCHING CHUNK", texts["chunked"])
        self.assertIn("STALE FALLBACK", texts["stale"])

    def test_malformed_and_nonfinite_scores_preserve_order(self):
        model = MagicMock()
        model.rerank.return_value = [float("nan")]
        with patch.object(reranker_service, "get_model", return_value=model):
            self.assertIsNone(reranker_service.score_pairs("q", ["a"]))
            self.assertEqual(
                reranker_service.get_last_score_diagnostics()["reason"], "malformed_output"
            )


class TestSearchOptionPropagation(unittest.TestCase):
    def test_benchmark_forwards_runtime_controls(self):
        config = {
            "mode": "broad",
            "prefer_durable_types": False,
            "demote_superseded": False,
            "cross_encoder_candidate_cap": 20,
            "cross_encoder_text_cap_chars": 2000,
            "use_chunk_candidates": True,
            "oversampling_multiplier": 12,
            "candidate_window": 60,
            "chunk_weight": 1.5,
            "collapse_supersedes_families": True,
        }
        with patch.object(benchmark_matrix, "search_memory", return_value=[{"id": "e1"}]) as search:
            items, _latency, error = benchmark_matrix.run_one_config(
                None, "throwaway.db", "q", config
            )
        self.assertIsNone(error)
        self.assertEqual(items[0]["id"], "e1")
        call = search.call_args.kwargs
        self.assertTrue(call["use_chunk_candidates"])
        self.assertEqual(call["oversampling_multiplier"], 12)
        self.assertEqual(call["candidate_window"], 60)
        self.assertEqual(call["chunk_weight"], 1.5)
        self.assertTrue(call["collapse_supersedes_families"])
        self.assertEqual(call["cross_encoder_candidate_cap"], 20)
        self.assertEqual(call["cross_encoder_text_cap_chars"], 2000)

    def test_daemon_dispatch_forwards_all_new_controls(self):
        with patch(
            "saltmdb.daemon.dispatch.memory_service.search_memory", return_value=[]
        ) as search:
            _dispatch_search_memory(
                query_keywords="q",
                use_chunk_candidates=True,
                oversampling_multiplier=8,
                candidate_window=40,
                chunk_weight=1.5,
                collapse_supersedes_families=True,
                cross_encoder_candidate_cap=15,
                cross_encoder_text_cap_chars=2000,
                return_diagnostics=True,
            )
        call = search.call_args.kwargs
        self.assertEqual(call["oversampling_multiplier"], 8)
        self.assertEqual(call["candidate_window"], 40)
        self.assertEqual(call["chunk_weight"], 1.5)
        self.assertTrue(call["collapse_supersedes_families"])
        self.assertEqual(call["cross_encoder_candidate_cap"], 15)
        self.assertEqual(call["cross_encoder_text_cap_chars"], 2000)
        self.assertTrue(call["return_diagnostics"])


if __name__ == "__main__":
    unittest.main()
