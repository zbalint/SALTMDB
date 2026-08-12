import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from freeze_live_corpus import CorpusFreezeError, derive  # noqa: E402


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def snapshot():
    return {
        "entities": [
            {"id": "a", "title": " A  title ", "body": "", "source_hash": _hash("a")},
            {"id": "b", "title": "B", "body": "word  " * 401, "source_hash": _hash("b")},
        ],
        "entity_count": 2,
        "has_more": False,
        "next_cursor": None,
        "snapshot_hash": _hash("snapshot"),
        "supersedes_edges": [],
    }


def test_derive_normalizes_chunks_signs_and_projects_all_declared_models():
    export, manifest, projection = derive(snapshot())
    assert export["entities"][0]["title"] == "A title"
    assert export["entities"][0]["chunks"] == ["A title"]
    assert len(export["entities"][1]["chunks"]) == 3
    assert manifest["artifact_fingerprint"]
    assert projection["entity_count"] == 2
    assert projection["chunk_count"] == 4
    assert len(projection["dense_indexes"]) == 8
    assert len(projection["late_interaction_indexes"]) == 1


def test_derive_rejects_incomplete_or_drifting_snapshot():
    value = snapshot()
    value["has_more"] = True
    with pytest.raises(CorpusFreezeError, match="complete final page"):
        derive(value)
    value = snapshot()
    value["entity_count"] = 3
    with pytest.raises(CorpusFreezeError, match="entity_count"):
        derive(value)
