import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from saltmdb.db.schema import init_db  # noqa: E402
from lexical_adapter import bm25_search, include_current_heads, resolve_current_head  # noqa: E402


def connection(tmp_path):
    return init_db(str(tmp_path / "corpus.sqlite"))


def add_entity(conn, entity_id, title, body, status="consolidated"):
    conn.execute(
        "INSERT INTO entities(id, title, full_content, status, weight, is_core, created_at, "
        "updated_at, last_accessed_at, owner_id, scope, metadata, memory_type) "
        "VALUES (?, ?, ?, ?, 1, 0, datetime('now'), datetime('now'), datetime('now'), "
        "'test', 'shared', '{}', 'fact')",
        (entity_id, title, body, status),
    )


def test_bm25_exposes_raw_score_and_fallback(tmp_path):
    conn = connection(tmp_path)
    add_entity(conn, "e1", "Cache protocol", "A deterministic cache protocol")
    add_entity(conn, "e2", "Other", "Cache only")
    conn.commit()
    hits = bm25_search(conn, "cache protocol")
    assert [hit.entity_id for hit in hits] == ["e1"]
    assert hits[0].raw_bm25_score < 0
    assert all(hit.used_or_fallback is False for hit in hits)
    fallback = bm25_search(conn, "cache missingword")
    assert fallback and all(hit.used_or_fallback is True for hit in fallback)


def test_current_head_inclusion_is_multihop_and_stable(tmp_path):
    conn = connection(tmp_path)
    for entity_id in ("old", "middle", "new", "other"):
        add_entity(conn, entity_id, entity_id, entity_id)
    conn.executemany(
        "INSERT INTO relations(id, source_id, target_id, predicate, created_at) "
        "VALUES (?, ?, ?, 'supersedes', datetime('now'))",
        [("r1", "middle", "old"), ("r2", "new", "middle")],
    )
    conn.commit()
    assert resolve_current_head(conn, "old") == "new"
    assert include_current_heads(conn, ["old", "other"]) == ["old", "new", "other"]


def test_current_head_fork_fails_closed(tmp_path):
    conn = connection(tmp_path)
    for entity_id in ("old", "a", "b"):
        add_entity(conn, entity_id, entity_id, entity_id)
    conn.executemany(
        "INSERT INTO relations(id, source_id, target_id, predicate, created_at) "
        "VALUES (?, ?, ?, 'supersedes', datetime('now'))",
        [("r1", "a", "old"), ("r2", "b", "old")],
    )
    conn.commit()
    assert resolve_current_head(conn, "old") is None
