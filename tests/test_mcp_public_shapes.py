from copy import deepcopy

from saltmdb.mcp.public_shapes import (
    context_data,
    lineage_data,
    lineage_node,
    lineage_view,
    memory_record,
    related_data,
)


def test_lineage_node_renames_only_id_and_does_not_mutate():
    source = {"id": "mem-1", "title": "Title", "depth": 2, "extra": {"value": 7}}
    original = deepcopy(source)
    assert lineage_node(source) == {
        "memory_id": "mem-1",
        "title": "Title",
        "depth": 2,
        "extra": {"value": 7},
    }
    assert source == original


def test_lineage_node_tolerates_missing_id():
    assert lineage_node({"title": "Title"}) == {"title": "Title"}


def test_lineage_view_maps_both_directions():
    view = {"ancestors": [{"id": "a", "depth": 1}], "descendants": [{"id": "d"}], "other": 3}
    assert lineage_view(view) == {
        "ancestors": [{"memory_id": "a", "depth": 1}],
        "descendants": [{"memory_id": "d"}],
        "other": 3,
    }


def test_memory_record_renames_nested_record_fields():
    record = {
        "id": "mem-1",
        "parent_ids": ["parent-1"],
        "title": "Title",
        "lineage": {"ancestors": [{"id": "parent-1"}], "descendants": []},
        "content": "body",
    }
    assert memory_record(record) == {
        "memory_id": "mem-1",
        "parent_memory_ids": ["parent-1"],
        "title": "Title",
        "lineage": {"ancestors": [{"memory_id": "parent-1"}], "descendants": []},
        "content": "body",
    }
    assert record["id"] == "mem-1"


def test_lineage_data_renames_data_and_nodes():
    data = {
        "entity_id": "mem-1",
        "direction": "ancestors",
        "root": {"id": "mem-1", "title": "Root"},
        "nodes": [{"id": "mem-1", "title": "Root"}],
        "edges": [{"source_id": "mem-1", "target_id": "old", "predicate": "revises"}],
        "total": 1,
        "total_nodes": 1,
    }
    assert lineage_data(data) == {
        "memory_id": "mem-1",
        "direction": "ancestors",
        "root": {"memory_id": "mem-1", "title": "Root"},
        "nodes": [{"memory_id": "mem-1", "title": "Root"}],
        "edges": [{"source_id": "mem-1", "target_id": "old", "predicate": "revises"}],
        "total_edges": 1,
        "total_nodes": 1,
    }


def test_related_data_uses_one_edge_count_and_records():
    data = {
        "entity_id": "root",
        "root": {"id": "root", "title": "Root", "depth": 0},
        "related_memories": [
            {"id": "root", "title": "Root", "depth": 0},
            {"id": "child", "parent_ids": ["root"], "snippet": "summary"},
        ],
        "total_related_found": 1,
        "edges": [{"source_id": "root", "target_id": "child"}],
    }
    expected = {
        "memory_id": "root",
        "root": {"memory_id": "root", "title": "Root", "depth": 0},
        "related_memories": [
            {"memory_id": "root", "title": "Root", "depth": 0},
            {"memory_id": "child", "parent_memory_ids": ["root"], "snippet": "summary"},
        ],
        "total_edges": 1,
        "edges": [{"source_id": "root", "target_id": "child"}],
    }
    assert related_data(data) == expected


def test_context_data_renames_only_memory_identity_fields():
    data = {
        "entity_ids": ["head"],
        "memories": [
            {
                "entity_id": "head",
                "retrieval_provenance": [
                    {"origin_hit_id": "head"},
                    {"orphan_entity_id": "orphan"},
                ],
            }
        ],
        "lineage": {
            "head": {
                "current": {"id": "head"},
                "historical": [{"id": "old", "contradicted_with": ["peer"]}],
            }
        },
        "conflict_sets": [
            {
                "id": "cs-1",
                "members": [{"id": "peer", "inclusion": "conflict_only"}],
                "edges": [{"source_id": "head", "target_id": "peer"}],
            }
        ],
        "edges": [
            {
                "relation_id": "rel-1",
                "source_id": "head",
                "target_id": "peer",
                "predicate": "depends_on",
            }
        ],
        "metadata": {"strategy": "local"},
    }
    assert context_data(data) == {
        "memory_ids": ["head"],
        "memories": [
            {
                "memory_id": "head",
                "retrieval_provenance": [
                    {"origin_memory_id": "head"},
                    {"orphan_memory_id": "orphan"},
                ],
            }
        ],
        "lineage": {
            "head": {
                "current": {"memory_id": "head"},
                "historical": [{"memory_id": "old", "contradicted_with": ["peer"]}],
            }
        },
        "conflict_sets": [
            {
                "id": "cs-1",
                "members": [{"memory_id": "peer", "inclusion": "conflict_only"}],
                "edges": [{"source_id": "head", "target_id": "peer"}],
            }
        ],
        "edges": [
            {
                "relation_id": "rel-1",
                "source_id": "head",
                "target_id": "peer",
                "predicate": "depends_on",
            }
        ],
        "metadata": {"strategy": "local"},
    }


def test_all_public_shape_helpers_do_not_mutate_nested_inputs():
    fixtures = [
        (lineage_view, {"ancestors": [{"id": "a"}], "descendants": [{"id": "d"}]}),
        (
            lineage_data,
            {
                "entity_id": "root",
                "root": {"id": "root"},
                "nodes": [{"id": "root"}],
                "edges": [{"source_id": "root", "target_id": "old"}],
                "total": 1,
            },
        ),
        (
            related_data,
            {
                "entity_id": "root",
                "root": {"id": "root"},
                "related_memories": [{"id": "root"}],
                "total_related_found": 0,
                "edges": [],
            },
        ),
        (
            context_data,
            {
                "entity_ids": ["root"],
                "memories": [{"entity_id": "root"}],
                "lineage": {"root": {"current": {"id": "root"}}},
                "conflict_sets": [{"id": "cs-1", "members": []}],
            },
        ),
    ]
    for helper, source in fixtures:
        original = deepcopy(source)
        _ = helper(source)
        assert source == original
