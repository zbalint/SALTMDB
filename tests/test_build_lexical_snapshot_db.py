import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from build_query_slots import GateBSlotError  # noqa: E402
from freeze_live_corpus import derive  # noqa: E402
from build_lexical_snapshot_db import (  # noqa: E402
    LexicalSnapshotDbError,
    build_snapshot_db,
)
from saltmdb.db.connection import close_connection, get_connection  # noqa: E402
from lexical_adapter import bm25_search  # noqa: E402


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def snapshot():
    return {
        "entities": [
            {
                "id": "e1",
                "title": "Alpha Widget Manual",
                "body": "This is the alpha widget manual body text describing installation steps.",
                "source_hash": _hash("e1"),
            },
            {
                "id": "e2",
                "title": "Beta Widget Manual",
                "body": "This is the beta widget manual body text describing installation steps too.",
                "source_hash": _hash("e2"),
            },
        ],
        "entity_count": 2,
        "has_more": False,
        "next_cursor": None,
        "snapshot_hash": _hash("snapshot"),
        "supersedes_edges": [
            {
                "id": "rel-1",
                "source_id": "e1",
                "target_id": "e2",
                "predicate": "supersedes",
                "created_at": "2026-08-01T00:00:00+00:00",
                "valid_from": "2026-08-01T00:00:00+00:00",
                "valid_to": None,
                "valid_at": "2026-08-01T00:00:00+00:00",
                "invalid_at": None,
            }
        ],
        "provenance": {"owner_id": "test-owner"},
    }


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    export, manifest, _projection = derive(snapshot())
    export["snapshot_provenance"] = {"owner_id": "test-owner"}
    export_path = tmp_path / "corpus_export.json"
    manifest_path = tmp_path / "corpus_representation_manifest.json"
    export_path.write_text(json.dumps(export))
    manifest_path.write_text(json.dumps(manifest))
    return export_path, manifest_path


def test_build_snapshot_db_inserts_expected_entity_and_relation_counts(tmp_path):
    export_path, manifest_path = _write_fixture(tmp_path)
    db_path = tmp_path / "lexical.db"
    receipt = build_snapshot_db(export_path, manifest_path, db_path)

    assert receipt["entity_count"] == 2
    assert receipt["relation_count"] == 1
    assert receipt["kind"] == "LexicalSnapshotReceipt"
    assert receipt["owner_id"] == "test-owner"
    manifest = json.loads(manifest_path.read_text())
    assert receipt["corpus_root_hash"] == manifest["corpus_root_hash"]
    assert db_path.exists()

    conn = get_connection(str(db_path))
    try:
        rows = conn.execute("SELECT id, title FROM entities ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["e1", "e2"]
        rel = conn.execute("SELECT source_id, target_id, predicate FROM relations").fetchone()
        assert rel == ("e1", "e2", "supersedes")
        fts_count = conn.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
        assert fts_count == 2
    finally:
        close_connection(conn)


def test_build_snapshot_db_propagates_hash_tampering_rejection(tmp_path):
    export_path, manifest_path = _write_fixture(tmp_path)
    export = json.loads(export_path.read_text())
    export["entities"][0]["title"] = "Tampered Title"
    export_path.write_text(json.dumps(export))

    db_path = tmp_path / "lexical.db"
    with pytest.raises(GateBSlotError, match="does not match the signed manifest hash"):
        build_snapshot_db(export_path, manifest_path, db_path)
    assert not db_path.exists()


def test_build_snapshot_db_refuses_to_overwrite_existing_db_path(tmp_path):
    export_path, manifest_path = _write_fixture(tmp_path)
    db_path = tmp_path / "lexical.db"
    build_snapshot_db(export_path, manifest_path, db_path)

    with pytest.raises(LexicalSnapshotDbError, match="already exists"):
        build_snapshot_db(export_path, manifest_path, db_path)


def test_build_snapshot_db_bm25_search_returns_expected_entity(tmp_path):
    export_path, manifest_path = _write_fixture(tmp_path)
    db_path = tmp_path / "lexical.db"
    build_snapshot_db(export_path, manifest_path, db_path)

    conn = get_connection(str(db_path))
    try:
        hits = bm25_search(conn, "Alpha Widget Manual", limit=20)
        assert hits
        assert hits[0].entity_id == "e1"
    finally:
        close_connection(conn)
