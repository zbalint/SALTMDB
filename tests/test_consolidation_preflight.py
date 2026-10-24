from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services import relation_service as relations
from saltmdb.domain.services.memory_service import Provenance, store_memory
from saltmdb.domain.services.relation_service import consolidate_memories


class TestConsolidationPreflight(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.path = os.path.join(self.temp_dir, "preflight.db")
        self.conn = init_db(self.path)
        self.parents = [self._parent("Preflight Parent A"), self._parent("Preflight Parent B")]
        self.state = {
            parent: self.conn.execute(
                "SELECT content_hash, status FROM entities WHERE id = ?", (parent,)
            ).fetchone()
            for parent in self.parents
        }
        self.quality = {
            "status": "ACCEPT",
            "quality_score": 0.75,
            "quality_flags": ["fixture"],
            "reason": "accepted",
        }

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _parent(self, title: str) -> str:
        result = store_memory(
            title=title,
            content=f"Detailed, durable description of {title} used in preflight tests.",
            agent_id="other-owner",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def _centroids(self, _resolved_parents, _conn):
        return (
            {parent: [1.0, 0.0] for parent in self.parents},
            {},
            self.state,
        )

    def _call(self, **kwargs):
        arguments = {
            "parent_ids": self.parents,
            "title": "Preflight canonical result",
            "content": "A durable canonical result used by preflight characterization.",
            "provenance": Provenance(agent_id="other-owner"),
            "db_connection": self.conn,
        }
        arguments.update(kwargs)
        return consolidate_memories(**arguments)

    def _owned_call(self, close, quality=None, **kwargs):
        quality = self.quality if quality is None else quality
        with (
            patch.object(relations, "get_connection", return_value=self.conn),
            patch.object(relations, "close_connection", close),
            patch.object(
                relations,
                "get_fresh_entity_centroids",
                side_effect=self._centroids,
            ),
            patch.object(relations, "evaluate_memory_quality", return_value=quality),
            patch("saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"),
        ):
            return self._call(db_connection=None, db_path=self.path, **kwargs)

    def test_owned_connection_closes_on_tag_core_quality_and_exact_duplicate_rejections(self):
        with patch(
            "saltmdb.domain.services.memory_service.tags.validate_tag_names",
            return_value="invalid fixture tag",
        ):
            close = Mock()
            result = self._owned_call(close, tags=["bad::tag"])
        self.assertEqual(result["errors"][0]["code"], "INVALID_TAG_NAME")
        close.assert_called_once_with(self.conn)

        with patch.object(
            relations.core_governance_service,
            "parse_is_core",
            side_effect=ValueError("invalid fixture core"),
        ):
            close = Mock()
            result = self._owned_call(close)
        self.assertEqual(result["errors"][0]["code"], "INVALID_CORE_REQUEST")
        close.assert_called_once_with(self.conn)

        rejected_quality = {
            **self.quality,
            "status": "REJECT",
            "quality_score": 0.125,
            "reason": "fixture rejection",
        }
        close = Mock()
        result = self._owned_call(close, quality=rejected_quality)
        self.assertEqual(result["errors"][0]["code"], "REJECT_QUALITY")
        close.assert_called_once_with(self.conn)

        duplicate = store_memory(
            title="Existing duplicate",
            content="A durable canonical result used by preflight characterization.",
            agent_id="other-owner",
            db_connection=self.conn,
        )
        self.assertEqual(duplicate["status"], "ok")
        close = Mock()
        result = self._owned_call(close)
        self.assertEqual(result["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")
        close.assert_called_once_with(self.conn)

    def test_caller_owned_connection_stays_open_on_same_rejections(self):
        cases = (
            ("INVALID_TAG_NAME", {"tags": ["bad::tag"]}),
            ("INVALID_CORE_REQUEST", {}),
            ("REJECT_QUALITY", {}),
        )
        for code, kwargs in cases:
            with self.subTest(code=code):
                close = Mock()
                if code == "INVALID_TAG_NAME":
                    patcher = patch(
                        "saltmdb.domain.services.memory_service.tags.validate_tag_names",
                        return_value="invalid fixture tag",
                    )
                elif code == "INVALID_CORE_REQUEST":
                    patcher = patch.object(
                        relations.core_governance_service,
                        "parse_is_core",
                        side_effect=ValueError("invalid fixture core"),
                    )
                else:
                    patcher = patch.object(
                        relations,
                        "evaluate_memory_quality",
                        return_value={
                            **self.quality,
                            "status": "REJECT",
                            "quality_score": 0.125,
                            "reason": "fixture rejection",
                        },
                    )
                with (
                    patcher,
                    patch.object(
                        relations,
                        "get_fresh_entity_centroids",
                        side_effect=self._centroids,
                    ),
                    patch(
                        "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"
                    ),
                    patch.object(relations, "close_connection", close),
                ):
                    result = self._call(**kwargs)
                self.assertEqual(result["errors"][0]["code"], code)
                close.assert_not_called()

        duplicate = store_memory(
            title="Existing caller duplicate",
            content="A durable canonical result used by preflight characterization.",
            agent_id="other-owner",
            db_connection=self.conn,
        )
        self.assertEqual(duplicate["status"], "ok")
        close = Mock()
        with (
            patch.object(
                relations,
                "get_fresh_entity_centroids",
                side_effect=self._centroids,
            ),
            patch.object(relations, "evaluate_memory_quality", return_value=self.quality),
            patch("saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"),
            patch.object(relations, "close_connection", close),
        ):
            result = self._call()
        self.assertEqual(result["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")
        close.assert_not_called()

    def test_owned_connection_closes_when_quality_preflight_raises(self):
        close = Mock()
        with self.assertRaisesRegex(RuntimeError, "quality probe failed"):
            with (
                patch.object(relations, "get_connection", return_value=self.conn),
                patch.object(relations, "close_connection", close),
                patch.object(
                    relations,
                    "get_fresh_entity_centroids",
                    side_effect=self._centroids,
                ),
                patch.object(
                    relations,
                    "evaluate_memory_quality",
                    side_effect=RuntimeError("quality probe failed"),
                ),
                patch(
                    "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"
                ),
            ):
                self._call(db_connection=None, db_path=self.path)
        close.assert_called_once_with(self.conn)

    def test_owned_connection_closes_when_keyboard_interrupt_occurs(self):
        close = Mock()
        with self.assertRaises(KeyboardInterrupt):
            with (
                patch.object(relations, "get_connection", return_value=self.conn),
                patch.object(relations, "close_connection", close),
                patch.object(
                    relations,
                    "get_fresh_entity_centroids",
                    side_effect=self._centroids,
                ),
                patch.object(
                    relations,
                    "evaluate_memory_quality",
                    side_effect=KeyboardInterrupt(),
                ),
                patch(
                    "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"
                ),
            ):
                self._call(db_connection=None, db_path=self.path)
        close.assert_called_once_with(self.conn)

    def test_single_parent_measurement_captures_observed_state(self):
        measurement = relations._consolidation_measure_cohesion(self.conn, [self.parents[0]], None)
        self.assertEqual(measurement.min_sim, 1.0)
        self.assertIsNone(measurement.offending_pair)
        self.assertEqual(measurement.unresolved, {})
        self.assertEqual(measurement.observed_state, {self.parents[0]: self.state[self.parents[0]]})

    def test_override_annotation_is_included_in_content_hash(self):
        content = "A durable low-cohesion canonical result with an explicit override justification."
        justification = "x" * relations.COHESION_OVERRIDE_MIN_LENGTH
        with patch.object(
            relations,
            "min_pairwise_cohesion",
            return_value=(0.0, tuple(self.parents)),
        ):
            result = self._call(content=content, override_justification=justification)
        self.assertEqual(result["status"], "ok")
        stored_content, stored_hash = self.conn.execute(
            "SELECT full_content, content_hash FROM entities WHERE id = ?",
            (result["data"]["entity_id"],),
        ).fetchone()
        self.assertIn("[Consolidation Override]", stored_content)
        self.assertEqual(stored_hash, relations.compute_content_hash(stored_content))
        self.assertNotEqual(stored_hash, relations.compute_content_hash(content))


if __name__ == "__main__":
    unittest.main()
