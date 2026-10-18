import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
import json
import sqlite3

from saltmdb.domain.services import relation_service as relations

from saltmdb.config import MAX_MEMORY_CONTENT_CHARS
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import (
    bulk_consolidate_memories,
    consolidate_memories,
    store_relation,
)


class TestConsolidateMemoriesPhase4(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _parent(self, title: str) -> str:
        result = store_memory(
            title=title,
            content=f"Detailed, durable description of {title} used in consolidation tests.",
            agent_id="agent_db",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def _cohesion(self, ids, _conn, _db_path):
        state = {
            row[0]: (row[1], row[2])
            for row in self.conn.execute(
                f"SELECT id, content_hash, status FROM entities WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        }
        vectors = {entity_id: [1.0, 0.0] for entity_id in ids}
        return vectors, {}, state

    def test_success_archives_parents_and_returns_optional_worklist_without_repointing(self):
        parent_a = self._parent("Parent A")
        parent_b = self._parent("Parent B")
        neighbor = self._parent("Neighbor")
        parent_bytes = self.conn.execute(
            "SELECT title, full_content, content_hash, parent_ids FROM entities WHERE id = ?",
            (parent_a,),
        ).fetchone()
        relation = store_relation(
            source_id=parent_a,
            target_id=neighbor,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertNotIn("Error", relation)
        before = self.conn.execute(
            "SELECT source_id, target_id, predicate, valid_to FROM relations "
            "WHERE source_id = ? AND target_id = ?",
            (parent_a, neighbor),
        ).fetchone()

        with patch.object(
            __import__(
                "saltmdb.domain.services.relation_service", fromlist=["get_fresh_entity_centroids"]
            ),
            "get_fresh_entity_centroids",
            side_effect=self._cohesion,
        ):
            result = consolidate_memories(
                parent_ids=[parent_a, parent_b],
                title="Canonical summary",
                content="A coherent canonical summary of both parent memories.",
                agent_id="agent_db",
                db_connection=self.conn,
            )

        self.assertEqual(result["status"], "ok")
        data = result["data"]
        self.assertEqual(len(data["orphaned_relations"]), 1)
        item = data["orphaned_relations"][0]
        self.assertEqual(item["predicate"], "related_to")
        self.assertEqual(item["other_endpoint"], neighbor)
        self.assertEqual(item["originating_parent"], parent_a)
        self.assertEqual(item["source_id"], parent_a)
        self.assertEqual(item["target_id"], neighbor)

        after = self.conn.execute(
            "SELECT source_id, target_id, predicate, valid_to FROM relations "
            "WHERE source_id = ? AND target_id = ?",
            (parent_a, neighbor),
        ).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE predicate = 'related_to'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE predicate = 'consolidated_from' AND source_id = ?",
                (data["entity_id"],),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT title, full_content, content_hash, parent_ids FROM entities WHERE id = ?",
                (parent_a,),
            ).fetchone(),
            parent_bytes,
        )

    def test_worklist_deduplicates_relation_touching_both_parents(self):
        parent_a = self._parent("Two-Sided Parent A")
        parent_b = self._parent("Two-Sided Parent B")
        outsider = self._parent("Two-Sided Outsider")
        for source_id, target_id in (
            (parent_a, parent_b),
            (parent_a, outsider),
            (parent_b, outsider),
        ):
            result = store_relation(
                source_id=source_id,
                target_id=target_id,
                predicate="related_to",
                db_connection=self.conn,
            )
            self.assertNotIn("Error", result)

        with patch.object(
            __import__(
                "saltmdb.domain.services.relation_service", fromlist=["get_fresh_entity_centroids"]
            ),
            "get_fresh_entity_centroids",
            side_effect=self._cohesion,
        ):
            result = consolidate_memories(
                parent_ids=[parent_a, parent_b],
                title="Two-Sided Canonical",
                content="A canonical summary for the two-sided relation worklist.",
                agent_id="agent_db",
                db_connection=self.conn,
            )

        self.assertEqual(result["status"], "ok")
        worklist = result["data"]["orphaned_relations"]
        self.assertEqual(len(worklist), 3)
        triples = {(item["source_id"], item["target_id"], item["predicate"]) for item in worklist}
        self.assertEqual(
            triples,
            {
                (parent_a, parent_b, "related_to"),
                (parent_a, outsider, "related_to"),
                (parent_b, outsider, "related_to"),
            },
        )
        self.assertEqual(len(triples), len(worklist))

    def test_one_parent_unknown_and_archived_parent_reject_with_zero_writes(self):
        parent = self._parent("Only Parent")
        before_entities = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        one = consolidate_memories(
            parent_ids=[parent],
            title="Should reject",
            content="A valid enough summary that should not be written.",
            db_connection=self.conn,
        )
        self.assertEqual(one["status"], "rejected")
        self.assertEqual(one["errors"][0]["code"], "REJECT_PARENT_COUNT")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )

        unknown = consolidate_memories(
            parent_ids=[parent, "not-an-entity"],
            title="Should reject",
            content="A valid enough summary that should not be written.",
            db_connection=self.conn,
        )
        self.assertEqual(unknown["errors"][0]["code"], "UNKNOWN_PARENT_ID")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )

        archived = self._parent("Archived Parent")
        self.conn.execute("UPDATE entities SET status = 'archived' WHERE id = ?", (archived,))
        self.conn.commit()
        inactive = consolidate_memories(
            parent_ids=[parent, archived],
            title="Should reject",
            content="A valid enough summary that should not be written.",
            db_connection=self.conn,
        )
        self.assertEqual(inactive["errors"][0]["code"], "INACTIVE_PARENT")
        self.assertIn("predicate='corrects'", inactive["errors"][0]["message"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'archived'").fetchone()[
                0
            ],
            1,
        )

    def test_consolidate_rejects_content_over_maximum(self):
        result = consolidate_memories(
            parent_ids=["not-a-real-parent"],
            title="Oversized Canonical Summary",
            content="x" * (MAX_MEMORY_CONTENT_CHARS + 1),
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "CONTENT_TOO_LONG")

    def test_bulk_success_preserves_each_item_worklist(self):
        parent_a = self._parent("Bulk Parent A")
        parent_b = self._parent("Bulk Parent B")
        neighbor = self._parent("Bulk Neighbor")
        self.assertNotIn(
            "Error",
            store_relation(
                source_id=parent_a,
                target_id=neighbor,
                predicate="related_to",
                db_connection=self.conn,
            ),
        )

        with patch.object(
            __import__(
                "saltmdb.domain.services.relation_service", fromlist=["get_fresh_entity_centroids"]
            ),
            "get_fresh_entity_centroids",
            side_effect=self._cohesion,
        ):
            results = bulk_consolidate_memories(
                consolidations=[
                    {
                        "parent_ids": [parent_a, parent_b],
                        "title": "Bulk Canonical",
                        "content": "A coherent canonical summary for the bulk operation.",
                    }
                ],
                db_connection=self.conn,
            )

        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(len(results[0]["orphaned_relations"]), 1)
        self.assertEqual(results[0]["orphaned_relations"][0]["originating_parent"], parent_a)


class TestConsolidationBoundaries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "consolidation.db")
        self.conn = init_db(self.path)
        self.addCleanup(self.conn.close)
        self.parents = []
        for parent in ("Parent A", "Parent B"):
            result = store_memory(
                title=parent,
                content=f"Original parent content used for {parent} characterization.",
                agent_id="other-owner",
                scope="private",
                db_connection=self.conn,
            )
            self.assertEqual(result["status"], "ok")
            self.parents.append(result["data"]["id"])
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
        self.addCleanup(patch.stopall)
        patch.object(
            relations,
            "get_fresh_entity_centroids",
            return_value=({parent: [1.0, 0.0] for parent in self.parents}, {}, self.state),
        ).start()
        patch.object(relations, "evaluate_memory_quality", return_value=self.quality).start()
        self.duplicates = patch.object(
            relations, "check_duplicate_memories", return_value={"duplicate_found": False}
        ).start()
        patch("saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity").start()

    def call(self, **kwargs):
        arguments = {
            "parent_ids": self.parents,
            "title": "Canonical result",
            "content": "Canonical durable content for the merged result.",
            "db_connection": self.conn,
        }
        arguments.update(kwargs)
        return consolidate_memories(**arguments)

    def assert_rejected(self, result, code, message):
        self.assertEqual(
            result,
            {"status": "rejected", "errors": [{"code": code, "message": message}], "warnings": []},
        )
        self.assertEqual(
            self.conn.execute("SELECT id, status FROM entities ORDER BY id").fetchall(),
            sorted((parent, "raw") for parent in self.parents),
        )

    def test_input_rejections_precede_connection_and_parent_work(self):
        with patch.object(relations, "get_connection", side_effect=AssertionError("opened")):
            for parents in (None, [], "parent-a"):
                with self.subTest(parents=parents):
                    self.assert_rejected(
                        self.call(parent_ids=parents, db_connection=None),
                        "INVALID_PARENT_IDS",
                        "parent_ids must be a non-empty list of UUID strings.",
                    )
            for kwargs in ({"title": ""}, {"content": ""}):
                self.assert_rejected(
                    self.call(**kwargs, db_connection=None),
                    "MISSING_REQUIRED_FIELDS",
                    "title and content are mandatory.",
                )
            self.assert_rejected(
                self.call(content="x" * (MAX_MEMORY_CONTENT_CHARS + 1), db_connection=None),
                "CONTENT_TOO_LONG",
                f"content exceeds {MAX_MEMORY_CONTENT_CHARS} characters (got {MAX_MEMORY_CONTENT_CHARS + 1}).",
            )

    def test_content_maximum_is_inclusive(self):
        result = self.call(content="x" * MAX_MEMORY_CONTENT_CHARS)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            self.conn.execute(
                "SELECT full_content FROM entities WHERE id = ?", (result["data"]["entity_id"],)
            ).fetchone(),
            ("x" * MAX_MEMORY_CONTENT_CHARS,),
        )

    def test_parent_validation_precedes_tags_quality_and_duplicates(self):
        for parents, code, message in (
            (
                [self.parents[0]],
                "REJECT_PARENT_COUNT",
                "consolidate_memories requires at least 2 distinct active parents; use revise_memory for a single parent.",
            ),
            (
                [self.parents[0], "missing"],
                "UNKNOWN_PARENT_ID",
                "could not resolve parent_id 'missing'.",
            ),
        ):
            with self.subTest(code=code):
                self.assert_rejected(
                    self.call(parent_ids=parents, tags=["bad::tag"]), code, message
                )
        self.duplicates.assert_not_called()

    def test_tag_and_core_parse_rejections(self):
        with patch(
            "saltmdb.domain.services.memory_service.tags.validate_tag_names",
            return_value="invalid fixture tag",
        ):
            self.assert_rejected(self.call(), "INVALID_TAG_NAME", "invalid fixture tag")
        with patch.object(
            relations.core_governance_service,
            "parse_is_core",
            side_effect=ValueError("invalid fixture core"),
        ):
            self.assert_rejected(self.call(), "INVALID_CORE_REQUEST", "invalid fixture core")
        self.duplicates.assert_not_called()

    def test_quality_rejection_is_exact_and_precedes_duplicates(self):
        self.quality.update(status="REJECT", reason="fixture rejection", quality_score=0.125)
        self.assert_rejected(
            self.call(),
            "REJECT_QUALITY",
            "Consolidation quality check rejected (Score: 0.12). Reason: fixture rejection",
        )
        self.duplicates.assert_not_called()

    def test_cohesion_threshold_equality_needs_no_override(self):
        with patch.object(
            relations,
            "min_pairwise_cohesion",
            return_value=(relations.COHESION_MIN_PAIRWISE_THRESHOLD, tuple(self.parents)),
        ):
            result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            self.conn.execute(
                "SELECT type FROM events WHERE type = 'consolidation_gate_override'"
            ).fetchall(),
            [],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT full_content FROM entities WHERE id = ?", (result["data"]["entity_id"],)
            ).fetchone(),
            ("Canonical durable content for the merged result.",),
        )

    def test_cohesion_rejection_and_stripped_override_minimum(self):
        with patch.object(
            relations, "min_pairwise_cohesion", return_value=(0.0, tuple(self.parents))
        ):
            self.assert_rejected(
                self.call(
                    override_justification=" "
                    + "x" * (relations.COHESION_OVERRIDE_MIN_LENGTH - 1)
                    + " "
                ),
                "REJECT_LOW_COHESION",
                f"parent set fails pairwise similarity gate (min=0.0000 < {relations.COHESION_MIN_PAIRWISE_THRESHOLD}, "
                f"weakest pair={tuple(self.parents)}). Pass "
                f"override_justification (>= {relations.COHESION_OVERRIDE_MIN_LENGTH} chars) to force this merge.",
            )

    def test_unresolved_override_audit_payload_and_content_hash(self):
        justification = "x" * relations.COHESION_OVERRIDE_MIN_LENGTH
        with patch.object(
            relations,
            "get_fresh_entity_centroids",
            return_value=(
                {},
                {self.parents[1]: "no content", self.parents[0]: "second reason"},
                self.state,
            ),
        ):
            result = self.call(override_justification=" " + justification + " ", agent_id="writer")
        self.assertEqual(result["status"], "ok")
        entity_id = result["data"]["entity_id"]
        event = self.conn.execute(
            "SELECT agent_id, type, content FROM events WHERE type = 'consolidation_gate_override'"
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event[:2], ("writer", "consolidation_gate_override"))
        self.assertEqual(
            json.loads(event[2]),
            {
                "parent_ids": self.parents,
                "min_pairwise_similarity": 0.0,
                "threshold": relations.COHESION_MIN_PAIRWISE_THRESHOLD,
                "offending_pair": [self.parents[1], "<no content>"],
                "unresolved": {self.parents[1]: "no content", self.parents[0]: "second reason"},
                "justification": justification,
                "consolidated_id": entity_id,
            },
        )
        content = (
            "Canonical durable content for the merged result.\n\n---\n"
            "[Consolidation Override] This merge was forced past the automatic cohesion gate. "
            "The minimum pairwise similarity between parents was 0.0000, below the "
            f"required threshold of {relations.COHESION_MIN_PAIRWISE_THRESHOLD}. "
            "One or more parents had no usable content for scoring. "
            f"Justification: {justification}"
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT full_content, content_hash FROM entities WHERE id = ?", (entity_id,)
            ).fetchone(),
            (content, relations.compute_content_hash(content)),
        )

    def test_override_audit_failure_rolls_back(self):
        with (
            patch.object(
                relations, "min_pairwise_cohesion", return_value=(0.0, tuple(self.parents))
            ),
            patch.object(
                relations,
                "log_event",
                return_value={"status": "rejected", "errors": [{"message": "audit unavailable"}]},
            ),
        ):
            self.assert_rejected(
                self.call(override_justification="x" * relations.COHESION_OVERRIDE_MIN_LENGTH),
                "CONSOLIDATION_FAILED",
                "Failed to record consolidation override audit event: audit unavailable",
            )
        self.assertEqual(self.conn.execute("SELECT * FROM relations").fetchall(), [])

    def test_exact_duplicate_is_owner_scoped_and_excludes_parents(self):
        content = "Canonical durable content for the merged result."
        self.conn.execute(
            "UPDATE entities SET content_hash = ? WHERE id = ?",
            (relations.compute_content_hash(content), self.parents[0]),
        )
        self.conn.commit()
        self.state[self.parents[0]] = (relations.compute_content_hash(content), "raw")
        result = self.call(agent_id="other-owner")
        self.assertEqual(result["status"], "ok")
        self.duplicates.assert_called_once_with(
            title="Canonical result",
            content=content,
            agent_id="other-owner",
            exclude_ids=self.parents,
            db_connection=self.conn,
        )

    def test_unrelated_exact_duplicate_rejects_before_near_duplicate_probe(self):
        self.duplicates.stop()
        duplicate = store_memory(
            title="Other memory",
            content="Canonical durable content for the merged result.",
            agent_id="system",
            db_connection=self.conn,
        )
        self.assertEqual(duplicate["status"], "ok")
        self.duplicates.start()
        result = self.call()
        self.assertEqual(
            result,
            {
                "status": "rejected",
                "errors": [
                    {
                        "code": "REJECT_EXACT_DUPLICATE",
                        "message": f"Consolidated memory exact hash matches existing entity ID: {duplicate['data']['id']}",
                    }
                ],
                "warnings": [],
            },
        )
        self.duplicates.assert_not_called()

    def test_near_duplicate_probe_failures_are_advisory(self):
        self.duplicates.side_effect = sqlite3.OperationalError("probe unavailable")
        with self.assertLogs(relations.logger, level="WARNING") as logs:
            result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            logs.output,
            [
                "WARNING:saltmdb.domain.services.relation_service:Consolidation near-duplicate check unavailable; continuing: probe unavailable"
            ],
        )

    def test_capacity_rejection_rolls_back_before_audit(self):
        core_state = {
            "is_core": True,
            "is_new_core": True,
            "core_reason": "reason",
            "core_exit_condition": "exit",
            "core_review_after": "future",
            "core_detail_memory_ids": [],
        }
        with (
            patch.object(
                relations.core_governance_service,
                "resolve_consolidation_core_state",
                return_value=core_state,
            ),
            patch.object(relations.core_governance_service, "enforce_overdue_boundary"),
            patch.object(
                relations.core_governance_service,
                "check_capacity_admission",
                return_value="capacity fixture",
            ),
        ):
            self.assert_rejected(self.call(), "CONSOLIDATION_FAILED", "capacity fixture")

    def test_late_lineage_failure_rolls_back_entity_parents_tags_and_audit(self):
        self.conn.execute(
            "CREATE TRIGGER fail_lineage BEFORE INSERT ON relations "
            "WHEN NEW.predicate = 'consolidated_from' BEGIN SELECT RAISE(ABORT, 'lineage fixture'); END"
        )
        self.conn.commit()
        with patch.object(
            relations, "min_pairwise_cohesion", return_value=(0.0, tuple(self.parents))
        ):
            self.assert_rejected(
                self.call(
                    tags=["#merge"],
                    override_justification="x" * relations.COHESION_OVERRIDE_MIN_LENGTH,
                ),
                "CONSOLIDATION_FAILED",
                "lineage fixture",
            )
        self.assertEqual(self.conn.execute("SELECT * FROM entity_tags").fetchall(), [])
        self.assertEqual(self.conn.execute("SELECT * FROM events").fetchall(), [])

    def test_content_redaction_precedes_entity_write(self):
        result = self.call(content="Canonical password=supersecret content.")
        self.assertEqual(result["status"], "ok")
        stored = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (result["data"]["entity_id"],)
        ).fetchone()[0]
        self.assertEqual(stored, "Canonical [REDACTED_SECRET] content.")

    def test_near_duplicate_error_result_is_advisory(self):
        self.duplicates.stop()
        self.duplicates = patch.object(
            relations,
            "check_duplicate_memories",
            return_value={
                "duplicate_found": True,
                "error": "probe unavailable",
                "potential_duplicates": [{"title": "Near", "id": "near-id"}],
            },
        ).start()
        with self.assertNoLogs(relations.logger, level="WARNING"):
            result = self.call()
        self.assertEqual(result["status"], "ok")

    def test_success_persists_entity_fields_status_and_tags(self):
        result = self.call(
            tags=["#merge"],
            scope="private",
            weight=7,
            context_id="context-fixture",
            agent_session_id="session-fixture",
            memory_type="decision",
            metadata={"fixture": "value"},
        )

        self.assertEqual(result["status"], "ok")
        entity_id = result["data"]["entity_id"]
        row = self.conn.execute(
            "SELECT status, scope, weight, context_id, agent_session_id, memory_type, metadata "
            "FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(
            row,
            (
                "consolidated",
                "private",
                7,
                "context-fixture",
                "session-fixture",
                "decision",
                '{"fixture": "value"}',
            ),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entity_tags et JOIN tags t ON t.id = et.tag_id "
                "WHERE et.entity_id = ? AND t.name = ?",
                (entity_id, "#merge"),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE status = 'archived' AND id IN (?, ?)",
                tuple(self.parents),
            ).fetchone()[0],
            2,
        )

    def test_near_duplicate_warning_is_reported(self):
        self.duplicates.stop()
        self.duplicates = patch.object(
            relations,
            "check_duplicate_memories",
            return_value={
                "duplicate_found": True,
                "potential_duplicates": [{"title": "Near", "id": "near-id"}],
            },
        ).start()
        with self.assertLogs(relations.logger, level="WARNING") as logs:
            result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            logs.output,
            [
                "WARNING:saltmdb.domain.services.relation_service:Consolidation potential near-duplicate detected against unrelated memory 'Near' (ID: near-id)"
            ],
        )

    def test_override_justification_minimum_length_is_inclusive(self):
        with patch.object(
            relations, "min_pairwise_cohesion", return_value=(0.0, tuple(self.parents))
        ):
            result = self.call(
                override_justification="x" * relations.COHESION_OVERRIDE_MIN_LENGTH,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type = 'consolidation_gate_override'"
            ).fetchone()[0],
            1,
        )

    def test_tag_resolution_without_id_skips_only_that_tag(self):
        real_resolve = relations.resolve_or_create_tag

        def resolve(conn, tag_name, agent_id):
            if tag_name == "#missing":
                return None
            return real_resolve(conn, tag_name, agent_id=agent_id)

        with patch.object(relations, "resolve_or_create_tag", side_effect=resolve):
            result = self.call(tags=["#keep", "#missing"])

        self.assertEqual(result["status"], "ok")
        entity_id = result["data"]["entity_id"]
        self.assertEqual(
            self.conn.execute(
                "SELECT t.name FROM entity_tags et JOIN tags t ON t.id = et.tag_id "
                "WHERE et.entity_id = ? ORDER BY t.name",
                (entity_id,),
            ).fetchall(),
            [("#keep",)],
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entity_tags WHERE entity_id = ? AND tag_id IS NULL",
                (entity_id,),
            ).fetchone()[0],
            0,
        )

    def test_override_audit_preserves_full_precision_minimum_similarity(self):
        with patch.object(
            relations,
            "min_pairwise_cohesion",
            return_value=(0.123456789, tuple(self.parents)),
        ):
            result = self.call(override_justification="x" * relations.COHESION_OVERRIDE_MIN_LENGTH)
        self.assertEqual(result["status"], "ok")
        event = self.conn.execute(
            "SELECT content FROM events WHERE type = 'consolidation_gate_override'"
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(
            json.loads(event[0])["min_pairwise_similarity"],
            0.123456789,
        )

    def test_core_capacity_admission_excludes_resolved_parents(self):
        core_state = {
            "is_core": True,
            "is_new_core": True,
            "core_reason": "reason",
            "core_exit_condition": "exit",
            "core_review_after": "future",
            "core_detail_memory_ids": [],
        }
        with (
            patch.object(
                relations.core_governance_service,
                "resolve_consolidation_core_state",
                return_value=core_state,
            ),
            patch.object(relations.core_governance_service, "enforce_overdue_boundary"),
            patch.object(
                relations.core_governance_service,
                "check_capacity_admission",
                return_value=None,
            ) as admission,
        ):
            result = self.call(
                is_core=True,
                core_reason="reason",
                core_exit_condition="exit",
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(admission.call_args.kwargs["exclude_ids"], self.parents)

    def test_core_capacity_payload_defaults_memory_type_to_fact(self):
        core_state = {
            "is_core": True,
            "is_new_core": True,
            "core_reason": "reason",
            "core_exit_condition": "exit",
            "core_review_after": "future",
            "core_detail_memory_ids": [],
        }
        with (
            patch.object(
                relations.core_governance_service,
                "resolve_consolidation_core_state",
                return_value=core_state,
            ),
            patch.object(relations.core_governance_service, "enforce_overdue_boundary"),
            patch.object(
                relations.core_governance_service,
                "check_capacity_admission",
                return_value=None,
            ) as admission,
        ):
            result = self.call(
                is_core=True,
                core_reason="reason",
                core_exit_condition="exit",
                memory_type=None,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(admission.call_args.kwargs["new_entry"]["memory_type"], "fact")

    def test_duplicate_tag_in_one_call_is_inserted_once(self):
        result = self.call(tags=["#duplicate", "#duplicate"])
        self.assertEqual(result["status"], "ok")
        entity_id = result["data"]["entity_id"]
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entity_tags et JOIN tags t ON t.id = et.tag_id "
                "WHERE et.entity_id = ? AND t.name = '#duplicate'",
                (entity_id,),
            ).fetchone()[0],
            1,
        )

    def test_unresolved_tag_before_later_tag_does_not_stop_resolution(self):
        real_resolve = relations.resolve_or_create_tag

        def resolve(conn, tag_name, agent_id):
            if tag_name == "#missing-first":
                return None
            return real_resolve(conn, tag_name, agent_id=agent_id)

        with patch.object(relations, "resolve_or_create_tag", side_effect=resolve):
            result = self.call(tags=["#missing-first", "#keep-after"])

        self.assertEqual(result["status"], "ok")
        entity_id = result["data"]["entity_id"]
        self.assertEqual(
            self.conn.execute(
                "SELECT t.name FROM entity_tags et JOIN tags t ON t.id = et.tag_id "
                "WHERE et.entity_id = ? ORDER BY t.name",
                (entity_id,),
            ).fetchall(),
            [("#keep-after",)],
        )

    def test_incoming_worklist_preserves_originating_parent_and_other_endpoint(self):
        outsider_result = store_memory(
            title="Incoming Outsider",
            content="Incoming outsider content for worklist coverage.",
            agent_id="other-owner",
            scope="private",
            db_connection=self.conn,
        )
        self.assertEqual(outsider_result["status"], "ok")
        outsider = outsider_result["data"]["id"]
        relation = store_relation(
            source_id=outsider,
            target_id=self.parents[0],
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertNotIn("Error", relation)
        result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["data"]["orphaned_relations"],
            [
                {
                    "predicate": "related_to",
                    "other_endpoint": outsider,
                    "originating_parent": self.parents[0],
                    "source_id": outsider,
                    "target_id": self.parents[0],
                }
            ],
        )

    def test_db_path_none_uses_database_path_fallback_for_cohesion(self):
        with (
            patch.object(relations, "get_db_path", return_value="/fallback/db") as fallback,
            patch.object(
                relations,
                "get_fresh_entity_centroids",
                return_value=(
                    {parent: [1.0, 0.0] for parent in self.parents},
                    {},
                    self.state,
                ),
            ) as centroids,
        ):
            result = self.call(db_path=None)
        self.assertEqual(result["status"], "ok")
        fallback.assert_called_once_with()
        self.assertEqual(centroids.call_args.args[2], "/fallback/db")

    def test_override_annotation_omits_unresolved_sentence_when_all_parents_resolve(self):
        with patch.object(
            relations,
            "min_pairwise_cohesion",
            return_value=(0.0, tuple(self.parents)),
        ):
            result = self.call(override_justification="x" * relations.COHESION_OVERRIDE_MIN_LENGTH)
        self.assertEqual(result["status"], "ok")
        content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (result["data"]["entity_id"],)
        ).fetchone()[0]
        self.assertIn("[Consolidation Override]", content)
        self.assertNotIn("One or more parents had no usable content for scoring.", content)

    def test_self_opened_connection_closes_on_low_cohesion_rejection(self):
        with (
            patch.object(relations, "get_connection", return_value=self.conn),
            patch.object(relations, "close_connection") as close,
            patch.object(
                relations,
                "min_pairwise_cohesion",
                return_value=(0.0, tuple(self.parents)),
            ),
        ):
            result = self.call(db_connection=None, db_path=self.path)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "REJECT_LOW_COHESION")
        close.assert_called_once_with(self.conn)

    def test_self_opened_connection_closes_on_parent_rejection(self):
        with (
            patch.object(relations, "get_connection", return_value=self.conn),
            patch.object(relations, "close_connection") as close,
        ):
            result = self.call(
                parent_ids=[self.parents[0], "missing"],
                db_connection=None,
                db_path=self.path,
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "UNKNOWN_PARENT_ID")
        close.assert_called_once_with(self.conn)

    def test_title_redaction_precedes_entity_write(self):
        result = self.call(title="Canonical password=supersecret")
        self.assertEqual(result["status"], "ok")
        stored = self.conn.execute(
            "SELECT title FROM entities WHERE id = ?", (result["data"]["entity_id"],)
        ).fetchone()[0]
        self.assertEqual(stored, "Canonical [REDACTED_SECRET]")

    def test_content_hash_is_computed_after_redaction(self):
        raw = "Canonical password=supersecret content."
        redacted = "Canonical [REDACTED_SECRET] content."
        result = self.call(content=raw)
        self.assertEqual(result["status"], "ok")
        stored = self.conn.execute(
            "SELECT full_content, content_hash FROM entities WHERE id = ?",
            (result["data"]["entity_id"],),
        ).fetchone()
        self.assertEqual(stored, (redacted, relations.compute_content_hash(redacted)))
        self.assertNotEqual(stored[1], relations.compute_content_hash(raw))

    def test_quality_gate_receives_redacted_title_and_content(self):
        with patch.object(
            relations,
            "evaluate_memory_quality",
            return_value=self.quality,
        ) as quality:
            result = self.call(
                title="Canonical password=supersecret",
                content="Canonical password=supersecret content.",
            )
        self.assertEqual(result["status"], "ok")
        quality.assert_called_once_with(
            "Canonical [REDACTED_SECRET] content.",
            "Canonical [REDACTED_SECRET]",
        )

    def test_worklist_returns_exact_full_relation_rows(self):
        outsider_result = store_memory(
            title="Exact Worklist Outsider",
            content="Exact worklist outsider content for coverage.",
            agent_id="other-owner",
            scope="private",
            db_connection=self.conn,
        )
        self.assertEqual(outsider_result["status"], "ok")
        outsider = outsider_result["data"]["id"]
        for source_id, target_id in (
            (self.parents[0], self.parents[1]),
            (outsider, self.parents[0]),
        ):
            relation = store_relation(
                source_id=source_id,
                target_id=target_id,
                predicate="related_to",
                db_connection=self.conn,
            )
            self.assertNotIn("Error", relation)
        result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["data"]["orphaned_edge_worklist"],
            [
                {
                    "predicate": "related_to",
                    "other_endpoint": self.parents[1],
                    "originating_parent": self.parents[0],
                    "source_id": self.parents[0],
                    "target_id": self.parents[1],
                },
                {
                    "predicate": "related_to",
                    "other_endpoint": outsider,
                    "originating_parent": self.parents[0],
                    "source_id": outsider,
                    "target_id": self.parents[0],
                },
            ],
        )

    def test_worklist_guidance_is_exact(self):
        result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["data"]["worklist_guidance"],
            "Re-declaring these relations through manage_relation is optional; "
            "skipping them leaves a correct historical graph.",
        )


if __name__ == "__main__":
    unittest.main()
