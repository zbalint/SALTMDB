from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.db.schema import init_db
from saltmdb.domain.services import relation_service as relations
from saltmdb.domain.services.memory_service import CoreDeclaration, Provenance, store_memory
from saltmdb.domain.services.relation_service import consolidate_memories, store_relation


class TestConsolidationCommit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.path = os.path.join(self.temp_dir, "commit.db")
        self.conn = init_db(self.path)
        self.parents = [self._parent("Commit Parent A"), self._parent("Commit Parent B")]
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
        enqueue_patch = patch(
            "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"
        )
        self._patches = [
            patch.object(
                relations,
                "get_fresh_entity_centroids",
                return_value=({parent: [1.0, 0.0] for parent in self.parents}, {}, self.state),
            ),
            patch.object(relations, "evaluate_memory_quality", return_value=self.quality),
            patch.object(
                relations, "check_duplicate_memories", return_value={"duplicate_found": False}
            ),
            enqueue_patch,
        ]
        patch_mocks = [fixture_patch.start() for fixture_patch in self._patches]
        self.enqueue_embedding = patch_mocks[-1]
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _parent(self, title: str) -> str:
        result = store_memory(
            title=title,
            content=f"Detailed durable content for {title} in commit tests.",
            agent_id="commit-owner",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def _call(self, **kwargs):
        arguments = {
            "parent_ids": self.parents,
            "title": "Commit canonical result",
            "content": "A durable canonical result for commit characterization.",
            "provenance": Provenance(agent_id="commit-owner"),
            "db_connection": self.conn,
        }
        arguments.update(kwargs)
        return consolidate_memories(**arguments)

    def test_bidirectional_worklist_is_deduplicated_and_shared_by_envelope_keys(self):
        external = self._parent("Commit external endpoint")
        store_relation(
            source_id=self.parents[0],
            target_id=external,
            predicate="related_to",
            db_connection=self.conn,
        )
        store_relation(
            source_id=external,
            target_id=self.parents[1],
            predicate="depends_on",
            db_connection=self.conn,
        )
        store_relation(
            source_id=self.parents[0],
            target_id=self.parents[1],
            predicate="verifies",
            db_connection=self.conn,
        )

        result = self._call()
        self.assertEqual(result["status"], "ok", result)
        data = result["data"]
        expected = [
            {
                "predicate": "related_to",
                "other_endpoint": external,
                "originating_parent": self.parents[0],
                "source_id": self.parents[0],
                "target_id": external,
            },
            {
                "predicate": "verifies",
                "other_endpoint": self.parents[1],
                "originating_parent": self.parents[0],
                "source_id": self.parents[0],
                "target_id": self.parents[1],
            },
            {
                "predicate": "depends_on",
                "other_endpoint": external,
                "originating_parent": self.parents[1],
                "source_id": external,
                "target_id": self.parents[1],
            },
        ]
        self.assertEqual(data["orphaned_edges"], expected)
        self.assertEqual(data["orphaned_relations"], expected)
        self.assertEqual(data["orphaned_edge_worklist"], expected)
        self.assertIs(data["orphaned_edges"], data["orphaned_relations"])
        self.assertIs(data["orphaned_edges"], data["orphaned_edge_worklist"])
        self.assertEqual(len(data["orphaned_edges"]), 3)

    def test_override_audit_payload_and_writer_are_exact(self):
        justification = "merging these parents intentionally despite a low cohesion score"
        with patch.object(
            relations,
            "min_pairwise_cohesion",
            return_value=(0.123456789, tuple(self.parents)),
        ):
            result = self._call(
                override_justification=justification,
                provenance=Provenance(agent_id="audit-writer"),
            )

        self.assertEqual(result["status"], "ok", result)
        consolidated_id = result["data"]["entity_id"]
        event = self.conn.execute(
            "SELECT agent_id, type, content FROM events WHERE type = 'consolidation_gate_override'"
        ).fetchone()
        self.assertEqual(event[:2], ("audit-writer", "consolidation_gate_override"))
        self.assertEqual(
            json.loads(event[2]),
            {
                "parent_ids": self.parents,
                "min_pairwise_similarity": 0.123456789,
                "threshold": relations.COHESION_MIN_PAIRWISE_THRESHOLD,
                "offending_pair": list(self.parents),
                "unresolved": {},
                "justification": justification,
                "consolidated_id": consolidated_id,
            },
        )

    def test_archive_step_persists_tags_core_state_and_embedding_jobs(self):
        core_state = {
            "is_core": True,
            "is_new_core": True,
            "core_reason": "A sufficiently detailed core reason for commit tests",
            "core_exit_condition": "A sufficiently detailed core exit condition for commit tests",
            "core_review_after": "2099-01-01T00:00:00+00:00",
            "core_detail_memory_ids": [],
        }
        with (
            patch.object(
                relations.core_governance_service,
                "resolve_derived_core_state",
                return_value=core_state,
            ) as resolve_core,
            patch(
                "saltmdb.domain.services.embedding_service.cancel_embedding_jobs_for_entity"
            ) as cancel_embedding,
            patch(
                "saltmdb.domain.services.embedding_service.cancel_retrieval_embedding_jobs_for_entity"
            ) as cancel_retrieval,
        ):
            result = self._call(
                tags=["#commit-tag"],
                core=CoreDeclaration(
                    is_core=True,
                    core_reason=core_state["core_reason"],
                    core_exit_condition=core_state["core_exit_condition"],
                    core_review_after=core_state["core_review_after"],
                ),
            )
        self.assertEqual(result["status"], "ok", result)
        entity_id = result["data"]["entity_id"]
        content = "A durable canonical result for commit characterization."
        resolve_core.assert_called_once_with(
            self.conn,
            source_ids=self.parents,
            declaration=CoreDeclaration(
                is_core=True,
                core_reason=core_state["core_reason"],
                core_exit_condition=core_state["core_exit_condition"],
                core_review_after=core_state["core_review_after"],
            ),
            content=content,
            scope="shared",
            new_entry={
                "id": entity_id,
                "title": "Commit canonical result",
                "memory_type": "fact",
                "core_reason": core_state["core_reason"],
                "core_exit_condition": core_state["core_exit_condition"],
                "core_review_after": core_state["core_review_after"],
                "full_content": content,
                "agent_id": "commit-owner",
            },
        )
        self.enqueue_embedding.assert_called_once_with(
            self.conn,
            entity_id,
            "Commit canonical result",
            content,
            relations.compute_content_hash(content),
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT status, valid_to FROM entities WHERE id IN (?, ?) ORDER BY id",
                (self.parents[0], self.parents[1]),
            ).fetchall(),
            [
                (
                    "archived",
                    self.conn.execute(
                        "SELECT valid_from FROM entities WHERE id = ?", (entity_id,)
                    ).fetchone()[0],
                )
            ]
            * 2,
        )
        tags = self.conn.execute(
            "SELECT t.name FROM entity_tags et JOIN tags t ON t.id = et.tag_id "
            "WHERE et.entity_id = ? ORDER BY t.name",
            (entity_id,),
        ).fetchall()
        self.assertEqual(tags, [("#commit-tag",), ("#core",)])
        self.assertEqual(cancel_embedding.call_count, 2)
        self.assertEqual(cancel_retrieval.call_count, 4)
        self.assertEqual(
            {call.args[1] for call in cancel_embedding.call_args_list}, set(self.parents)
        )
        self.assertEqual(
            {call.args[1] for call in cancel_retrieval.call_args_list}, set(self.parents)
        )

    def test_rejected_audit_aborts_entity_and_parent_writes(self):
        with (
            patch.object(
                relations,
                "min_pairwise_cohesion",
                return_value=(0.0, tuple(self.parents)),
            ),
            patch.object(
                relations,
                "log_event",
                return_value={
                    "status": "rejected",
                    "errors": [{"message": "audit unavailable"}],
                },
            ),
        ):
            result = self._call(
                override_justification="a sufficiently detailed audit failure justification"
            )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "CONSOLIDATION_FAILED")
        self.assertIn("audit unavailable", result["errors"][0]["message"])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM entities WHERE id IN (?, ?) ORDER BY id",
                tuple(self.parents),
            ).fetchall(),
            [("raw",), ("raw",)],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE title = ?",
                ("Commit canonical result",),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type = ?", ("consolidation_gate_override",)
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
