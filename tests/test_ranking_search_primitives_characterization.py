"""Exact retrieval contracts measured against the pre-extraction implementation."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from saltmdb.domain.services import embedding_service
from saltmdb.domain.services.memory_service import ranking, search_primitives
from test_search_accuracy_stage2 import _DbFixture, _axis


class TestRetrievalCharacterization(_DbFixture):
    def test_empty_inputs_and_no_relations(self):
        assert ranking._collapse_supersedes_families([], self.conn) == []
        assert ranking._collapse_supersedes_families(["b", "a", "b"], self.conn) == ["b", "a", "b"]
        assert ranking._build_cross_encoder_candidate_texts("q", [], self.conn, self.db_path) == {}
        assert (
            ranking._build_cross_encoder_candidate_texts("q", ["missing"], self.conn, self.db_path)
            == {}
        )
        assert search_primitives.get_relevance_preview_data("q", [], self.db_path) == {}
        assert search_primitives.chunk_candidate_search("", [], [], 3, 4, self.db_path) == (
            [],
            {
                "requested_chunk_rows": 0,
                "candidate_window": 3,
                "oversampling_multiplier": 4,
                "raw_chunk_rows": 0,
                "unique_fresh_entities": 0,
                "candidate_shortfall": 3,
            },
        )

    def test_family_order_duplicates_multiple_components_and_status(self):
        for eid in ("a-old", "a-head", "b-old", "b-head", "other"):
            self.entity(eid)
        self.relation("a-head", "a-old")
        self.relation("b-head", "b-old")
        self.conn.execute("UPDATE entities SET status='consolidated' WHERE id='b-head'")
        assert ranking._collapse_supersedes_families(
            ["a-old", "other", "b-head", "a-head", "b-old", "other", "a-old"], self.conn
        ) == ["a-head", "other", "b-head", "other"]

    def test_family_archived_missing_fork_and_self_loop(self):
        for eid in ("old", "head", "left", "right", "fork", "loop"):
            self.entity(eid, status="archived" if eid == "head" else "raw")
        self.relation("head", "old")
        self.relation("fork", "left")
        self.relation("fork", "right")
        self.relation("loop", "loop")
        ids = ["old", "head", "right", "fork", "left", "loop", "absent"]
        assert ranking._collapse_supersedes_families(ids, self.conn) == ids
        assert ranking._collapse_supersedes_families(["old"], self.conn) == ["old"]
        # A dangling relation remains ineligible even when both ids are supplied.
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DELETE FROM entities WHERE id='head'")
        self.conn.commit()
        assert ranking._collapse_supersedes_families(["old", "head"], self.conn) == ["old", "head"]

    def test_chunk_exact_order_cap_minimum_and_all_diagnostics(self):
        for eid in ("z", "a", "b", "stale", "archived", "foreign", "shared"):
            self.entity(
                eid,
                owner="bob" if eid == "foreign" else "alice",
                status="archived" if eid == "archived" else "raw",
            )
        self.conn.execute("UPDATE entities SET scope='shared' WHERE id='shared'")
        self.conn.commit()
        for eid in ("z", "a", "b", "archived", "foreign", "shared"):
            self.chunk(eid, 0, _axis(0), content_hash=f"hash-{eid}")
        self.chunk("a", 1, _axis(1), content_hash="hash-a")
        self.chunk("stale", 0, _axis(0), content_hash="obsolete")
        with patch.object(embedding_service, "embed_query_text", return_value=_axis(0)):
            result = search_primitives.chunk_candidate_search(
                "q", ["e.agent_id = ?", "e.scope = ?"], ["alice", "private"], 2, 5, self.db_path
            )
        assert result == (
            [("a", 0.0), ("b", 0.0)],
            {
                "requested_chunk_rows": 10,
                "candidate_window": 2,
                "oversampling_multiplier": 5,
                "raw_chunk_rows": 8,
                "unique_fresh_entities": 3,
                "returned_entities": 2,
                "candidate_shortfall": 0,
            },
        )
        with patch.object(embedding_service, "embed_query_text", return_value=_axis(0)):
            result = search_primitives.chunk_candidate_search("q", [], [], 8, 2, self.db_path)
        assert result == (
            [("a", 0.0), ("b", 0.0), ("foreign", 0.0), ("shared", 0.0), ("z", 0.0)],
            {
                "requested_chunk_rows": 16,
                "candidate_window": 8,
                "oversampling_multiplier": 2,
                "raw_chunk_rows": 8,
                "unique_fresh_entities": 5,
                "returned_entities": 5,
                "candidate_shortfall": 3,
            },
        )

    def test_chunk_no_rows(self):
        with patch.object(embedding_service, "embed_query_text", return_value=_axis(0)):
            assert search_primitives.chunk_candidate_search("q", [], [], 2, 3, self.db_path) == (
                [],
                {
                    "requested_chunk_rows": 6,
                    "candidate_window": 2,
                    "oversampling_multiplier": 3,
                    "raw_chunk_rows": 0,
                    "unique_fresh_entities": 0,
                    "returned_entities": 0,
                    "candidate_shortfall": 2,
                },
            )

    def test_ce_full_text_fallback_hash_status_and_order(self):
        self.entity("chunked", content="abcdef", content_hash="current")
        self.chunk("chunked", 0, _axis(1), content_hash="current", start=0, end=2)
        self.chunk("chunked", 1, _axis(0), content_hash="current", start=2, end=4)
        self.entity("stale", content="old evidence")
        self.chunk("stale", 0, _axis(0), content_hash="obsolete", start=0, end=3)
        self.entity("archived", status="archived")
        self.entity("long", content="x" * 2001)
        with patch.object(embedding_service, "embed_query_text", return_value=_axis(0)):
            result = ranking._build_cross_encoder_candidate_texts(
                "q", ["stale", "missing", "chunked", "archived", "long"], self.conn, self.db_path
            )
        assert list(result) == ["stale", "chunked", "long"]
        assert result == {
            "stale": "stale\n\nold evidence",
            "chunked": "chunked\n\ncd",
            "long": "long\n\n" + "x" * 2000,
        }
        with patch.object(
            embedding_service, "embed_query_text", side_effect=RuntimeError("offline")
        ):
            assert ranking._build_cross_encoder_candidate_texts(
                "q", ["chunked"], self.conn, self.db_path
            ) == {"chunked": "chunked\n\nabcdef"}

    def test_ce_nonfinite_only_rows_fall_back_while_finite_sibling_uses_span(self):
        self.entity("nan-only", content="n" * 2001)
        self.entity("finite-sibling", content="0123456789")
        chunk_conn = MagicMock()
        chunk_conn.execute.return_value.fetchall.return_value = [
            ("nan-only", 0, 0, 4, "bad-span", float("nan")),
            ("nan-only", 1, 4, 8, "bad-span", float("inf")),
            ("finite-sibling", 0, 2, 5, "finite-span", 0.25),
        ]
        with (
            patch.object(ranking, "get_connection", return_value=chunk_conn),
            patch.object(ranking, "close_connection"),
            patch("sqlite_vec.load"),
            patch.object(embedding_service, "embed_query_text", return_value=_axis(0)),
        ):
            result = ranking._build_cross_encoder_candidate_texts(
                "q", ["nan-only", "finite-sibling"], self.conn, "unused"
            )
        assert result == {
            "nan-only": "nan-only\n\n" + "n" * 2000,
            "finite-sibling": "finite-sibling\n\n234",
        }

    def test_ce_empty_chunk_span_uses_exact_2000_char_content_fallback(self):
        self.entity("empty-span", content="x" * 2001)
        chunk_conn = MagicMock()
        chunk_conn.execute.return_value.fetchall.return_value = [
            ("empty-span", 0, 0, 0, "", 0.25),
        ]
        with (
            patch.object(ranking, "get_connection", return_value=chunk_conn),
            patch.object(ranking, "close_connection"),
            patch("sqlite_vec.load"),
            patch.object(embedding_service, "embed_query_text", return_value=_axis(0)),
        ):
            result = ranking._build_cross_encoder_candidate_texts(
                "q", ["empty-span"], self.conn, "unused"
            )
        assert result == {"empty-span": "empty-span\n\n" + "x" * 2000}

    def test_preview_full_results_multiple_query_vectors_and_filters(self):
        for eid in ("fresh", "stale", "archived", "empty", "zero"):
            self.entity(
                eid,
                content="alpha\n\nbeta\n\ngamma",
                status="archived" if eid == "archived" else "raw",
            )
        self.conn.execute("UPDATE entities SET full_content='' WHERE id='empty'")
        self.conn.commit()
        self.chunk("fresh", 0, _axis(0), content_hash="hash-fresh", start=0, end=5)
        self.chunk("fresh", 1, _axis(1), content_hash="hash-fresh", start=7, end=11)
        self.chunk("stale", 0, _axis(0), content_hash="obsolete")
        self.chunk("archived", 0, _axis(0), content_hash="hash-archived")
        self.chunk("empty", 0, _axis(0), content_hash="hash-empty", start=0, end=0)
        with patch.object(
            embedding_service, "embed_query_texts", return_value=[_axis(1), _axis(0)]
        ):
            result = search_primitives.get_relevance_preview_data(
                "q", ["fresh", "stale", "archived", "empty", "zero", "missing"], self.db_path
            )
        assert result == {"fresh": {"text": "alpha"}, "empty": {"text": ""}}
        with patch.object(embedding_service, "embed_query_texts", return_value=[]):
            assert search_primitives.get_relevance_preview_data("", ["fresh"], self.db_path) == {}
        with patch.object(embedding_service, "embed_query_texts", return_value=[_axis(0)]):
            assert search_primitives.get_relevance_preview_data("", ["zero"], self.db_path) == {}

    def test_preview_empty_query_fallback_passes_literal_query_to_embedder(self):
        with (
            patch("saltmdb.utils.chunking.chunk_text", return_value=[]),
            patch.object(embedding_service, "embed_query_texts", return_value=[]) as embed,
        ):
            assert (
                search_primitives.get_relevance_preview_data(
                    "literal-query", ["missing"], self.db_path
                )
                == {}
            )
        embed.assert_called_once_with(["literal-query"])

    def test_preview_duplicate_chunk_rows_keep_minimum_distance_span(self):
        self.entity("duplicate-chunk", content="BEST\n\nWORST", content_hash="duplicate-hash")
        self.chunk(
            "duplicate-chunk",
            0,
            _axis(0),
            content_hash="duplicate-hash",
            start=0,
            end=4,
            chunk_id="best-span",
        )
        self.chunk(
            "duplicate-chunk",
            0,
            _axis(1),
            content_hash="duplicate-hash",
            start=6,
            end=10,
            chunk_id="worst-span",
        )
        with patch.object(embedding_service, "embed_query_texts", return_value=[_axis(0)]):
            result = search_primitives.get_relevance_preview_data(
                "q", ["duplicate-chunk"], self.db_path
            )
        assert result == {"duplicate-chunk": {"text": "BEST"}}

    def test_preview_missing_content_row_uses_empty_authoritative_fallback(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.side_effect = [
            [("id", 0, 0, 1, 0.0)],
            [],
        ]
        with (
            patch.object(search_primitives, "get_connection", return_value=conn),
            patch.object(search_primitives, "close_connection"),
            patch("sqlite_vec.load"),
            patch.object(embedding_service, "embed_query_texts", return_value=[_axis(0)]),
        ):
            assert search_primitives.get_relevance_preview_data("q", ["id"], "unused") == {
                "id": {"text": ""}
            }


@pytest.mark.parametrize(
    "column,offset,eligible",
    [
        ("valid_from", -1, True),
        ("valid_from", 0, True),
        ("valid_from", 1, False),
        ("valid_at", -1, True),
        ("valid_at", 0, True),
        ("valid_at", 1, False),
        ("valid_to", -1, False),
        ("valid_to", 0, False),
        ("valid_to", 1, True),
        ("invalid_at", -1, False),
        ("invalid_at", 0, False),
        ("invalid_at", 1, True),
    ],
)
def test_temporal_boundaries_for_both_family_and_resolver(tmp_path, column, offset, eligible):
    from datetime import timedelta
    from saltmdb.db.schema import init_db

    conn = init_db(str(tmp_path / "boundary.db"))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        for eid in ("old", "head"):
            conn.execute(
                "INSERT INTO entities(id,created_at,updated_at,last_accessed_at,agent_id,scope,status,title,full_content,content_hash) "
                "VALUES (?,datetime('now'),datetime('now'),datetime('now'),'owner','private','raw',?,?,?)",
                (eid, eid, eid, eid),
            )
        conn.execute(
            f"INSERT INTO relations(id,source_id,target_id,predicate,{column}) "
            "VALUES ('edge','head','old','supersedes',?)",
            ((now + timedelta(seconds=offset)).isoformat(),),
        )
        conn.commit()
        with patch.object(ranking, "datetime") as clock:
            clock.now.return_value = now
            assert ranking._collapse_supersedes_families(["old", "head"], conn) == (
                ["head"] if eligible else ["old", "head"]
            )
            assert ranking._resolve_supersession_chains(conn, ["old"], [], []) == (
                {"old": "head"} if eligible else {}
            )
    finally:
        conn.close()


def test_chunk_candidate_search_sorts_nearest_first_then_id():
    conn = MagicMock()
    conn.execute.return_value.fetchall.side_effect = [
        [("z-id", 0.9, "h"), ("b-id", 0.1, "h"), ("mid-id", 0.5, "h"), ("a-id", 0.1, "h")],
        [(eid, "h", "raw") for eid in ("z-id", "b-id", "mid-id", "a-id")],
    ]
    with (
        patch.object(search_primitives, "get_connection", return_value=conn),
        patch.object(search_primitives, "close_connection"),
        patch("sqlite_vec.load"),
        patch.object(embedding_service, "embed_query_text", return_value=_axis(0)),
    ):
        result = search_primitives.chunk_candidate_search("q", [], [], 4, 1, "unused")
    assert result == (
        [("a-id", 0.1), ("b-id", 0.1), ("mid-id", 0.5), ("z-id", 0.9)],
        {
            "requested_chunk_rows": 4,
            "candidate_window": 4,
            "oversampling_multiplier": 1,
            "raw_chunk_rows": 4,
            "unique_fresh_entities": 4,
            "returned_entities": 4,
            "candidate_shortfall": 0,
        },
    )


def test_supersession_sort_key_preserves_id_for_falsey_timestamps():
    sort_key = getattr(ranking, "_supersession_sort_key", None)
    if sort_key is None:
        pytest.skip("pre-extraction HEAD has no isolated sort-key helper")
    assert sort_key(
        "fixed-z",
        {"fixed-z": {"updated_at": None, "created_at": None}},
    ) == ("", "", "fixed-z")


def test_preview_equal_distance_keeps_first_span():
    content = "0123456789" * 60
    conn = MagicMock()
    conn.execute.return_value.fetchall.side_effect = [
        [("id", 0, 100, 104, 0.5)],
        [("id", 0, 300, 304, 0.5)],
        [("id", content)],
    ]
    with (
        patch.object(search_primitives, "get_connection", return_value=conn),
        patch.object(search_primitives, "close_connection"),
        patch("sqlite_vec.load"),
        patch.object(embedding_service, "embed_query_texts", return_value=[_axis(0), _axis(1)]),
    ):
        result = search_primitives.get_relevance_preview_data("q", ["id"], "unused")
    assert result == {"id": {"text": "0123456789" * 30 + "0123"}}


def test_preview_chunk_span_order_is_preserved_without_paragraph_rescue():
    content = "0123456789" * 60
    conn = MagicMock()
    conn.execute.return_value.fetchall.side_effect = [
        [("id", 0, 250, 253, 0.5)],
        [("id", content)],
    ]
    with (
        patch.object(search_primitives, "get_connection", return_value=conn),
        patch.object(search_primitives, "close_connection"),
        patch("sqlite_vec.load"),
        patch.object(embedding_service, "embed_query_texts", return_value=[_axis(0)]),
    ):
        result = search_primitives.get_relevance_preview_data("q", ["id"], "unused")
    assert result == {"id": {"text": "0123456789" * 40 + "012"}}


def test_chunk_malformed_rows_minimum_and_finite_filter():
    conn = MagicMock()
    conn.execute.return_value.fetchall.side_effect = [
        [
            ("good", 0.8, "h"),
            ("good", 0.2, "h"),
            ("good", 0.5, "h"),
            ("missing", 0.0, "h"),
            ("stale", 0.0, "old"),
            ("archived", 0.0, "h"),
            ("bad", "oops", "h"),
            ("null", None, "h"),
            ("nan", float("nan"), "h"),
            ("inf", float("inf"), "h"),
        ],
        [
            (eid, "h", "archived" if eid == "archived" else "raw")
            for eid in ("good", "stale", "archived", "bad", "null", "nan", "inf")
        ],
    ]
    with (
        patch.object(search_primitives, "get_connection", return_value=conn),
        patch.object(search_primitives, "close_connection"),
        patch("sqlite_vec.load"),
        patch.object(embedding_service, "embed_query_text", return_value=_axis(0)),
    ):
        assert search_primitives.chunk_candidate_search("q", [], [], 3, 4, "unused") == (
            [("good", 0.2)],
            {
                "requested_chunk_rows": 12,
                "candidate_window": 3,
                "oversampling_multiplier": 4,
                "raw_chunk_rows": 10,
                "unique_fresh_entities": 1,
                "returned_entities": 1,
                "candidate_shortfall": 2,
            },
        )


@pytest.mark.parametrize(
    "start,end,chunk_content,expected",
    [
        (1, 3, "fallback", "bc"),
        (None, 3, "fallback", "fallback"),
        (1, None, "fallback", "fallback"),
        ("bad", 3, "fallback", "fallback"),
        (1, "bad", "fallback", "fallback"),
        (2, 2, "fallback", "abcdef"),
        (None, None, None, "abcdef"),
    ],
)
def test_ce_chunk_span_fallbacks(start, end, chunk_content, expected):
    entity_conn = MagicMock()
    entity_conn.execute.return_value.fetchall.return_value = [("id", None, "abcdef", "hash")]
    chunk_conn = MagicMock()
    chunk_conn.execute.return_value.fetchall.return_value = [
        ("id", 0, start, end, chunk_content, 0.25),
        ("id", 1, 0, 1, "ignored", 0.25),
        ("id", 2, 0, 1, "ignored", 0.75),
        ("id", 3, 0, 1, "ignored", "bad"),
        ("id", 4, 0, 1, "ignored", None),
        ("id", 5, 0, 1, "ignored", float("nan")),
        ("id", 6, 0, 1, "ignored", float("inf")),
    ]
    with (
        patch.object(ranking, "get_connection", return_value=chunk_conn),
        patch.object(ranking, "close_connection"),
        patch("sqlite_vec.load"),
        patch.object(embedding_service, "embed_query_text", return_value=_axis(0)),
    ):
        assert ranking._build_cross_encoder_candidate_texts("q", ["id"], entity_conn, "unused") == {
            "id": "\n\n" + expected
        }
