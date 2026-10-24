"""S3a batch consolidation service contract tests."""

from __future__ import annotations
from saltmdb.domain.services.memory_service import Provenance

import sqlite3
from unittest.mock import patch

import pytest

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.db.connection import open_read_connection
from saltmdb.db.vector_schema import try_load_vector_extension
from saltmdb.db.schema import init_db
from saltmdb.domain.services import relation_service
from saltmdb.domain.services.memory_service import store_memory


@pytest.fixture(params=["direct", "coordinator"])
def harness(tmp_path, request):
    db_path = str(tmp_path / "batch.db")
    conn = init_db(db_path)
    coordinator = None
    if request.param == "coordinator":
        coordinator = DbWriteCoordinator(db_path)
        coordinator.start()
    yield db_path, conn, coordinator
    if coordinator is not None:
        coordinator.shutdown(timeout=5)
    conn.close()


def _parent(conn, title: str) -> str:
    result = store_memory(
        title=title,
        content=f"Durable source content for {title} with enough detail.",
        db_connection=conn,
        provenance=Provenance(agent_id="batch-owner"),
    )
    assert result["status"] == "ok", result
    return result["data"]["id"]


def _cohesion(ids, conn):
    rows = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            f"SELECT id, content_hash, status FROM entities WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
    }
    return {entity_id: [1.0, 0.0] for entity_id in ids}, {}, rows


def _apply(harness, items):
    _db_path, conn, coordinator = harness
    if coordinator is None:
        return relation_service.apply_consolidations(
            items, agent_id="batch-owner", context_id="batch-context", db_connection=conn
        )
    return dispatch_tool(
        "consolidate_memories",
        {"items": items, "context_id": "batch-context", "agent_id": "batch-owner"},
        coordinator,
    )


def test_two_items_return_public_shape_in_request_order(harness):
    _db_path, conn, _coordinator = harness
    groups = [[_parent(conn, f"parent-{i}-a"), _parent(conn, f"parent-{i}-b")] for i in range(2)]
    items = [
        {
            "parent_ids": group,
            "title": f"result-{i}",
            "content": f"Canonical result {i} with durable details.",
        }
        for i, group in enumerate(groups)
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(harness, items)
    assert result["status"] == "ok", result
    assert result["data"]["consolidated_count"] == 2
    assert [item["title"] for item in result["data"]["items"]] == ["result-0", "result-1"]
    assert all(
        set(item) == {"memory_id", "title", "parent_memory_ids", "orphaned_relations"}
        for item in result["data"]["items"]
    )


def test_parent_error_is_indexed_and_preserves_code(harness):
    _db_path, conn, _coordinator = harness
    valid = [_parent(conn, "valid-a"), _parent(conn, "valid-b")]
    before = _snapshot(_db_path)
    items = [
        {
            "parent_ids": valid,
            "title": "first",
            "content": "First canonical result with durable details.",
        },
        {
            "parent_ids": ["missing-parent"],
            "title": "second",
            "content": "Second canonical result with durable details.",
        },
    ]
    result = _apply(harness, items)
    assert result["status"] == "rejected"
    assert result["errors"] == [
        {
            "code": "UNKNOWN_PARENT_ID",
            "message": "could not resolve parent_id 'missing-parent'.",
            "field": "items[1].parent_memory_ids",
        }
    ]
    assert _snapshot(_db_path) == before


def test_cross_item_shared_parent_rejects_later_index(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, f"shared-{i}") for i in range(3)]
    before = _snapshot(_db_path)
    items = [
        {
            "parent_ids": parents[:2],
            "title": "first",
            "content": "First canonical result with durable details.",
        },
        {
            "parent_ids": [parents[1], parents[2]],
            "title": "second",
            "content": "Second canonical result with durable details.",
        },
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(harness, items)
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "DUPLICATE_PARENT_ID"
    assert result["errors"][0]["field"] == "items[1].parent_memory_ids"
    assert _snapshot(_db_path) == before


def test_omitted_core_requires_explicit_decision(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "core-parent"), _parent(conn, "plain-parent")]
    conn.execute(
        "UPDATE entities SET is_core = 1, core_reason = ?, core_exit_condition = ? WHERE id = ?",
        (
            "Keep this active core because it is operationally required.",
            "Remove after replacement.",
            parents[0],
        ),
    )
    conn.commit()
    before = _snapshot(_db_path)
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": parents,
                    "title": "needs-decision",
                    "content": "A durable result requiring a core decision.",
                }
            ],
        )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CORE_DECISION_REQUIRED"
    assert result["errors"][0]["field"] == "items[0].parent_memory_ids"
    assert _snapshot(_db_path) == before


def test_item_agent_id_is_ignored_and_context_is_preserved(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "identity-a"), _parent(conn, "identity-b")]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = relation_service.apply_consolidations(
            [
                {
                    "parent_ids": parents,
                    "title": "identity-result",
                    "content": "A durable identity result with context.",
                    "agent_id": "attacker",
                    "context_id": "item-context",
                }
            ],
            agent_id="configured-owner",
            context_id="batch-context",
            db_connection=conn,
        )
    assert result["status"] == "ok", result
    row = conn.execute(
        "SELECT agent_id, context_id FROM entities WHERE id = ?",
        (result["data"]["items"][0]["memory_id"],),
    ).fetchone()
    assert row == ("configured-owner", "item-context")


def test_none_and_missing_item_context_inherit_batch_context(harness):
    _db_path, conn, coordinator = harness
    if coordinator is not None:
        pytest.skip("direct service context passthrough check")
    groups = [
        [_parent(conn, f"context-{index}-a"), _parent(conn, f"context-{index}-b")]
        for index in range(2)
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = relation_service.apply_consolidations(
            [
                {
                    "parent_ids": groups[0],
                    "title": "none-context",
                    "content": "Durable context result with explicit none.",
                    "context_id": None,
                },
                {
                    "parent_ids": groups[1],
                    "title": "missing-context",
                    "content": "Durable context result with missing context.",
                },
            ],
            agent_id="configured-owner",
            context_id="batch-context",
            db_connection=conn,
        )
    assert result["status"] == "ok", result
    rows = conn.execute(
        "SELECT context_id FROM entities WHERE id IN (?, ?) ORDER BY title",
        (result["data"]["items"][0]["memory_id"], result["data"]["items"][1]["memory_id"]),
    ).fetchall()
    assert rows == [("batch-context",), ("batch-context",)]


def test_coordinator_item_context_wins_and_missing_inherits(harness):
    _db_path, conn, coordinator = harness
    if coordinator is None:
        pytest.skip("coordinator dispatch context check")
    groups = [
        [_parent(conn, f"dispatch-context-{index}-a"), _parent(conn, f"dispatch-context-{index}-b")]
        for index in range(2)
    ]
    items = [
        {
            "parent_ids": groups[0],
            "title": "dispatch-batch-context",
            "content": "Durable dispatch context result.",
        },
        {
            "parent_ids": groups[1],
            "title": "dispatch-item-context",
            "content": "Durable dispatch item context result.",
            "context_id": "item-context",
        },
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(harness, items)
    assert result["status"] == "ok", result
    rows = conn.execute(
        "SELECT context_id FROM entities WHERE title IN (?, ?) ORDER BY title",
        ("dispatch-batch-context", "dispatch-item-context"),
    ).fetchall()
    assert rows == [("batch-context",), ("item-context",)]


def test_unexpected_write_fault_is_re_raised_by_coordinator(harness, monkeypatch):
    _db_path, conn, coordinator = harness
    if coordinator is None:
        pytest.skip("coordinator-only propagation check")
    parents = [_parent(conn, "fault-a"), _parent(conn, "fault-b")]
    before = _snapshot(_db_path)
    monkeypatch.setattr(
        relation_service,
        "_write_bulk_consolidations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected batch fault")),
    )
    with pytest.raises(RuntimeError, match="unexpected batch fault"):
        _apply(
            harness,
            [{"parent_ids": parents, "title": "fault-result", "content": "A durable result."}],
        )
    assert _snapshot(_db_path) == before


def test_non_mapping_item_is_rejected_without_writes(harness):
    _db_path, conn, _coordinator = harness
    before = _snapshot(_db_path)
    result = relation_service.apply_consolidations([None], db_connection=conn)
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "items[0]"
    assert _snapshot(_db_path) == before


def test_late_item_rejection_is_atomic(harness):
    db_path, conn, _coordinator = harness
    groups = [[_parent(conn, f"late-{i}-a"), _parent(conn, f"late-{i}-b")] for i in range(3)]
    before = _snapshot(db_path)
    items = [
        {
            "parent_ids": groups[0],
            "title": "first",
            "content": "First canonical result with durable details.",
        },
        {"parent_ids": groups[1], "title": "second", "content": "test " * 40},
        {
            "parent_ids": groups[2],
            "title": "third",
            "content": "Third canonical result that must not run.",
        },
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(harness, items)
    assert result["status"] == "rejected"
    assert result["errors"]
    assert result["errors"][0]["field"] == "items[1]"
    assert _snapshot(db_path) == before


def test_failure_after_two_successes_reports_third_item_and_stops(harness):
    db_path, conn, _coordinator = harness
    groups = [[_parent(conn, f"third-{i}-a"), _parent(conn, f"third-{i}-b")] for i in range(3)]
    before = _snapshot(db_path)
    items = [
        {
            "parent_ids": groups[0],
            "title": "first-third",
            "content": "First canonical result with durable details.",
        },
        {
            "parent_ids": groups[1],
            "title": "second-third",
            "content": "Second canonical result with durable details.",
        },
        {"parent_ids": groups[2], "title": "third-failing", "content": "test " * 40},
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(harness, items)
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "items[2]"
    assert _snapshot(db_path) == before


def _snapshot(db_path: str):
    reader = open_read_connection(db_path)
    try:
        tables = (
            "entities",
            "relations",
            "events",
            "entity_tags",
            "embedding_jobs",
            "retrieval_embedding_jobs",
        )
        snapshot = {
            table: reader.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        if try_load_vector_extension(reader):
            vector_tables = reader.execute(
                "SELECT name FROM sqlite_master WHERE sql LIKE '%USING vec0%' ORDER BY name"
            ).fetchall()
            for (table_name,) in vector_tables:
                quoted = '"' + table_name.replace('"', '""') + '"'
                snapshot[f"vector:{table_name}"] = reader.execute(
                    f"SELECT * FROM {quoted} ORDER BY 1"
                ).fetchall()
        snapshot["fts_count"] = reader.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
        return snapshot
    finally:
        reader.close()


def _assert_rejected_unchanged(harness, items):
    db_path, _conn, _coordinator = harness
    before = _snapshot(db_path)
    result = _apply(harness, items)
    assert result["status"] == "rejected"
    assert _snapshot(db_path) == before
    return result


def test_empty_items_rejects_without_changes(harness):
    result = _assert_rejected_unchanged(harness, [])
    assert result["errors"][0]["code"] == "VALIDATION_ERROR"
    assert result["errors"][0]["field"] == "items"


def test_first_failing_item_only_is_reported(harness):
    result = _assert_rejected_unchanged(
        harness,
        [
            {
                "parent_ids": ["missing-first"],
                "title": "first",
                "content": "Durable batch content for first.",
            },
            {
                "parent_ids": ["missing-second"],
                "title": "second",
                "content": "Durable batch content for second.",
            },
        ],
    )
    assert result["errors"] == [
        {
            "code": "UNKNOWN_PARENT_ID",
            "message": "could not resolve parent_id 'missing-first'.",
            "field": "items[0].parent_memory_ids",
        }
    ]


@pytest.mark.parametrize(
    "parent_ids", [lambda parents: [parents[0]], lambda parents: [parents[0], parents[0]]]
)
def test_parent_count_rejection_literals(harness, parent_ids):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "count-a"), _parent(conn, "count-b")]
    result = _assert_rejected_unchanged(
        harness,
        [
            {
                "parent_ids": parent_ids(parents),
                "title": "count",
                "content": "Durable batch content for count.",
            }
        ],
    )
    assert result["errors"][0]["code"] == "REJECT_PARENT_COUNT"
    assert result["errors"][0]["message"] == (
        "consolidate_memories requires at least 2 distinct active parents; "
        "use revise_memory for a single parent."
    )


def test_repeated_parent_with_third_is_deduplicated_and_accepted(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, f"dedupe-{index}") for index in range(3)]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": [parents[0], parents[0], parents[1]],
                    "title": "deduped",
                    "content": "A durable deduped result.",
                }
            ],
        )
    assert result["status"] == "ok", result
    assert result["data"]["items"][0]["parent_memory_ids"] == parents[:2]


def test_ambiguous_parent_rejection_literal(harness):
    _db_path, conn, _coordinator = harness
    before = _snapshot(_db_path)
    with patch.object(
        relation_service,
        "resolve_entity_ref",
        return_value=(None, [{"id": "one"}, {"id": "two"}], False),
    ):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": ["ambiguous", "missing"],
                    "title": "ambiguous-result",
                    "content": "Durable batch content for ambiguous.",
                }
            ],
        )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "AMBIGUOUS_PARENT_ID"
    assert _snapshot(_db_path) == before


def test_inactive_parent_rejection_literal(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "inactive-a"), _parent(conn, "inactive-b")]
    conn.execute("UPDATE entities SET status = 'archived' WHERE id = ?", (parents[0],))
    conn.commit()
    result = _assert_rejected_unchanged(
        harness,
        [
            {
                "parent_ids": parents,
                "title": "inactive-result",
                "content": "Durable batch content for inactive.",
            }
        ],
    )
    assert result["errors"][0]["code"] == "INACTIVE_PARENT"


def test_low_cohesion_and_override_escape_hatch(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "cohesion-a"), _parent(conn, "cohesion-b")]

    def low_cohesion(ids, observed_conn):
        centroids, unresolved, observed = _cohesion(ids, observed_conn)
        centroids[ids[1]] = [0.0, 1.0]
        return centroids, unresolved, observed

    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=low_cohesion):
        rejected_result = _assert_rejected_unchanged(
            harness,
            [{"parent_ids": parents, "title": "low", "content": "Durable batch content for low."}],
        )
    assert rejected_result["errors"][0]["code"] == "REJECT_LOW_COHESION"
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=low_cohesion):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": parents,
                    "title": "low-override",
                    "content": "Durable batch content for low override.",
                    "override_justification": "intentional test synthesis",
                }
            ],
        )
    assert result["status"] == "ok", result


def test_clear_vector_fault_maps_direct_or_raises_coordinator(harness):
    _db_path, conn, coordinator = harness
    parents = [_parent(conn, "clear-fault-a"), _parent(conn, "clear-fault-b")]
    before = _snapshot(_db_path)
    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch.object(
            relation_service,
            "_clear_archived_parent_vectors",
            side_effect=sqlite3.OperationalError("clear vector fault"),
        ),
    ):
        if coordinator is not None:
            with pytest.raises(sqlite3.OperationalError, match="clear vector fault"):
                _apply(
                    harness,
                    [
                        {
                            "parent_ids": parents,
                            "title": "clear-fault",
                            "content": "Durable batch content for clear fault.",
                        }
                    ],
                )
        else:
            result = _apply(
                harness,
                [
                    {
                        "parent_ids": parents,
                        "title": "clear-fault",
                        "content": "Durable batch content for clear fault.",
                    }
                ],
            )
            assert result["status"] == "rejected"
            assert result["errors"][0]["code"] == "INTERNAL_ERROR"
    assert _snapshot(_db_path) == before


def test_coordinator_unrelated_event_survives_rejected_batch(harness):
    _db_path, conn, coordinator = harness
    if coordinator is None:
        pytest.skip("coordinator composability is tested on the coordinator harness")
    parents = [_parent(conn, f"composable-{index}") for index in range(4)]

    def job(job_conn):
        job_conn.execute(
            "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
            ("unrelated-batch-event", "owner", "test", "must survive"),
        )
        return relation_service.apply_consolidations(
            [
                {
                    "parent_ids": parents[:2],
                    "title": "composable-first",
                    "content": "Durable batch content for composable first.",
                },
                {
                    "parent_ids": parents[2:],
                    "title": "composable-second",
                    "content": "test " * 40,
                },
            ],
            db_connection=job_conn,
        )

    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = coordinator.submit("composable-batch", job)
    assert result["status"] == "rejected"
    reader = open_read_connection(_db_path)
    try:
        assert reader.execute(
            "SELECT id FROM events WHERE id = ?", ("unrelated-batch-event",)
        ).fetchone()
        assert (
            reader.execute(
                "SELECT id FROM entities WHERE title = ?", ("composable-first",)
            ).fetchone()
            is None
        )
    finally:
        reader.close()


def test_vector_clear_once_on_success_and_never_on_rejection(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "vector-a"), _parent(conn, "vector-b")]
    cleared = []
    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch.object(
            relation_service,
            "_clear_archived_parent_vectors",
            side_effect=lambda _conn, parent_ids: cleared.append(tuple(parent_ids)),
        ),
    ):
        success = _apply(
            harness,
            [
                {
                    "parent_ids": parents,
                    "title": "vector-success",
                    "content": "Durable batch content for vector.",
                }
            ],
        )
        assert success["status"] == "ok"
        assert cleared == [tuple(parents)]
        rejected = _assert_rejected_unchanged(
            harness,
            [
                {
                    "parent_ids": ["missing-vector-a", "missing-vector-b"],
                    "title": "vector-rejected",
                    "content": "Durable batch content for vector rejected.",
                }
            ],
        )
        assert rejected["status"] == "rejected"
        assert cleared == [tuple(parents)]
        fault_parents = [_parent(conn, "vector-fault-a"), _parent(conn, "vector-fault-b")]
        before_fault = _snapshot(_db_path)
        with patch.object(
            relation_service,
            "_consolidation_commit",
            side_effect=sqlite3.OperationalError("vector fault"),
        ):
            rejected = _apply(
                harness,
                [
                    {
                        "parent_ids": fault_parents,
                        "title": "vector-fault",
                        "content": "Durable vector fault result.",
                    }
                ],
            )
        assert rejected["status"] == "rejected"
        assert rejected["errors"][0]["code"] == "CONSOLIDATION_FAILED"
        assert _snapshot(_db_path) == before_fault
        assert cleared == [tuple(parents)]


def _mark_core(conn, entity_id, reason, exit_condition):
    conn.execute(
        "UPDATE entities SET is_core = 1, core_reason = ?, core_exit_condition = ?, core_review_after = ? WHERE id = ?",
        (reason, exit_condition, "2099-01-01T00:00:00+00:00", entity_id),
    )
    conn.commit()


def test_core_corrected_fragments_are_exact_and_follow_parent_order(harness):
    _db_path, conn, _coordinator = harness
    first = _parent(conn, "core-order-first")
    second = _parent(conn, "core-order-second")
    _mark_core(conn, first, "first reason", "first exit")
    _mark_core(conn, second, "second reason", "second exit")
    before = _snapshot(_db_path)
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": [second, first],
                    "title": "core-order",
                    "content": "Durable batch content for core order.",
                }
            ],
        )
    fragments = result["errors"][0]["corrected_calls"]
    assert fragments == [
        {"core": {"reason": "second reason", "exit_condition": "second exit"}},
        {"demote_core": True},
    ]
    assert _snapshot(_db_path) == before


def test_private_core_decision_keep_fragment_forces_shared_scope(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "private-core-a"), _parent(conn, "private-core-b")]
    _mark_core(conn, parents[0], "private reason", "private exit")
    before = _snapshot(_db_path)
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": parents,
                    "title": "private-core",
                    "content": "Durable batch content for private core.",
                    "scope": "private",
                }
            ],
        )
    assert result["errors"][0]["corrected_calls"][0]["scope"] == "shared"
    assert _snapshot(_db_path) == before


def test_explicit_core_keep_and_demote_paths(harness):
    _db_path, conn, _coordinator = harness
    core_parents = [_parent(conn, "keep-a"), _parent(conn, "keep-b")]
    _mark_core(conn, core_parents[0], "keep reason", "keep exit")
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        kept = _apply(
            harness,
            [
                {
                    "parent_ids": core_parents,
                    "title": "kept-core",
                    "content": "Durable batch content for kept core.",
                    "is_core": True,
                    "core_reason": "new core reason with enough detail",
                    "core_exit_condition": "new core exit condition with enough detail",
                }
            ],
        )
    assert kept["status"] == "ok"
    assert (
        conn.execute(
            "SELECT is_core FROM entities WHERE id = ?", (kept["data"]["items"][0]["memory_id"],)
        ).fetchone()[0]
        == 1
    )
    demote_parents = [_parent(conn, "demote-a"), _parent(conn, "demote-b")]
    _mark_core(conn, demote_parents[0], "demote reason", "demote exit")
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        demoted = _apply(
            harness,
            [
                {
                    "parent_ids": demote_parents,
                    "title": "demoted-result",
                    "content": "Durable batch content for demoted result.",
                    "is_core": False,
                }
            ],
        )
    assert demoted["status"] == "ok"
    assert (
        conn.execute(
            "SELECT is_core FROM entities WHERE id = ?", (demoted["data"]["items"][0]["memory_id"],)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entities WHERE id IN (?, ?) AND status = 'archived'",
            demote_parents,
        ).fetchone()[0]
        == 2
    )


def test_demote_non_core_parents_is_accepted(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "ordinary-a"), _parent(conn, "ordinary-b")]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": parents,
                    "title": "ordinary-result",
                    "content": "Durable batch content for ordinary.",
                    "is_core": False,
                }
            ],
        )
    assert result["status"] == "ok"


def test_parent_turning_core_after_preflight_is_caught(harness):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "late-core-a"), _parent(conn, "late-core-b")]
    before = _snapshot(_db_path)
    called = False

    def preflight(ids, observed_conn):
        nonlocal called
        centroids, unresolved, observed = _cohesion(ids, observed_conn)
        if not called:
            called = True
            observed_conn.execute(
                "UPDATE entities SET is_core = 1, core_reason = ?, core_exit_condition = ?, core_review_after = ? WHERE id = ?",
                (
                    "late core reason with detail",
                    "late core exit with detail",
                    "2099-01-01T00:00:00+00:00",
                    parents[0],
                ),
            )

        return centroids, unresolved, observed

    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=preflight):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": parents,
                    "title": "late-core",
                    "content": "Durable batch content for late core.",
                }
            ],
        )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CORE_DECISION_REQUIRED"
    after = _snapshot(_db_path)
    for table in before:
        if table not in {"entities", "fts_count"}:
            assert after[table] == before[table]
    before_entities = {row[0]: row for row in before["entities"]}
    after_entities = {row[0]: row for row in after["entities"]}
    assert set(after_entities) == set(before_entities)
    assert all(
        after_entities[entity_id] == before_entities[entity_id]
        for entity_id in before_entities
        if entity_id != parents[0]
    )
    assert conn.execute(
        "SELECT is_core, core_reason, core_exit_condition FROM entities WHERE id = ?",
        (parents[0],),
    ).fetchone() == (
        1,
        "late core reason with detail",
        "late core exit with detail",
    )


def test_capacity_and_overdue_core_rejections_keep_parents_active(harness):
    _db_path, conn, _coordinator = harness
    capacity_parents = [_parent(conn, "capacity-a"), _parent(conn, "capacity-b")]
    capacity_rejection = {
        "status": "rejected",
        "errors": [
            {
                "code": "CORE_CAPACITY_EXCEEDED",
                "message": "CORE_CAPACITY_EXCEEDED: core capacity exceeded",
                "field": "is_core",
            }
        ],
    }
    before_capacity = _snapshot(_db_path)
    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch.object(
            relation_service.core_governance_service,
            "check_capacity_admission",
            return_value=capacity_rejection,
        ),
    ):
        capacity_result = _apply(
            harness,
            [
                {
                    "parent_ids": capacity_parents,
                    "title": "capacity-result",
                    "content": "Durable capacity result with enough detail.",
                    "is_core": True,
                    "core_reason": "capacity reason with enough detail",
                    "core_exit_condition": "capacity exit with enough detail",
                }
            ],
        )
    assert capacity_result["status"] == "rejected"
    assert capacity_result["errors"][0]["code"] == "CONSOLIDATION_FAILED"
    assert "capacity exceeded" in capacity_result["errors"][0]["message"]
    assert _snapshot(_db_path) == before_capacity

    overdue_parents = [_parent(conn, "overdue-a"), _parent(conn, "overdue-b")]
    _mark_core(
        conn,
        overdue_parents[0],
        "overdue reason with enough detail",
        "overdue exit with enough detail",
    )
    conn.execute(
        "UPDATE entities SET core_review_after = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (overdue_parents[0],),
    )
    conn.commit()
    before_overdue = _snapshot(_db_path)
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        overdue_result = _apply(
            harness,
            [
                {
                    "parent_ids": overdue_parents,
                    "title": "overdue-result",
                    "content": "Durable overdue result with enough detail.",
                    "is_core": True,
                    "core_reason": "overdue result reason with enough detail",
                    "core_exit_condition": "overdue result exit with enough detail",
                }
            ],
        )
    assert overdue_result["status"] == "rejected"
    assert "overdue" in overdue_result["errors"][0]["message"].lower()
    assert _snapshot(_db_path) == before_overdue


def test_inside_item_fault_maps_consolidation_failed_and_rolls_back(harness, monkeypatch):
    _db_path, conn, _coordinator = harness
    parents = [_parent(conn, "inside-fault-a"), _parent(conn, "inside-fault-b")]
    before = _snapshot(_db_path)
    monkeypatch.setattr(
        relation_service,
        "_consolidation_commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("inside item fault")
        ),
    )
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _apply(
            harness,
            [
                {
                    "parent_ids": parents,
                    "title": "inside-fault",
                    "content": "Durable batch content for inside fault.",
                }
            ],
        )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CONSOLIDATION_FAILED"
    assert result["errors"][0]["field"] == "items[0]"
    assert _snapshot(_db_path) == before


def test_item_context_wins_and_missing_context_inherits_batch(harness):
    _db_path, conn, _coordinator = harness
    groups = [
        [_parent(conn, f"context-{index}-{side}") for side in ("a", "b")] for index in range(2)
    ]
    items = [
        {
            "parent_ids": groups[0],
            "title": "context-inherited",
            "content": "Durable batch content for inherited context.",
        },
        {
            "parent_ids": groups[1],
            "title": "context-item",
            "content": "Durable batch content for item context.",
            "context_id": "item-context",
        },
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = relation_service.apply_consolidations(
            items,
            agent_id="owner",
            context_id="batch-context",
            db_connection=conn,
        )
    assert result["status"] == "ok", result
    rows = conn.execute(
        "SELECT context_id FROM entities WHERE id IN (?, ?) ORDER BY title",
        (result["data"]["items"][0]["memory_id"], result["data"]["items"][1]["memory_id"]),
    ).fetchall()
    assert {row[0] for row in rows} == {"batch-context", "item-context"}
