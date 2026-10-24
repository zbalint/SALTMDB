from saltmdb.domain.services.memory_service import Provenance
from typing import cast
from unittest.mock import patch

import pytest

from saltmdb.db.schema import init_db
from saltmdb.domain.services import relation_service
from saltmdb.domain.services.memory_service import store_memory


def _parent(conn, title):
    result = cast(
        dict[str, object],
        store_memory(
            title=title,
            content=f"Durable bulk source content for {title} with enough detail.",
            db_connection=conn,
            provenance=Provenance(agent_id="bulk-owner"),
        ),
    )
    assert result["status"] == "ok", result
    data = cast(dict[str, str], result["data"])
    return data["id"]


def _cohesion(ids, conn):
    rows = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            f"SELECT id, content_hash, status FROM entities WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
    }
    return {entity_id: [1.0, 0.0] for entity_id in ids}, {}, rows


def _items(groups, suffix="result"):
    return [
        {
            "parent_ids": group,
            "title": f"{suffix}-{index}",
            "content": f"Durable bulk result {index} with enough details.",
        }
        for index, group in enumerate(groups)
    ]


def test_third_item_rejection_reports_index_and_rolls_back_prior_items(tmp_path):
    conn = init_db(str(tmp_path / "third-rejection.db"))
    groups = [[_parent(conn, f"third-{index}-{side}") for side in ("a", "b")] for index in range(3)]
    items = _items(groups)
    items[2]["title"] = ""
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = relation_service.apply_consolidations(items, db_connection=conn)
    assert result["status"] == "rejected", result
    assert result["errors"][0]["code"] == "MISSING_REQUIRED_FIELDS"
    assert result["errors"][0]["field"] == "items[2]"
    placeholders = ",".join("?" for _ in groups[0] + groups[1])
    assert (
        conn.execute(
            f"SELECT status FROM entities WHERE id IN ({placeholders}) ORDER BY id",
            groups[0] + groups[1],
        ).fetchall()
        == [("raw",)] * 4
    )
    conn.close()


def test_first_item_rejection_reports_zero_index(tmp_path):
    conn = init_db(str(tmp_path / "first-rejection.db"))
    groups = [[_parent(conn, f"first-{index}-{side}") for side in ("a", "b")] for index in range(3)]
    items = _items(groups)
    items[0]["title"] = ""
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = relation_service.apply_consolidations(items, db_connection=conn)
    assert result["status"] == "rejected", result
    assert result["errors"][0]["field"] == "items[0]"
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'consolidated'").fetchone()[0]
        == 0
    )
    conn.close()


def test_bulk_items_share_batch_agent_session_id(tmp_path):
    conn = init_db(str(tmp_path / "session-id.db"))
    groups = [
        [_parent(conn, f"session-{index}-{side}") for side in ("a", "b")] for index in range(2)
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = relation_service.apply_consolidations(
            _items(groups),
            agent_id="bulk-owner",
            agent_session_id="batch-session",
            db_connection=conn,
        )
    assert result["status"] == "ok", result
    ids = [item["memory_id"] for item in result["data"]["items"]]
    assert conn.execute(
        f"SELECT DISTINCT agent_session_id FROM entities WHERE id IN ({','.join('?' for _ in ids)})",
        ids,
    ).fetchall() == [("batch-session",)]
    conn.close()


def test_bulk_context_for_parents_preserves_order_and_rejects_unpopulated_maps():
    bulk = relation_service._BulkContext(
        {"parent-b": [2.0], "parent-a": [1.0]},
        {"parent-a": "missing"},
        {"parent-b": ("hash-b", "raw")},
    )
    sliced = bulk.for_parents(["parent-b", "absent", "parent-a"])
    assert sliced.precomputed_centroids is not None
    assert list(sliced.precomputed_centroids) == ["parent-b", "parent-a"]
    assert sliced.precomputed_unresolved == {"parent-a": "missing"}
    assert sliced.precomputed_observed_state == {"parent-b": ("hash-b", "raw")}

    with pytest.raises(ValueError, match="maps must be populated"):
        relation_service._BulkContext(None, {}, {}).for_parents(["parent-a"])


@pytest.mark.parametrize(
    "missing_field",
    ["precomputed_centroids", "precomputed_unresolved", "precomputed_observed_state"],
)
def test_bulk_context_rejects_each_unpopulated_map(missing_field):
    centroids = None if missing_field == "precomputed_centroids" else {"parent-a": [1.0]}
    unresolved = None if missing_field == "precomputed_unresolved" else {"parent-a": "missing"}
    observed = (
        None if missing_field == "precomputed_observed_state" else {"parent-a": ("hash-a", "raw")}
    )
    bulk = relation_service._BulkContext(centroids, unresolved, observed)
    with pytest.raises(ValueError, match="maps must be populated"):
        bulk.for_parents(["parent-a"])


def test_bulk_commit_passes_each_item_parent_slice_to_consolidate(tmp_path):
    conn = init_db(str(tmp_path / "slice.db"))
    groups = [[_parent(conn, f"slice-{index}-{side}") for side in ("a", "b")] for index in range(2)]
    items = _items(groups, suffix="slice")

    union_ids = [parent_id for group in groups for parent_id in group]
    union_centroids, union_unresolved, union_observed = _cohesion(union_ids, conn)
    union_centroids["unrelated"] = [3.0, 0.0]
    for parent_id in union_ids:
        union_unresolved[parent_id] = "parent-missing"
    union_unresolved["unrelated"] = "unrelated-missing"
    union_observed["unrelated"] = ("unrelated-hash", "raw")

    with (
        patch.object(
            relation_service,
            "get_fresh_entity_centroids",
            return_value=(union_centroids, union_unresolved, union_observed),
        ),
        patch.object(
            relation_service,
            "consolidate_memories",
            side_effect=[
                {"status": "ok", "data": {"entity_id": "bulk-id-0"}},
                {"status": "ok", "data": {"entity_id": "bulk-id-1"}},
            ],
        ) as consolidate,
    ):
        result = relation_service.bulk_commit_consolidation(
            items,
            agent_id="bulk-owner",
            db_connection=conn,
        )

    assert [item["entity_id"] for item in result] == ["bulk-id-0", "bulk-id-1"]
    assert len(consolidate.call_args_list) == 2
    for call, group in zip(consolidate.call_args_list, groups):
        sliced = call.kwargs["_bulk"]
        assert sliced.precomputed_centroids is not None
        assert sliced.precomputed_observed_state is not None
        assert sliced.precomputed_unresolved is not None
        assert set(sliced.precomputed_centroids) == set(group)
        assert set(sliced.precomputed_observed_state) == set(group)
        assert set(sliced.precomputed_unresolved) == set(group)
    conn.close()
