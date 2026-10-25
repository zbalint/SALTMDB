import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from saltmdb.daemon.dispatch import _dispatch_store_memory
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    CoreDeclaration,
    EntityAnnotations,
    Provenance,
    StoreRuntime,
    StoreScores,
    store_memory,
    RETRIEVAL_TEXT_UNSET,
)


class TestStoreMemorySignature(unittest.TestCase):
    def test_signature_has_thirteen_parameters_and_only_content_positional(self):
        signature = inspect.signature(store_memory)
        self.assertEqual(len(signature.parameters), 13)
        self.assertEqual(
            signature.parameters["content"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        self.assertTrue(
            all(
                p.kind is inspect.Parameter.KEYWORD_ONLY
                for p in list(signature.parameters.values())[1:]
            )
        )

    def test_removed_flat_names_and_extra_positionals_are_rejected(self):
        removed: dict[str, Any] = {
            "agent_id": "owner",
            "context_id": "context",
            "agent_session_id": "session",
            "is_core": False,
            "core_reason": "reason",
            "core_exit_condition": "exit",
            "core_review_after": None,
            "detail_memory_ids": [],
            "metadata": {},
            "memory_type": "fact",
            "weight": 1,
            "relevance": 1,
            "impact": 1,
            "novelty": 1,
            "actionability": 1,
            "coordinator": object(),
            "probe_sink": [],
        }
        for name, value in removed.items():
            with self.subTest(name=name), self.assertRaises(TypeError):
                cast(Any, store_memory)(**{name: value})
        with self.assertRaises(TypeError):
            cast(Any, store_memory)("content", [])

    def test_group_defaults_match_literal_values_and_kept_defaults(self):
        signature = inspect.signature(store_memory)
        self.assertEqual(signature.parameters["content"].default, None)
        self.assertIsNone(signature.parameters["tags"].default)
        self.assertEqual(signature.parameters["scope"].default, "shared")
        self.assertEqual(signature.parameters["title"].default, None)
        self.assertEqual(signature.parameters["entity_id"].default, None)
        self.assertIs(signature.parameters["retrieval_text"].default, RETRIEVAL_TEXT_UNSET)
        self.assertEqual(signature.parameters["db_connection"].default, None)
        self.assertEqual(signature.parameters["db_path"].default, None)

        provenance = signature.parameters["provenance"].default
        self.assertIsNone(provenance.agent_id)
        self.assertIsNone(provenance.context_id)
        self.assertIsNone(provenance.agent_session_id)
        core = signature.parameters["core"].default
        self.assertIsNone(core.is_core)
        self.assertIsNone(core.core_reason)
        self.assertIsNone(core.core_exit_condition)
        self.assertIsNone(core.core_review_after)
        self.assertIsNone(core.detail_memory_ids)
        annotations = signature.parameters["annotations"].default
        self.assertIsNone(annotations.metadata)
        self.assertIsNone(annotations.memory_type)
        scores = signature.parameters["scores"].default
        self.assertEqual(scores.weight, 1)
        self.assertIsNone(scores.relevance)
        self.assertIsNone(scores.impact)
        self.assertIsNone(scores.novelty)
        self.assertIsNone(scores.actionability)
        runtime = signature.parameters["runtime"].default
        self.assertIsNone(runtime.coordinator)
        self.assertIsNone(runtime.probe_sink)

        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(str(Path(directory) / "default-weight.db"))
            result = store_memory(
                "Default score content is long enough to satisfy the quality gate and exercise the normal store path.",
                title="Default Weight",
                provenance=Provenance(agent_id="owner"),
                db_connection=connection,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                connection.execute("SELECT weight FROM entities").fetchone()[0],
                1,
            )
            connection.close()

    def test_content_only_positional_call_stores_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(str(Path(directory) / "positional.db"))
            with patch("saltmdb.domain.services.librarian_service.trigger_librarian"):
                result = store_memory(
                    "content positional content body with enough detail to satisfy the quality gate and preserve the original memory semantics for this signature test.",
                    title="Positional title",
                    provenance=Provenance(agent_id="owner"),
                    db_connection=connection,
                )
            self.assertEqual(result["status"], "ok")
            connection.close()

    def test_grouped_forms_preserve_rows_and_envelopes(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(str(Path(directory) / "equivalence.db"))
            with patch("saltmdb.domain.services.librarian_service.trigger_librarian"):
                plain = cast(
                    dict[str, Any],
                    store_memory(
                        "plain grouped content body with enough detail to satisfy the quality gate and preserve the original memory semantics for this signature test.",
                        title="Plain Memory",
                        provenance=Provenance(agent_id="owner"),
                        db_connection=connection,
                        runtime=StoreRuntime(probe_sink=[]),
                    ),
                )
                detail = cast(
                    dict[str, Any],
                    store_memory(
                        "detail grouped content body about database backup rotation, retention windows, and disaster recovery drills with enough detail for this test.",
                        title="Detail Memory",
                        provenance=Provenance(agent_id="owner"),
                        db_connection=connection,
                        runtime=StoreRuntime(probe_sink=[]),
                    ),
                )
                detail_id = detail["data"]["id"]
                core = cast(
                    dict[str, Any],
                    store_memory(
                        f"core grouped content body references Detail Memory ({detail_id}) and has enough detail to satisfy the quality gate.",
                        title="Core Memory",
                        provenance=Provenance(agent_id="owner"),
                        core=CoreDeclaration(
                            is_core=True,
                            core_reason="A sufficiently detailed core reason for equivalence.",
                            core_exit_condition="A sufficiently detailed core exit condition.",
                            detail_memory_ids=[detail_id],
                        ),
                        db_connection=connection,
                        runtime=StoreRuntime(probe_sink=[]),
                    ),
                )
                annotated = cast(
                    dict[str, Any],
                    store_memory(
                        "annotated grouped content body about geospatial indexing, coordinate transforms, and tile-cache invalidation with enough detail for this test.",
                        title="Annotated Memory",
                        provenance=Provenance(
                            agent_id="owner", context_id="ctx", agent_session_id="session"
                        ),
                        annotations=EntityAnnotations(
                            metadata={"source": "test"}, memory_type="decision"
                        ),
                        scores=StoreScores(relevance=2, impact=3, novelty=3, actionability=3),
                        db_connection=connection,
                        runtime=StoreRuntime(probe_sink=[]),
                    ),
                )
            for result, context, memory_type, session in (
                (plain, None, "fact", None),
                (core, None, "fact", None),
                (annotated, "ctx", "decision", "session"),
            ):
                self.assertEqual(
                    result,
                    {
                        "status": "ok",
                        "data": {
                            "id": result["data"]["id"],
                            "message": f"Knowledge stored successfully with ID: {result['data']['id']}",
                            "submitted_tags": [],
                            "effective_tags": result["data"]["effective_tags"],
                            "duplicate_candidates": [],
                        },
                        "warnings": [],
                        "errors": [],
                        "effective": {
                            "agent_id": "owner",
                            "context_id": context,
                            "scope": "shared",
                            "memory_type": memory_type,
                            "agent_session_id": session,
                        },
                    },
                )
            rows = {}
            for result in (plain, detail, core, annotated):
                rows[result["data"]["id"]] = connection.execute(
                    "SELECT is_core, memory_type, metadata, context_id, agent_session_id, quality_score, core_detail_memory_ids "
                    "FROM entities WHERE id = ?",
                    (result["data"]["id"],),
                ).fetchone()
            self.assertEqual(rows[plain["data"]["id"]][:2], (0, "fact"))
            self.assertEqual(rows[detail["data"]["id"]][:2], (0, "fact"))
            self.assertEqual(rows[core["data"]["id"]][:2], (1, "fact"))
            self.assertEqual(json.loads(rows[core["data"]["id"]][6]), [detail_id])
            self.assertEqual(
                rows[annotated["data"]["id"]],
                (0, "decision", '{"source": "test"}', "ctx", "session", 0.5, None),
            )
            connection.close()

    @patch("saltmdb.daemon.dispatch.memory_service.store_memory", return_value="stored")
    def test_dispatch_builds_all_group_values(self, service):
        coordinator = object()
        sink = []
        _dispatch_store_memory(
            content="content",
            title="title",
            agent_id="owner",
            context_id="context",
            agent_session_id="session",
            is_core=True,
            core_reason="core reason",
            core_exit_condition="core exit condition",
            metadata={"key": "value"},
            memory_type="decision",
            weight=4,
            relevance=1,
            impact=2,
            novelty=3,
            actionability=4,
            coordinator=coordinator,
            probe_sink=sink,
        )
        call = service.call_args.kwargs
        self.assertEqual(
            call["provenance"],
            Provenance(agent_id="owner", context_id="context", agent_session_id="session"),
        )
        self.assertEqual(
            call["core"],
            CoreDeclaration(
                is_core=True, core_reason="core reason", core_exit_condition="core exit condition"
            ),
        )
        self.assertEqual(
            call["annotations"],
            EntityAnnotations(metadata={"key": "value"}, memory_type="decision"),
        )
        self.assertEqual(
            call["scores"], StoreScores(weight=4, relevance=1, impact=2, novelty=3, actionability=4)
        )
        self.assertEqual(call["runtime"], StoreRuntime(coordinator=coordinator, probe_sink=sink))

    def test_runtime_coordinator_reaches_librarian(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = init_db(str(Path(directory) / "signature.db"))
            coordinator = object()
            with patch("saltmdb.domain.services.librarian_service.trigger_librarian") as trigger:
                result = store_memory(
                    "coordinator propagation content body with enough detail to satisfy the quality gate and preserve the original memory semantics for this signature test.",
                    title="Coordinator propagation",
                    provenance=Provenance(agent_id="owner"),
                    runtime=StoreRuntime(coordinator=coordinator),
                    db_connection=connection,
                )
            self.assertEqual(result["status"], "ok")
            trigger.assert_called_once()
            self.assertIs(trigger.call_args.kwargs["coordinator"], coordinator)
            connection.close()


if __name__ == "__main__":
    unittest.main()
