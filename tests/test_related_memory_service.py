"""Read-only related-memory retrieval contracts; no real model is loaded."""

import importlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services import reranker_service
from saltmdb.domain.services.memory_service import search_primitives


P1 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
P2 = (
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
)
P3 = "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
P4 = (
    "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
    "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
    "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
)
P5 = (
    "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
    "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
    "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
    "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
)
P6 = (
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
)
SEGMENT_A = "Orchard planning requires careful pruning before the spring harvest."
SEGMENT_B = "Orchard irrigation protects mature trees throughout the summer season."


def related_service():
    # Defer the import so all missing-feature failures are collected during the red phase.
    return importlib.import_module("saltmdb.domain.services.related_memory_service")


class TestSplitSegments(unittest.TestCase):
    def test_removes_noise_and_selects_longest_plus_tail(self):
        text = "\n\n".join(
            [
                P1,
                "```python\ncode that must never become a retrieval segment\n```",
                P2,
                "01234567-89ab-cdef-0123-456789abcdef",
                P3,
                "https://example.org/a/very/long/url http://example.org/another/link",
                P4,
                "0123456789",
                P5,
                P6,
            ]
        )
        self.assertEqual(related_service().split_segments(text), [P2, P4, P5, P6])

    def test_query_is_truncated_to_three_hundred_characters(self):
        self.assertEqual(related_service().split_segments(P2 + P1), [P2])

    def test_unclosed_fence_drops_remainder_and_whitespace_collapses(self):
        text = "  Orchard   planning\nrequires careful pruning before spring.\n\n```\n" + P2
        self.assertEqual(
            related_service().split_segments(text),
            ["Orchard planning requires careful pruning before spring."],
        )


class TestRelatedMemoryService(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = os.path.join(directory.name, "test.db")
        self.conn = init_db(self.db_path)
        self.addCleanup(self.conn.close)
        self.scores = {}
        self.model = Mock()
        self.model.rerank.side_effect = lambda segment, texts: [
            self.scores.get((segment, text.split(" ", 1)[0]), 6.0) for text in texts
        ]
        model_patch = patch.object(reranker_service, "get_model", return_value=self.model)
        model_patch.start()
        self.addCleanup(model_patch.stop)
        semantic_patch = patch.object(search_primitives, "semantic_search", return_value=[])
        self.semantic = semantic_patch.start()
        self.addCleanup(semantic_patch.stop)

    def seed(self, entity_id, *, scope="shared", agent="a", status="raw", content=None):
        self.conn.execute(
            """INSERT INTO entities
            (id, title, full_content, scope, agent_id, status, created_at, updated_at,
             last_accessed_at, valid_from)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_id,
                entity_id,
                content or SEGMENT_A,
                scope,
                agent,
                status,
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
            ),
        )
        self.conn.commit()

    def find(self, text=SEGMENT_A, **kwargs):
        return related_service().find_related_memories(self.conn, self.db_path, text, **kwargs)

    def test_shared_only_without_identity_and_own_private_with_identity(self):
        self.seed("shared", agent="b")
        self.seed("own", scope="private")
        self.seed("other", scope="private", agent="b")
        self.assertEqual([row["id"] for row in self.find()], ["shared"])
        self.assertIn("e.scope = 'shared'", self.semantic.call_args.args[1])
        self.assertEqual([row["id"] for row in self.find(agent_id="a")], ["own", "shared"])
        clauses, params = self.semantic.call_args.args[1:3]
        self.assertIn("(e.agent_id = ? OR e.scope = 'shared')", clauses)
        self.assertIn("a", params)

    def test_archive_and_exclusion_filters_reach_both_channels(self):
        self.seed("keep")
        self.seed("excluded")
        self.seed("archived", status="archived")
        self.assertEqual([row["id"] for row in self.find(exclude_ids=["excluded"])], ["keep"])
        clauses, params, request_size = self.semantic.call_args.args[1:4]
        self.assertIn("e.status != 'archived'", clauses)
        self.assertIn("e.id NOT IN (?)", clauses)
        self.assertIn("excluded", params)
        self.assertEqual(request_size, 11)
        self.find(exclude_ids=[str(index) for index in range(60)])
        self.assertEqual(self.semantic.call_args.args[3], 50)

    def test_current_supersession_target_is_dropped_but_expired_edge_is_not(self):
        for entity_id in ("new", "old", "expired"):
            self.seed(entity_id)
        self.conn.executemany(
            """INSERT INTO relations
            (id, source_id, target_id, predicate, valid_from, valid_to, valid_at, invalid_at)
            VALUES (?, 'new', ?, 'supersedes', ?, NULL, ?, ?)""",
            [
                ("r1", "old", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", None),
                (
                    "r2",
                    "expired",
                    "2024-01-01T00:00:00+00:00",
                    "2024-01-01T00:00:00+00:00",
                    "2024-02-01T00:00:00+00:00",
                ),
            ],
        )
        self.conn.commit()
        self.assertEqual([row["id"] for row in self.find()], ["expired", "new"])

    def test_best_segment_retains_its_score_and_channel_metadata(self):
        self.seed("orchard")
        self.scores = {(SEGMENT_A, "orchard"): 2.0, (SEGMENT_B, "orchard"): 6.0}
        self.semantic.side_effect = [[("orchard", 0.4)], [("orchard", 0.2)]]
        self.assertEqual(
            self.find(SEGMENT_A + "\n\n" + SEGMENT_B),
            [
                {
                    "id": "orchard",
                    "title": "orchard",
                    "score": 6.0,
                    "segment": 1,
                    "fts_rank": 1,
                    "semantic_distance": 0.2,
                }
            ],
        )

    def test_threshold_and_limit_do_not_apply_to_all_candidates(self):
        for entity_id, score in (("a", 7.0), ("b", 5.0), ("c", 4.5), ("d", 3.0)):
            self.seed(entity_id)
            self.scores[SEGMENT_A, entity_id] = score
        self.assertEqual([row["id"] for row in self.find(min_score=4.0, limit=2)], ["a", "b"])
        self.assertEqual(
            [row["id"] for row in self.find(min_score=4.0, limit=2, with_all=True)],
            ["a", "b", "c", "d"],
        )

    def test_no_cross_encoder_signal_returns_no_results(self):
        self.seed("orchard")
        with patch.object(reranker_service, "score_pairs", return_value=None):
            self.assertEqual(self.find(), [])

    def test_semantic_failure_preserves_fts_candidates(self):
        self.seed("orchard")
        self.semantic.side_effect = RuntimeError("vector index unavailable")
        self.assertEqual([row["id"] for row in self.find()], ["orchard"])

    def test_fts_failure_preserves_semantic_candidates(self):
        self.seed("semantic", content="Unrelated text without any matching orchard terms.")
        self.semantic.return_value = [("semantic", 0.25)]
        with patch.object(
            search_primitives,
            "_run_fts_search",
            side_effect=sqlite3.OperationalError("missing FTS"),
        ):
            self.assertEqual(
                self.find(),
                [
                    {
                        "id": "semantic",
                        "title": "semantic",
                        "score": 6.0,
                        "segment": 0,
                        "fts_rank": None,
                        "semantic_distance": 0.25,
                    }
                ],
            )

    def test_programmer_errors_are_not_silently_reclassified_as_retrieval_misses(self):
        self.semantic.side_effect = TypeError("incorrect semantic call signature")
        with self.assertRaises(TypeError):
            self.find()


class TestExplicitCrossEncoderModel(unittest.TestCase):
    def test_explicit_model_bypasses_deployment_gate_and_default_remains_disabled(self):
        model = Mock()
        model.rerank.return_value = [6.0]
        with (
            patch.dict(os.environ, {"SALTMDB_RERANKER_MODEL": ""}),
            patch.object(reranker_service, "get_model", return_value=model),
        ):
            self.assertIsNone(reranker_service.score_pairs("query", ["candidate"]))
            self.assertEqual(
                reranker_service.score_pairs("query", ["candidate"], model_name="fake-model"),
                [6.0],
            )
            self.assertEqual(reranker_service.get_last_score_diagnostics()["model"], "fake-model")
