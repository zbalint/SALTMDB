from saltmdb.domain.services.memory_service import CoreDeclaration, EntityAnnotations, Provenance
import inspect
from datetime import datetime, timedelta, timezone
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from saltmdb.daemon.dispatch import _dispatch_consolidate_memories
from saltmdb.db.connection import write_transaction
from saltmdb.db.schema import init_db
from saltmdb.domain.services import relation_service
from saltmdb.domain.services.memory_service import (
    store_memory,
)
from saltmdb.domain.services.relation_service import (
    _BulkContext,
    commit_consolidation,
    consolidate_memories,
    bulk_commit_consolidation,
)


class TestConsolidationGrouping(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        self.embed_patcher = patch(
            "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"
        )
        self.embed_patcher.start()
        self.addCleanup(self.embed_patcher.stop)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _parent(self, title: str, agent_id: str = "parent-agent") -> str:
        result = store_memory(
            title=title,
            content=f"Durable consolidation grouping fixture for {title}.",
            db_connection=self.conn,
            provenance=Provenance(agent_id=agent_id),
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def _state(self, ids):
        return {
            row[0]: (row[1], row[2])
            for row in self.conn.execute(
                f"SELECT id, content_hash, status FROM entities WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            )
        }

    def _cohesion(self, ids, _conn):
        return ({entity_id: [1.0, 0.0] for entity_id in ids}, {}, self._state(ids))

    def test_group_defaults_are_frozen_and_signature_is_keyword_only(self):
        self.assertEqual(CoreDeclaration(), CoreDeclaration(None, None, None, None, None))
        self.assertEqual(EntityAnnotations(), EntityAnnotations(None, None))
        for value, field in (
            (CoreDeclaration(), "is_core"),
            (EntityAnnotations(), "memory_type"),
            (_BulkContext(None, None, None), "precomputed_centroids"),
        ):
            with self.subTest(field=field):
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(value, field, "changed")
        signature = inspect.signature(consolidate_memories)
        self.assertLessEqual(len(signature.parameters), 13)
        self.assertEqual(
            [
                name
                for name, parameter in signature.parameters.items()
                if parameter.kind is parameter.KEYWORD_ONLY
            ],
            ["provenance", "core", "annotations", "_bulk"],
        )
        parent_ids = ["a", "b"]
        for keyword in ("agent_id", "is_core", "metadata", "_in_transaction"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(TypeError):
                    consolidate_memories(
                        parent_ids,
                        "Grouping rejected keyword",
                        "Durable grouping content.",
                        **{keyword: None},
                    )

    def test_provenance_and_annotations_reach_the_stored_entity(self):
        parent_ids = [self._parent("Provenance A"), self._parent("Provenance B")]
        with patch.object(
            relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
        ):
            result = consolidate_memories(
                parent_ids=parent_ids,
                title="Grouped Provenance Result",
                content="A durable grouped result with distinct provenance and annotations.",
                provenance=Provenance(
                    agent_id="writer-distinct",
                    context_id="context-distinct",
                    agent_session_id="session-distinct",
                ),
                annotations=EntityAnnotations(
                    metadata={"fixture": "grouped"}, memory_type="decision"
                ),
                scope="shared",
                weight=5,
                db_connection=self.conn,
            )
        self.assertEqual(result["status"], "ok")
        row = self.conn.execute(
            "SELECT agent_id, context_id, agent_session_id, memory_type, metadata, scope, weight "
            "FROM entities WHERE id = ?",
            (result["data"]["entity_id"],),
        ).fetchone()
        self.assertEqual(
            tuple(row[:4]), ("writer-distinct", "context-distinct", "session-distinct", "decision")
        )
        self.assertEqual(row[4], '{"fixture": "grouped"}')
        self.assertEqual(row[5:], ("shared", 5))

    def test_omitted_provenance_preserves_old_defaults(self):
        parent_ids = [self._parent("Default A"), self._parent("Default B")]
        with patch.object(
            relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
        ):
            result = consolidate_memories(
                parent_ids=parent_ids,
                title="Grouped Default Result",
                content="A durable grouped result with the old omitted provenance defaults.",
                db_connection=self.conn,
            )
        row = self.conn.execute(
            "SELECT agent_id, context_id, agent_session_id, memory_type, metadata FROM entities WHERE id = ?",
            (result["data"]["entity_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("system", None, None, "fact", None))

    def test_bulk_item_provenance_uses_item_agent_context_and_batch_session(self):
        first = [self._parent("Bulk A"), self._parent("Bulk B")]
        with patch.object(
            relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
        ):
            result = bulk_commit_consolidation(
                [
                    {
                        "parent_ids": first,
                        "title": "Grouped Bulk Result",
                        "content": "A durable bulk grouped result with item-level provenance.",
                        "agent_id": "item-agent",
                        "context_id": "item-context",
                        "agent_session_id": "item-session-ignored",
                    }
                ],
                agent_id="batch-agent",
                context_id="batch-context",
                agent_session_id="batch-session",
                db_connection=self.conn,
            )
        self.assertEqual(result[0]["status"], "success")
        row = self.conn.execute(
            "SELECT agent_id, context_id, agent_session_id FROM entities WHERE id = ?",
            (result[0]["entity_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("item-agent", "item-context", "batch-session"))

    def test_bulk_context_presence_skips_nested_transaction(self):
        parent_ids = [self._parent("Bulk Context A"), self._parent("Bulk Context B")]
        bulk = _BulkContext(None, None, self._state(parent_ids))
        with (
            patch.object(
                relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
            ),
            patch.object(relation_service, "write_transaction_retrying") as transaction,
        ):
            result = consolidate_memories(
                parent_ids=parent_ids,
                title="Bulk Context Result",
                content="A durable result committed through an existing bulk transaction.",
                db_connection=self.conn,
                _bulk=bulk,
            )
        self.assertEqual(result["status"], "ok")
        transaction.assert_not_called()

    def test_dispatcher_groups_wire_fields_and_alias_forwards_keywords(self):
        expected = {"status": "ok", "data": {"message": "ok"}, "warnings": []}
        with patch.object(
            relation_service, "consolidate_memories", return_value=expected
        ) as consolidate:
            result = _dispatch_consolidate_memories(
                parent_ids=["parent-a", "parent-b"],
                title="Dispatcher Grouping",
                content="A durable dispatcher grouping probe.",
                agent_id="dispatch-agent",
                context_id="dispatch-context",
                agent_session_id="dispatch-session",
                is_core=True,
                core_reason="dispatch-core-reason",
                core_exit_condition="dispatch-core-exit",
                core_review_after="2099-01-01T00:00:00+00:00",
                detail_memory_ids=["detail-id"],
            )
        self.assertEqual(result, expected)
        kwargs = consolidate.call_args.kwargs
        self.assertEqual(
            kwargs["provenance"],
            Provenance("dispatch-agent", "dispatch-context", "dispatch-session"),
        )
        self.assertEqual(
            kwargs["core"],
            CoreDeclaration(
                True,
                "dispatch-core-reason",
                "dispatch-core-exit",
                "2099-01-01T00:00:00+00:00",
                ["detail-id"],
            ),
        )
        with patch.object(
            relation_service, "consolidate_memories", return_value=expected
        ) as consolidate:
            commit_consolidation(
                parent_ids=["parent-a", "parent-b"],
                title="Alias Grouping",
                content="A durable alias grouping probe.",
                provenance=Provenance(agent_id="alias-agent"),
            )
        self.assertEqual(
            consolidate.call_args.kwargs["provenance"], Provenance(agent_id="alias-agent")
        )

    def test_partial_provenance_keeps_old_system_and_none_defaults(self):
        parent_ids = [self._parent("Partial A"), self._parent("Partial B")]
        with patch.object(
            relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
        ):
            result = consolidate_memories(
                parent_ids=parent_ids,
                title="Grouped Partial Result",
                content="A durable grouped result with only context provenance supplied.",
                provenance=Provenance(context_id="partial-context"),
                db_connection=self.conn,
            )
        row = self.conn.execute(
            "SELECT agent_id, context_id, agent_session_id FROM entities WHERE id = ?",
            (result["data"]["entity_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("system", "partial-context", None))

    def _core_parent(self, title: str) -> str:
        result = store_memory(
            title=title,
            content=f"Durable active core fixture for {title}.",
            db_connection=self.conn,
            provenance=Provenance(agent_id="core-parent-agent"),
            core=CoreDeclaration(
                is_core=True,
                core_reason="A distinct core reason for this grouping test fixture.",
                core_exit_condition="A distinct exit condition for this grouping test fixture.",
            ),
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def test_core_tri_state_and_explicit_demotion(self):
        for declaration in (None, CoreDeclaration(), CoreDeclaration(is_core=None)):
            with self.subTest(declaration=declaration):
                core_id = self._core_parent(f"Tri-state Core {declaration}")
                plain_id = self._parent(f"Tri-state Plain {declaration}")
                with patch.object(
                    relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
                ):
                    result = consolidate_memories(
                        parent_ids=[core_id, plain_id],
                        title="Grouped Omitted Core",
                        content="A durable grouped result that omits the core decision.",
                        core=declaration,
                        db_connection=self.conn,
                    )
                self.assertEqual(result["status"], "rejected")
                self.assertIn("active core", result["errors"][0]["message"])
                self.conn.execute(
                    "UPDATE entities SET status = 'archived' WHERE id IN (?, ?)",
                    (core_id, plain_id),
                )

        core_id = self._core_parent("Demotion Core")
        plain_id = self._parent("Demotion Plain")
        with patch.object(
            relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
        ):
            result = consolidate_memories(
                parent_ids=[core_id, plain_id],
                title="Grouped Demotion Result",
                content="A durable grouped result that explicitly demotes core status.",
                core=CoreDeclaration(is_core=False),
                db_connection=self.conn,
            )
        self.assertEqual(result["status"], "ok")
        is_core = self.conn.execute(
            "SELECT is_core FROM entities WHERE id = ?", (result["data"]["entity_id"],)
        ).fetchone()[0]
        self.assertEqual(is_core, 0)

    def test_explicit_core_declaration_persists_lifecycle_and_detail_relation(self):
        core_id = self._core_parent("Explicit Core Parent")
        plain_id = self._parent("Explicit Core Partner")
        detail_id = self._parent("Explicit Core Detail")
        content = f"Explicit Core Result documents Explicit Core Detail ({detail_id}) as detail."
        review_after = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        with patch.object(
            relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
        ):
            result = consolidate_memories(
                parent_ids=[core_id, plain_id],
                title="Explicit Core Result",
                content=content,
                core=CoreDeclaration(
                    is_core=True,
                    core_reason="Stored explicit core reason with distinct test evidence.",
                    core_exit_condition="Stored explicit core exit condition with distinct evidence.",
                    core_review_after=review_after,
                    detail_memory_ids=[detail_id],
                ),
                db_connection=self.conn,
            )
        self.assertEqual(result["status"], "ok")
        row = self.conn.execute(
            "SELECT is_core, core_reason, core_exit_condition, core_review_after FROM entities WHERE id = ?",
            (result["data"]["entity_id"],),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                1,
                "Stored explicit core reason with distinct test evidence.",
                "Stored explicit core exit condition with distinct evidence.",
                review_after,
            ),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT source_id, target_id, predicate FROM relations WHERE predicate = 'elaborates_on'"
            ).fetchall(),
            [(detail_id, result["data"]["entity_id"], "elaborates_on")],
        )

    def test_without_bulk_context_transaction_wrapper_is_used(self):
        parent_ids = [self._parent("Transaction A"), self._parent("Transaction B")]

        def _run_in_transaction(conn, callback):
            with write_transaction(conn) as tx_conn:
                return callback(tx_conn)

        with (
            patch.object(
                relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
            ),
            patch.object(
                relation_service,
                "write_transaction_retrying",
                side_effect=lambda conn, callback: _run_in_transaction(conn, callback),
            ) as transaction,
        ):
            result = consolidate_memories(
                parent_ids=parent_ids,
                title="Grouped Transaction Result",
                content="A durable grouped result using the normal transaction wrapper.",
                db_connection=self.conn,
            )
        self.assertEqual(result["status"], "ok")
        transaction.assert_called_once()

    def test_empty_precomputed_maps_are_used_without_fresh_recomputation(self):
        parent_ids = [self._parent("Precomputed A"), self._parent("Precomputed B")]
        bulk = _BulkContext({}, {}, self._state(parent_ids))
        with patch.object(
            relation_service,
            "get_fresh_entity_centroids",
            side_effect=AssertionError("fresh centroids must not be called"),
        ):
            result = consolidate_memories(
                parent_ids=parent_ids,
                title="Grouped Precomputed Result",
                content="A durable grouped result using empty precomputed maps.",
                db_connection=self.conn,
                _bulk=bulk,
            )
        self.assertEqual(result["status"], "ok")

    def test_non_empty_precomputed_maps_are_forwarded_without_fresh_recomputation(self):
        parent_ids = [
            self._parent("Precomputed Nonempty A"),
            self._parent("Precomputed Nonempty B"),
        ]
        bulk = _BulkContext(
            {parent_id: [1.0, 0.0] for parent_id in parent_ids},
            {},
            self._state(parent_ids),
        )
        with patch.object(
            relation_service,
            "get_fresh_entity_centroids",
            side_effect=AssertionError("fresh centroids must not be called"),
        ):
            result = consolidate_memories(
                parent_ids=parent_ids,
                title="Grouped Nonempty Precomputed Result",
                content="A durable grouped result using non-empty precomputed maps.",
                db_connection=self.conn,
                _bulk=bulk,
            )
        self.assertEqual(result["status"], "ok")

    def test_dispatcher_round_trip_stores_wire_grouped_values(self):
        old_db_path = os.environ.get("SALTMDB_DB_PATH")
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        try:
            parent_ids = [self._parent("Dispatch Parent A"), self._parent("Dispatch Parent B")]
            detail_id = self._parent("Dispatch Detail")
            detail_content = (
                f"Dispatch Result records Dispatch Detail ({detail_id}) as a grouped detail."
            )
            with patch.object(
                relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
            ):
                result = _dispatch_consolidate_memories(
                    parent_ids=parent_ids,
                    title="Dispatch Grouped Stored Result",
                    content=detail_content,
                    agent_id="wire-agent",
                    context_id="wire-context",
                    agent_session_id="wire-session",
                    is_core=False,
                    scope="private",
                    weight=3,
                )
            self.assertEqual(result["status"], "ok")
            row = self.conn.execute(
                "SELECT agent_id, context_id, agent_session_id, is_core, core_reason, "
                "core_exit_condition, scope, weight FROM entities WHERE id = ?",
                (result["data"]["entity_id"],),
            ).fetchone()
            self.assertEqual(
                tuple(row),
                (
                    "wire-agent",
                    "wire-context",
                    "wire-session",
                    0,
                    None,
                    None,
                    "private",
                    3,
                ),
            )
        finally:
            if old_db_path is None:
                os.environ.pop("SALTMDB_DB_PATH", None)
            else:
                os.environ["SALTMDB_DB_PATH"] = old_db_path

    def test_dispatcher_preserves_shared_scope_against_private_default(self):
        old_db_path = os.environ.get("SALTMDB_DB_PATH")
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        try:
            parent_ids = [self._parent("Shared Dispatch A"), self._parent("Shared Dispatch B")]
            with patch.object(
                relation_service, "get_fresh_entity_centroids", side_effect=self._cohesion
            ):
                result = _dispatch_consolidate_memories(
                    parent_ids=parent_ids,
                    title="Shared Dispatch Result",
                    content="A durable shared-scope dispatcher result.",
                    scope="shared",
                    weight=4,
                )
            self.assertEqual(result["status"], "ok")
            row = self.conn.execute(
                "SELECT scope, weight FROM entities WHERE id = ?",
                (result["data"]["entity_id"],),
            ).fetchone()
            self.assertEqual(tuple(row), ("shared", 4))
        finally:
            if old_db_path is None:
                os.environ.pop("SALTMDB_DB_PATH", None)
            else:
                os.environ["SALTMDB_DB_PATH"] = old_db_path


if __name__ == "__main__":
    unittest.main()
