"""Literal public search responses captured before the SEARCH-2/3 refactor."""

import json
import sqlite_vec

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service, memory_service, reranker_service
from saltmdb.domain.services.memory_service import (
    orchestrator,
    ranking,
    search_primitives,
    validation,
)


@pytest.fixture
def search_db(tmp_path):
    path = str(tmp_path / "characterization.db")
    conn = init_db(path)
    for index, (eid, kind, owner, scope, core, status) in enumerate(
        [
            ("a", "event", "owner", "private", 0, "raw"),
            ("b", "decision", "owner", "private", 1, "raw"),
            ("c", "procedure", "other", "shared", 0, "raw"),
            ("d", "fact", "other", "private", 0, "raw"),
            ("z", "fact", "owner", "private", 0, "archived"),
        ]
    ):
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,"
            "memory_type,is_core,created_at,updated_at,last_accessed_at,context_id,agent_session_id,metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,'2020-01-01','2020-01-01',?,?,?,?)",
            (
                eid,
                f"Title {eid}",
                f"needle evidence for {eid}",
                f"hash-{eid}",
                owner,
                scope,
                status,
                kind,
                core,
                f"2020-01-0{index + 1}",
                "ctx" if eid == "a" else "elsewhere",
                "session" if eid == "b" else None,
                json.dumps({"project": "ctx", "safe": "yes", "drift_flag": "review"})
                if eid == "b"
                else "{}",
            ),
        )
    conn.execute(
        "INSERT INTO relations (id,source_id,target_id,predicate) VALUES ('rel','b','a','supersedes')"
    )
    conn.execute("INSERT INTO tags (id,name) VALUES ('tag','#needle-tag')")
    conn.execute("INSERT INTO tags (id,name,canonical_id) VALUES ('alias','#alias-tag','tag')")
    conn.execute("INSERT INTO entity_tags (entity_id,tag_id) VALUES ('a','alias')")
    conn.execute("INSERT INTO entity_tags (entity_id,tag_id) VALUES ('b','tag')")
    conn.commit()
    yield conn, path
    conn.close()


def _search_case(conn, path, options):
    options = dict(options)
    scenario = options.pop("scenario", "hybrid")
    enabled = options.pop("ce", False)
    scores = options.pop("scores", [1.0, 1.0, 3.0])
    external_ce = options.pop("external_ce", False)
    fallback = options.pop("fallback", False)
    preview = options.pop("preview", False)
    fts = [
        (
            eid,
            "t",
            "c",
            1,
            0,
            -2.0 - i,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            f"<mark>needle</mark> {eid}",
        )
        for i, eid in enumerate(("a", "b", "c"))
    ]
    semantic = [("c", 0.125), ("b", 0.25), ("a", 0.5)]
    if scenario == "empty":
        fts, semantic = [], []
    if scenario == "semantic":
        fts = []
    if scenario == "browse":
        options.setdefault("query_keywords", None)
    else:
        options.setdefault("query_keywords", "needle")
    options.setdefault("include_related", False)
    with ExitStack() as stack:
        stack.enter_context(patch("saltmdb.config.is_semantic_search_enabled", return_value=True))
        stack.enter_context(
            patch.object(search_primitives, "_run_fts_search", return_value=(fts, fallback))
        )
        stack.enter_context(
            patch.object(search_primitives, "semantic_search", return_value=semantic)
        )
        stack.enter_context(
            patch.object(
                search_primitives,
                "_score_topics_with_fallback",
                return_value={"c": {"topic_score": 0.9, "semantic_verdict": "SAME_TOPIC"}},
            )
        )
        stack.enter_context(
            patch.object(reranker_service, "is_cross_encoder_enabled", return_value=enabled)
        )
        if not external_ce:
            stack.enter_context(patch.object(reranker_service, "score_pairs", return_value=scores))
        stack.enter_context(
            patch.object(
                search_primitives,
                "get_relevance_preview_data",
                return_value={
                    "a": {"text": "alpha preview"},
                    "b": {"text": "beta preview"},
                    "c": {"text": "gamma preview"},
                }
                if preview
                else {},
            )
        )
        return memory_service.search_memory(db_connection=conn, db_path=path, **options)


class TestSearchMemoryCharacterization:
    def test_mode_fallback_close_path_and_zero_limit(self, search_db):
        conn, path = search_db
        fallback = _search_case(conn, path, {"mode": "unknown", "limit": 1})
        closed = memory_service.search_memory(
            query_keywords=None, limit=2, db_path=path, agent_id="owner", include_related=False
        )
        zero = _search_case(conn, path, {"limit": 0})
        assert [item["id"] for item in fallback] == ["a"]
        assert [item["id"] for item in closed] == ["b", "a"]
        assert zero == []

    def test_explain_invalid_tag_and_diagnostics_envelope(self, search_db):
        conn, path = search_db
        explained = memory_service.search_memory(
            query_keywords="needle",
            explain_mode=True,
            tags_filter=["#missing"],
            return_diagnostics=True,
            db_connection=conn,
            db_path=path,
        )
        assert explained["explain"]["invalid_tags_suggestions"] == ["#missing"]
        assert explained["diagnostics"]["candidate_pool_size"] == 0

    def test_tag_and_missing_fallback_and_related_entities(self, search_db):
        conn, path = search_db
        relation = memory_service.search_memory(
            query_keywords=None,
            tags_filter=["#needle-tag", "#missing"],
            tag_operator="AND",
            db_connection=conn,
            db_path=path,
        )
        assert relation == []
        conn.execute(
            "INSERT INTO relations (id,source_id,target_id,predicate) VALUES ('rel2','a','b','related_to')"
        )
        conn.commit()
        related = memory_service.search_memory(
            query_keywords=None, agent_id="owner", limit=1, db_connection=conn, db_path=path
        )
        assert related[0]["id"] == "b"
        assert related[0]["related_entities"] == [
            {"predicate": "supersedes", "id": "a", "title": "Title a"},
            {"predicate": "related_to", "id": "a", "title": "Title a"},
        ]

    def test_chunk_and_retrieval_channels_are_pinned(self, search_db):
        conn, path = search_db
        with ExitStack() as stack:
            chunk = stack.enter_context(
                patch.object(
                    search_primitives,
                    "chunk_candidate_search",
                    return_value=(
                        [("b", 0.1)],
                        {
                            "requested_chunk_rows": 4,
                            "candidate_window": 20,
                            "oversampling_multiplier": 4,
                            "raw_chunk_rows": 1,
                            "unique_fresh_entities": 1,
                            "returned_entities": 1,
                            "candidate_shortfall": 0,
                            "executed": False,
                        },
                    ),
                )
            )
            retrieval_fts = stack.enter_context(
                patch.object(
                    search_primitives, "_run_retrieval_fts_search", return_value=[("c", 0.1)]
                )
            )
            retrieval_vec = stack.enter_context(
                patch.object(
                    search_primitives, "retrieval_vector_search", return_value=[("b", 0.2)]
                )
            )
            result = _search_case(
                conn,
                path,
                {
                    "limit": 3,
                    "use_chunk_candidates": True,
                    "oversampling_multiplier": 4,
                    "candidate_window": 20,
                    "chunk_weight": 1.0,
                    "use_retrieval_text_candidates": True,
                    "retrieval_fts_weight": 1.0,
                    "retrieval_vector_weight": 1.5,
                    "return_diagnostics": True,
                },
            )
        assert chunk.call_count == 1
        assert retrieval_fts.call_count == 1
        assert retrieval_vec.call_count == 1
        assert result["diagnostics"]["chunk_candidate"]["executed"] is True
        assert result["diagnostics"]["retrieval_text"]["fts_candidate_count"] == 1
        assert result["diagnostics"]["retrieval_text"]["vector_candidate_count"] == 1

    def test_cross_encoder_caps_and_trace_evidence(self, search_db):
        conn, path = search_db
        conn.execute(
            "INSERT INTO conversation_traces (id,agent_session_id,agent_id,harness,harness_session_id,harness_turn_id,status,user_prompt,user_prompt_hash,created_at,updated_at) VALUES ('trace','session','owner','omp','hs','ht','completed','p','h','2020-01-01','2020-01-01')"
        )
        conn.execute(
            "INSERT INTO trace_memory_links (id,trace_id,entity_id,content_hash,write_operation,created_at) VALUES ('link','trace','a','hash-a','store_memory_new','2020-01-01')"
        )
        conn.commit()
        with (
            patch.object(reranker_service, "is_cross_encoder_enabled", return_value=True),
            patch.object(reranker_service, "score_pairs", return_value=[0.2, 0.3, 0.1]) as score,
        ):
            result = _search_case(
                conn,
                path,
                {
                    "limit": 3,
                    "include_related": True,
                    "ce": True,
                    "external_ce": True,
                    "cross_encoder_candidate_cap": 10,
                    "cross_encoder_text_cap_chars": 1000,
                },
            )
        assert score.call_args.kwargs == {"candidate_cap": 10, "text_cap_chars": 1000}
        assert next(item for item in result if item["id"] == "a")["trace_evidence_count"] == 1

    def test_empty_semantic_result(self, search_db):
        conn, path = search_db
        empty = _search_case(conn, path, {"scenario": "empty", "limit": 2})
        assert empty == []

    def test_broad_history_strict_and_collapse_full_orders(self, search_db):
        conn, path = search_db
        broad = _search_case(conn, path, {"limit": 3})
        history = _search_case(conn, path, {"mode": "history", "limit": 3})
        strict = _search_case(conn, path, {"mode": "strict", "limit": 3})
        collapsed = _search_case(conn, path, {"limit": 3, "collapse_supersedes_families": True})
        assert [item["id"] for item in broad] == ["a", "c", "b"]
        assert [item["id"] for item in history] == ["a", "c", "b"]
        assert history[0]["is_superseded"] is True
        assert [item["id"] for item in strict] == ["b", "c"]
        assert [item["id"] for item in collapsed] == ["b", "c"]

    def test_flags_cursor_scores_and_preview_payload(self, search_db):
        conn, path = search_db
        durable = _search_case(
            conn, path, {"limit": 2, "prefer_durable_types": True, "preview": True}
        )
        demoted = _search_case(conn, path, {"limit": 3, "demote_superseded": True})
        page = _search_case(conn, path, {"limit": 1, "cursor": "offset:1"})
        assert [item["id"] for item in durable] == ["c", "b"]
        assert durable[0]["relevance_preview_meta"]["complete"] is False
        assert [item["id"] for item in demoted] == ["c", "b", "a"]
        assert page[0]["id"] == "c"
        assert page[0]["cursor"] == "offset:2"

    def test_diagnostics_full_pool_and_ce_reorder(self, search_db):
        conn, path = search_db
        result = _search_case(
            conn,
            path,
            {"limit": 2, "return_diagnostics": True, "ce": True, "scores": [0.1, 0.9, 0.2]},
        )
        assert [item["id"] for item in result["results"]] == ["c", "b"]
        assert [item["cross_encoder_score"] for item in result["results"]] == [0.9, 0.2]
        assert result["diagnostics"]["candidate_pool_size"] == 3
        assert result["diagnostics"]["cross_encoder"]["executed"] is True

    def test_metadata_filter_error_and_semantic_disabled_contract(self, search_db):
        conn, path = search_db
        bad = _search_case(conn, path, {"scenario": "browse", "metadata_filter": {"bad.key": "x"}})
        assert bad == [
            {"error": "metadata_filter key 'bad.key' is invalid; keys must match '^[A-Za-z0-9_]+$'"}
        ]
        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            error = memory_service.search_memory(
                query_keywords="needle", db_connection=conn, db_path=path
            )
        assert error[0]["error"].startswith("Semantic search is disabled")

    def test_unknown_mode_matches_explicit_broad(self, search_db):
        conn, path = search_db
        assert _search_case(conn, path, {"mode": "unknown", "limit": 3}) == _search_case(
            conn, path, {"mode": "broad", "limit": 3}
        )

    def test_owner_filter_keeps_shared_and_own_rows(self, search_db):
        conn, path = search_db
        rows = memory_service.search_memory(
            agent_id="owner",
            limit=10,
            query_keywords=None,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert [row["id"] for row in rows] == ["b", "a", "c"]

    def test_context_session_core_and_type_filters_return_full_ids(self, search_db):
        conn, path = search_db
        assert [
            row["id"]
            for row in memory_service.search_memory(
                context_id="ctx",
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["b", "a"]
        assert [
            row["id"]
            for row in memory_service.search_memory(
                agent_session_id="session",
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["b"]
        assert [
            row["id"]
            for row in memory_service.search_memory(
                is_core=True,
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["b"]
        assert [
            row["id"]
            for row in memory_service.search_memory(
                memory_type_filter="procedure",
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["c"]

    def test_valid_metadata_filter_and_numeric_mismatch(self, search_db):
        conn, path = search_db
        conn.execute('UPDATE entities SET metadata = \'{"safe":"yes","number":7}\' WHERE id=\'a\'')
        conn.commit()
        assert [
            row["id"]
            for row in memory_service.search_memory(
                metadata_filter={"safe": "yes"},
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["b", "a"]
        assert (
            memory_service.search_memory(
                metadata_filter={"number": 7},
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
            == []
        )

    def test_tag_or_and_alias_results_are_exact(self, search_db):
        conn, path = search_db
        assert [
            row["id"]
            for row in memory_service.search_memory(
                tags_filter=["#needle-tag"],
                tag_operator="OR",
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["b", "a"]
        assert memory_service.search_memory(
            tags_filter=["#needle-tag", "#missing"],
            tag_operator="OR",
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        ) == [
            {
                "id": "b",
                "title": "Title b",
                "snippet": "needle evidence for b",
                "score": 0.0,
                "weight": 1,
                "is_core": True,
                "memory_type": "decision",
                "cursor": "offset:10",
                "drift_flag": "review",
                "trace_evidence_count": 0,
            },
            {
                "id": "a",
                "title": "Title a",
                "snippet": "needle evidence for a",
                "score": 0.0,
                "weight": 1,
                "is_core": False,
                "memory_type": "event",
                "cursor": "offset:10",
                "trace_evidence_count": 0,
            },
        ]

    def test_exact_title_fast_path_requires_unique_active_identity(self, search_db):
        conn, path = search_db
        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            assert [
                row["id"]
                for row in memory_service.search_memory(
                    query_keywords="Title a",
                    limit=1,
                    include_related=False,
                    db_connection=conn,
                    db_path=path,
                )
            ] == ["a"]
            assert (
                memory_service.search_memory(
                    query_keywords="Title a",
                    cursor="offset:1",
                    limit=1,
                    include_related=False,
                    db_connection=conn,
                    db_path=path,
                )
                == []
            )
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
            "VALUES ('a2','Title a','needle duplicate','hash-a2','owner','private','raw','fact',0,'2020-01-01','2020-01-01','2020-01-01')"
        )
        conn.commit()
        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            result = memory_service.search_memory(
                query_keywords="Title a",
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["error"].startswith("Semantic search is disabled")

    def test_fts_snippet_is_not_replaced_by_body_extractor(self, search_db):
        conn, path = search_db
        with patch.object(search_primitives, "_run_fts_search") as fts:
            fts.return_value = (
                [
                    (
                        "a",
                        "t",
                        "body",
                        1,
                        0,
                        -1.0,
                        "",
                        "",
                        "owner",
                        "private",
                        "{}",
                        None,
                        "fact",
                        0,
                        "DISTINCT FTS",
                    ),
                ],
                False,
            )
            with patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]):
                result = memory_service.search_memory(
                    query_keywords="needle",
                    limit=1,
                    include_related=False,
                    db_connection=conn,
                    db_path=path,
                )
        assert result[0]["snippet"] == "DISTINCT FTS"

    def test_diagnostics_candidate_cap_and_pool_count_are_distinct(self, search_db):
        conn, path = search_db
        with patch.object(orchestrator, "SEARCH_DIAGNOSTICS_MAX_CANDIDATES", 1):
            result = _search_case(conn, path, {"limit": 3, "return_diagnostics": True})
        assert len(result["diagnostics"]["candidates"]) == 1
        assert result["diagnostics"]["candidate_pool_size"] == 3

    def test_diagnostics_disabled_and_no_candidate_reasons(self, search_db):
        conn, path = search_db
        disabled = _search_case(conn, path, {"limit": 1, "return_diagnostics": True})
        empty = _search_case(
            conn, path, {"scenario": "empty", "limit": 1, "return_diagnostics": True}
        )
        assert disabled["diagnostics"]["cross_encoder"]["reason"] == "cross_encoder_disabled"
        assert empty["diagnostics"]["cross_encoder"]["reason"] == "no_candidates"

    def test_error_diagnostics_contains_exact_error(self, search_db):
        conn, path = search_db
        result = _search_case(
            conn,
            path,
            {"scenario": "browse", "return_diagnostics": True, "metadata_filter": {"bad.key": "x"}},
        )
        assert result["results"] == []
        assert result["error"] == result["diagnostics"]["error"]
        assert "metadata_filter key" in result["error"]

    def test_preview_budget_stops_after_first_payload(self, search_db):
        conn, path = search_db
        with patch.object(orchestrator, "RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS", 5):
            result = _search_case(conn, path, {"limit": 3, "preview": True})
        assert "relevance_preview" in result[0]
        assert "relevance_preview" not in result[1]
        assert "relevance_preview" not in result[2]

    def test_direct_db_path_connection_is_closed(self, search_db):
        _, path = search_db
        with patch("saltmdb.domain.services.memory_service.orchestrator.close_connection") as close:
            result = memory_service.search_memory(
                query_keywords=None, agent_id="owner", limit=1, include_related=False, db_path=path
            )
        assert result[0]["id"] == "b"
        close.assert_called_once()

    def test_limit_zero_is_an_empty_result_for_exact_title(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            query_keywords="Title a",
            limit=0,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert result == []

    def test_archived_rows_never_enter_browse_results(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            query_keywords=None, limit=10, include_related=False, db_connection=conn, db_path=path
        )
        assert "z" not in [row["id"] for row in result]

    def test_browse_order_uses_core_then_updated_desc(self, search_db):
        conn, path = search_db
        conn.execute("UPDATE entities SET updated_at='2020-01-03' WHERE id='a'")
        conn.execute("UPDATE entities SET updated_at='2020-01-02' WHERE id='b'")
        conn.commit()
        rows = memory_service.search_memory(
            query_keywords=None, limit=10, include_related=False, db_connection=conn, db_path=path
        )
        assert [row["id"] for row in rows] == ["b", "a", "c", "d"]

    def test_score_rounding_is_six_decimal_absolute(self, search_db):
        conn, path = search_db
        with (
            patch.object(
                search_primitives,
                "weighted_reciprocal_rank_fusion",
                return_value={"a": -1.23456789},
            ),
            patch.object(search_primitives, "_run_fts_search", return_value=([("a",)], False)),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["score"] == 1.234568

    def test_strict_gate_drops_ungrounded_candidate(self, search_db):
        conn, path = search_db
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.9)]),
            patch.object(search_primitives, "_score_topics_with_fallback", return_value={}),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=3,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []

    def test_strict_or_fallback_candidate_uses_topic_evidence(self, search_db):
        conn, path = search_db
        with (
            patch.object(
                search_primitives,
                "_run_fts_search",
                return_value=(
                    [
                        (
                            "a",
                            "t",
                            "c",
                            1,
                            0,
                            -1.0,
                            "",
                            "",
                            "owner",
                            "private",
                            "{}",
                            None,
                            "fact",
                            0,
                            "<mark>needle</mark> a",
                        )
                    ],
                    True,
                ),
            ),
            patch.object(search_primitives, "semantic_search", return_value=[]),
            patch.object(
                search_primitives,
                "_score_topics_with_fallback",
                return_value={"a": {"topic_score": 0.9, "semantic_verdict": "SAME_TOPIC"}},
            ),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []

    def test_disable_semantic_per_call_is_distinct_from_environment(self, search_db):
        conn, path = search_db
        result = _search_case(conn, path, {"disable_semantic": True, "limit": 1})
        assert result[0]["error"].startswith("Semantic search is disabled")

    def test_explain_strict_and_history_have_identical_explain_payloads(self, search_db):
        conn, path = search_db
        broad = memory_service.search_memory(
            query_keywords="needle",
            explain_mode=True,
            mode="broad",
            return_diagnostics=True,
            db_connection=conn,
            db_path=path,
        )
        strict = memory_service.search_memory(
            query_keywords="needle",
            explain_mode=True,
            mode="strict",
            return_diagnostics=True,
            db_connection=conn,
            db_path=path,
        )
        history = memory_service.search_memory(
            query_keywords="needle",
            explain_mode=True,
            mode="history",
            return_diagnostics=True,
            db_connection=conn,
            db_path=path,
        )
        assert strict == broad
        assert history == broad

    def test_strict_mode_bypasses_exact_title_identity_fast_path(self, search_db):
        conn, path = search_db
        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            result = memory_service.search_memory(
                query_keywords="Title a",
                mode="strict",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["error"].startswith("Semantic search is disabled")

    def test_title_collision_disables_identity_fast_path(self, search_db):
        conn, path = search_db
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
            "VALUES ('a2','Title a','needle duplicate','hash-a2','owner','private','raw','fact',0,'2020-01-01','2020-01-01','2020-01-01')"
        )
        conn.commit()
        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            result = memory_service.search_memory(
                query_keywords="Title a",
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["error"].startswith("Semantic search is disabled")

    def test_negative_cursor_offset_does_not_use_identity_fast_path(self, search_db):
        conn, path = search_db
        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            result = memory_service.search_memory(
                query_keywords="Title a",
                cursor="offset:-1",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []

    def test_false_core_and_fact_type_filters_are_not_treated_as_omitted(self, search_db):
        conn, path = search_db
        assert [
            row["id"]
            for row in memory_service.search_memory(
                is_core=False,
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["a", "c", "d"]
        assert [
            row["id"]
            for row in memory_service.search_memory(
                memory_type_filter="fact",
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        ] == ["d"]

    def test_unknown_tag_operator_uses_or_branch(self, search_db):
        conn, path = search_db
        rows = memory_service.search_memory(
            tags_filter=["#needle-tag"],
            tag_operator="AAA",
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert [row["id"] for row in rows] == ["b", "a"]

    def test_execution_rate_is_exactly_zero_or_one(self, search_db):
        conn, path = search_db
        disabled = _search_case(conn, path, {"return_diagnostics": True, "limit": 1})
        enabled = _search_case(
            conn,
            path,
            {"return_diagnostics": True, "limit": 1, "ce": True, "scores": [0.1, 0.2, 0.3]},
        )
        assert disabled["diagnostics"]["cross_encoder"]["execution_rate"] == 0.0
        assert enabled["diagnostics"]["cross_encoder"]["execution_rate"] == 1.0

    def test_collapse_validation_rejects_strict_and_history(self, search_db):
        conn, path = search_db
        for mode in ("strict", "history"):
            with pytest.raises(ValueError, match="broad mode"):
                memory_service.search_memory(
                    query_keywords="needle",
                    mode=mode,
                    collapse_supersedes_families=True,
                    db_connection=conn,
                    db_path=path,
                )

    def test_unique_title_length_check_is_value_equality(self, search_db):
        conn, path = search_db
        with (
            patch("saltmdb.config.is_semantic_search_enabled", return_value=True),
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
        ):
            result = memory_service.search_memory(
                query_keywords="Title a",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["id"] == "a"

    def test_unknown_operator_with_two_tags_stays_or(self, search_db):
        conn, path = search_db
        rows = memory_service.search_memory(
            tags_filter=["#needle-tag", "#missing"],
            tag_operator="A",
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert [row["id"] for row in rows] == ["b", "a"]

    def test_collapse_guard_uses_value_not_identity_for_mode(self, search_db):
        conn, path = search_db
        dynamic_broad = "".join(["br", "oad"])
        with patch("saltmdb.config.is_semantic_search_enabled", return_value=False):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode=dynamic_broad,
                collapse_supersedes_families=True,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["error"].startswith("Semantic search is disabled")

    def test_unique_title_fast_path_wins_over_other_candidates(self, search_db):
        conn, path = search_db
        fts_row = (
            "c",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "other",
            "shared",
            "{}",
            None,
            "procedure",
            0,
            "<mark>needle</mark> c",
        )
        with (
            patch("saltmdb.config.is_semantic_search_enabled", return_value=True),
            patch.object(search_primitives, "_run_fts_search", return_value=([fts_row], False)),
            patch.object(search_primitives, "semantic_search", return_value=[("c", 0.1)]),
        ):
            result = memory_service.search_memory(
                query_keywords="Title a",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["id"] == "a"

    def test_dynamic_and_tag_operator_is_not_identity_comparison(self, search_db):
        conn, path = search_db
        dynamic_and = "".join(["A", "N", "D"])
        rows = memory_service.search_memory(
            tags_filter=["#needle-tag", "#missing"],
            tag_operator=dynamic_and,
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert rows == []

    def test_malformed_cursor_falls_back_to_zero_offset(self, search_db):
        conn, path = search_db
        result = _search_case(conn, path, {"cursor": "offset:not-an-int", "limit": 3})
        assert [row["id"] for row in result] == ["a", "c", "b"]

    def test_blank_tags_are_an_empty_filter(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            tags_filter=[" ", ""],
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert [row["id"] for row in result] == ["b", "a", "c", "d"]

    def test_or_filter_with_only_missing_tags_is_empty(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            tags_filter=["#missing"],
            tag_operator="OR",
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert result == []

    def test_explain_valid_tag_has_no_invalid_suggestion(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            query_keywords=None,
            tags_filter=["#needle-tag"],
            explain_mode=True,
            return_diagnostics=True,
            db_connection=conn,
            db_path=path,
        )
        assert result["explain"]["invalid_tags_suggestions"] == []

    def test_semantic_pipeline_resolves_db_path_when_connection_is_supplied(self, search_db):
        conn, path = search_db
        with (
            patch.object(orchestrator, "get_db_path", return_value=path),
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                db_connection=conn,
                db_path="",
                limit=1,
                include_related=False,
            )
        assert result == []

    def test_retrieval_text_with_no_ids_skips_hash_lookup(self, search_db):
        conn, path = search_db
        with (
            patch.object(search_primitives, "_run_retrieval_fts_search", return_value=[]),
            patch.object(search_primitives, "retrieval_vector_search", return_value=[]),
        ):
            result = _search_case(
                conn,
                path,
                {
                    "use_retrieval_text_candidates": True,
                    "retrieval_fts_weight": 1.0,
                    "retrieval_vector_weight": 1.0,
                    "return_diagnostics": True,
                },
            )
        assert result["diagnostics"]["retrieval_text"]["candidate_evidence"] == {}

    def test_strict_empty_pool_exercises_cross_encoder_skip(self, search_db):
        conn, path = search_db
        result = _search_case(
            conn,
            path,
            {"mode": "strict", "scenario": "empty", "ce": True, "return_diagnostics": True},
        )
        assert result["results"] == []
        assert result["diagnostics"]["cross_encoder"]["execution_count"] == 0

    def test_related_results_are_capped_at_five(self, search_db):
        conn, path = search_db
        for index in range(7):
            conn.execute(
                "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
                "VALUES (?,?,?,?,?,'shared','raw','fact',0,'2020-01-01','2020-01-01','2020-01-01')",
                (f"many-{index}", f"Many {index}", "body", f"many-hash-{index}", "other"),
            )
            conn.execute(
                "INSERT INTO relations (id,source_id,target_id,predicate) VALUES (?,?,?,'related_to')",
                (f"many-rel-{index}", "b", f"many-{index}"),
            )
        conn.commit()
        result = memory_service.search_memory(
            query_keywords=None,
            agent_id="owner",
            limit=1,
            include_related=True,
            db_connection=conn,
            db_path=path,
        )
        assert len(result[0]["related_entities"]) == 5

    def test_invalid_json_metadata_is_ignored_without_result_loss(self, search_db):
        conn, path = search_db
        with patch.object(
            orchestrator.json,
            "loads",
            side_effect=json.JSONDecodeError("bad", "{", 0),
        ):
            result = memory_service.search_memory(
                query_keywords=None,
                limit=10,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        row = next(item for item in result if item["id"] == "a")
        assert "drift_flag" not in row

    def test_empty_rrf_pool_skips_cross_encoder_block(self, search_db):
        conn, path = search_db
        fts_row = (
            "a",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "<mark>needle</mark> a",
        )
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([fts_row], False)),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]),
            patch.object(search_primitives, "weighted_reciprocal_rank_fusion", return_value={}),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []

    def test_valid_metadata_drift_flag_precedes_body_snippet_fallback(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            query_keywords=None, limit=10, include_related=False, db_connection=conn, db_path=path
        )
        row = next(item for item in result if item["id"] == "b")
        assert row["drift_flag"] == "review"

    def test_json_scalar_metadata_skips_drift_extraction(self, search_db):
        conn, path = search_db
        conn.execute("UPDATE entities SET metadata='null' WHERE id='a'")
        conn.commit()
        result = memory_service.search_memory(
            query_keywords=None, limit=10, include_related=False, db_connection=conn, db_path=path
        )
        row = next(item for item in result if item["id"] == "a")
        assert "drift_flag" not in row

    def test_invalid_mode_warns_and_normalizes_to_broad(self, search_db, caplog):
        conn, path = search_db
        caplog.set_level("WARNING")
        invalid = _search_case(conn, path, {"mode": "invalid-mode", "limit": 3})
        broad = _search_case(conn, path, {"mode": "broad", "limit": 3})
        assert invalid == broad
        assert "search_memory: unknown mode='invalid-mode', falling back to 'broad'." in caplog.text

    def test_invalid_mode_collapse_behaves_as_normalized_broad(self, search_db):
        conn, path = search_db
        invalid = _search_case(
            conn, path, {"mode": "invalid-mode", "collapse_supersedes_families": True, "limit": 3}
        )
        broad = _search_case(
            conn, path, {"mode": "broad", "collapse_supersedes_families": True, "limit": 3}
        )
        assert invalid == broad

    def test_non_string_invalid_mode_is_accepted_as_broad(self, search_db):
        conn, path = search_db
        none_mode = _search_case(conn, path, {"mode": None, "limit": 3})
        int_mode = _search_case(conn, path, {"mode": 7, "limit": 3})
        broad = _search_case(conn, path, {"mode": "broad", "limit": 3})
        assert none_mode == broad
        assert int_mode == broad

    def test_runtime_built_mode_and_memory_type_values(self, search_db):
        conn, path = search_db
        strict = _search_case(conn, path, {"mode": "".join(["str", "ict"]), "limit": 3})
        history = _search_case(conn, path, {"mode": "".join(["his", "tory"]), "limit": 3})
        broad = _search_case(conn, path, {"mode": "".join(["br", "oad"]), "limit": 3})
        procedure = memory_service.search_memory(
            memory_type_filter="".join(["pro", "cedure"]),
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=search_db[0],
            db_path=search_db[1],
        )
        assert [row["id"] for row in strict] == ["b", "c"]
        assert [row["id"] for row in history] == ["a", "c", "b"]
        assert [row["id"] for row in broad] == ["a", "c", "b"]
        assert [row["id"] for row in procedure] == ["c"]

    def test_runtime_built_or_and_tag_values_preserve_semantics(self, search_db):
        conn, path = search_db
        runtime_or = "".join(["O", "R"])
        runtime_and = "and".upper()
        or_rows = memory_service.search_memory(
            tags_filter=["#needle-tag", "#missing"],
            tag_operator=runtime_or,
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        and_rows = memory_service.search_memory(
            tags_filter=["#needle-tag", "#missing"],
            tag_operator=runtime_and,
            query_keywords=None,
            limit=10,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert [row["id"] for row in or_rows] == ["b", "a"]
        assert and_rows == []

    def test_retrieval_rrf_contribution_literals_pin_rank_offsets_and_precision(self, search_db):
        conn, path = search_db
        conn.execute(
            "UPDATE entities SET retrieval_text_hash = CASE id WHEN 'a' THEN 'retrieval-a' WHEN 'b' THEN 'retrieval-b' END WHERE id IN ('a','b')"
        )
        conn.commit()
        with (
            patch.object(
                search_primitives,
                "_run_fts_search",
                return_value=(
                    [
                        (
                            "a",
                            "t",
                            "c",
                            1,
                            0,
                            -1.0,
                            "",
                            "",
                            "owner",
                            "private",
                            "{}",
                            None,
                            "fact",
                            0,
                            "fts",
                        )
                    ],
                    False,
                ),
            ),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]),
            patch.object(
                search_primitives,
                "_run_retrieval_fts_search",
                return_value=[("a", 0.1), ("b", 0.2)],
            ),
            patch.object(
                search_primitives,
                "retrieval_vector_search",
                return_value=[("a", 0.3)],
            ),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=1.5,
                retrieval_vector_weight=1.0,
                return_diagnostics=True,
                limit=3,
                db_connection=conn,
                db_path=path,
            )
        evidence = result["diagnostics"]["retrieval_text"]["candidate_evidence"]
        assert evidence == {
            "a": {
                "present": True,
                "hash": "retrieval-a",
                "fts_rank": 0,
                "vector_rank": 0,
                "rrf_contribution": 0.040983607,
            },
            "b": {
                "present": True,
                "hash": "retrieval-b",
                "fts_rank": 1,
                "vector_rank": None,
                "rrf_contribution": 0.024193548,
            },
        }

    def test_strict_overfetch_doubles_window_until_cap(self, search_db):
        conn, path = search_db
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
            "VALUES ('synthetic-39','Synthetic 39','needle synthetic','synthetic-39-hash','owner','private','raw','fact',0,'2020-01-01','2020-01-01','2020-01-01')"
        )
        conn.commit()
        fts_calls = []
        vector_calls = []

        def fts_rows(*args, **kwargs):
            window = args[4]
            fts_calls.append(window)
            return (
                [
                    (
                        f"synthetic-{index}",
                        "t",
                        "c",
                        1,
                        0,
                        -1.0,
                        "",
                        "",
                        "owner",
                        "private",
                        "{}",
                        None,
                        "fact",
                        0,
                        "fts",
                    )
                    for index in range(window)
                ],
                True,
            )

        def vector_rows(*args, **kwargs):
            window = args[3]
            vector_calls.append(window)
            return [(f"synthetic-{index}", 0.1) for index in range(window)]

        topic_calls = []

        def topic_scores(query, ids, db_path):
            topic_calls.append(list(ids))
            return (
                {"synthetic-39": {"topic_score": 0.9, "semantic_verdict": "SAME_SPECIFIC_TOPIC"}}
                if len(ids) == 40
                else {}
            )

        with (
            patch.object(search_primitives, "_run_fts_search", side_effect=fts_rows),
            patch.object(search_primitives, "semantic_search", side_effect=vector_rows),
            patch.object(
                search_primitives, "_score_topics_with_fallback", side_effect=topic_scores
            ),
            patch.object(orchestrator, "STRICT_OVERFETCH_CANDIDATE_CAP", 80),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert [row["id"] for row in result] == ["synthetic-39"]
        assert fts_calls == [20, 40]
        assert vector_calls == [20, 40]

    def test_strict_growth_clamps_non_power_window_to_cap(self, search_db):
        conn, path = search_db
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
            "VALUES ('growth-39','Growth 39','needle growth','growth-39-hash','owner','private','raw','fact',0,'2020-01-01','2020-01-01','2020-01-01')"
        )
        conn.commit()
        fts_calls = []
        vector_calls = []
        fts_row = (
            "growth-39",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "fts",
        )

        def fts_rows(*args, **kwargs):
            window = args[4]
            fts_calls.append(window)
            if len(fts_calls) > 2:
                raise AssertionError("strict growth exceeded configured cap")
            return [fts_row] * window, True

        def vector_rows(*args, **kwargs):
            window = args[3]
            vector_calls.append(window)
            if len(vector_calls) > 2:
                raise AssertionError("strict growth exceeded configured cap")
            return [("growth-39", 0.1)] * window

        topic_calls = []

        def topic_scores(query, ids, db_path):
            topic_calls.append(list(ids))
            return (
                {"growth-39": {"topic_score": 0.9, "semantic_verdict": "SAME_SPECIFIC_TOPIC"}}
                if len(topic_calls) == 2
                else {}
            )

        with (
            patch.object(search_primitives, "_run_fts_search", side_effect=fts_rows),
            patch.object(search_primitives, "semantic_search", side_effect=vector_rows),
            patch.object(
                search_primitives, "_score_topics_with_fallback", side_effect=topic_scores
            ),
            patch("saltmdb.config.RERANK_CANDIDATE_POOL_SIZE", 30),
            patch.object(orchestrator, "STRICT_OVERFETCH_CANDIDATE_CAP", 40),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert [row["id"] for row in result] == ["growth-39"]
        assert fts_calls == [30, 40]
        assert vector_calls == [30, 40]

    def test_strict_growth_loop_stops_at_patched_cap(self, search_db):
        conn, path = search_db
        fts_calls = []
        vector_calls = []
        fts_row = (
            "unaccepted",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "fts",
        )

        def fts_rows(*args, **kwargs):
            window = args[4]
            fts_calls.append(window)
            if len(fts_calls) > 2:
                raise AssertionError("strict growth loop exceeded patched cap")
            return [fts_row] * window, True

        def vector_rows(*args, **kwargs):
            window = args[3]
            vector_calls.append(window)
            if len(vector_calls) > 2:
                raise AssertionError("strict growth loop exceeded patched cap")
            return [("unaccepted", 0.1)] * window

        with (
            patch.object(search_primitives, "_run_fts_search", side_effect=fts_rows),
            patch.object(search_primitives, "semantic_search", side_effect=vector_rows),
            patch.object(search_primitives, "_score_topics_with_fallback", return_value={}),
            patch("saltmdb.config.RERANK_CANDIDATE_POOL_SIZE", 30),
            patch.object(orchestrator, "STRICT_OVERFETCH_CANDIDATE_CAP", 40),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []
        assert fts_calls == [30, 40]
        assert vector_calls == [30, 40]

    def test_strict_cap_clamps_initial_window_above_cap(self, search_db):
        conn, path = search_db
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
            "VALUES ('cap-boundary','Cap boundary','needle cap','cap-boundary-hash','owner','private','raw','fact',0,'2020-01-01','2020-01-01','2020-01-01')"
        )
        conn.commit()
        calls = []
        row = (
            "cap-boundary",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "fts",
        )

        def fts_rows(*args, **kwargs):
            calls.append(args[4])
            return [row] * args[4], True

        def semantic_rows(*args, **kwargs):
            return [("cap-boundary", 0.1)] * args[3]

        with (
            patch.object(search_primitives, "_run_fts_search", side_effect=fts_rows),
            patch.object(search_primitives, "semantic_search", side_effect=semantic_rows),
            patch.object(
                search_primitives,
                "_score_topics_with_fallback",
                return_value={
                    "cap-boundary": {
                        "topic_score": 0.9,
                        "semantic_verdict": "SAME_SPECIFIC_TOPIC",
                    }
                },
            ),
            patch.object(orchestrator, "STRICT_OVERFETCH_CANDIDATE_CAP", 40),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=50,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert [row["id"] for row in result] == ["cap-boundary"]
        assert calls == [40]

    def test_semantic_only_payload_has_heuristic_snippet_and_rrf_score(self, search_db):
        conn, path = search_db
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["id"] == "a"
        assert result[0]["snippet"] == "needle evidence for a"
        assert result[0]["score"] == 0.016393

    def test_empty_fts_snippet_uses_body_extractor(self, search_db):
        conn, path = search_db
        with (
            patch.object(
                search_primitives,
                "_run_fts_search",
                return_value=(
                    [
                        (
                            "a",
                            "t",
                            "c",
                            1,
                            0,
                            -1.0,
                            "",
                            "",
                            "owner",
                            "private",
                            "{}",
                            None,
                            "fact",
                            0,
                            "",
                        )
                    ],
                    False,
                ),
            ),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["snippet"] == "needle evidence for a"

    def test_include_related_without_edges_returns_empty_list(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            query_keywords=None,
            memory_type_filter="fact",
            include_related=True,
            limit=10,
            db_connection=conn,
            db_path=path,
        )
        assert [row["id"] for row in result] == ["d"]
        assert result[0]["related_entities"] == []

    def test_strict_topic_scoring_receives_only_ungrounded_ids(self, search_db):
        conn, path = search_db
        fts_rows = [
            ("a", "t", "c", 1, 0, -1.0, "", "", "owner", "private", "{}", None, "fact", 0, "a"),
            ("b", "t", "c", 1, 0, -2.0, "", "", "owner", "private", "{}", None, "decision", 0, "b"),
        ]
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=(fts_rows, False)),
            patch.object(
                search_primitives, "semantic_search", return_value=[("a", 0.1), ("b", 0.2)]
            ),
            patch.object(
                search_primitives, "_score_topics_with_fallback", return_value={}
            ) as score_topics,
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=2,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert [row["id"] for row in result] == ["b"]
        assert score_topics.call_args.args[1] == []

    def test_cross_encoder_missing_text_ids_are_not_scored(self, search_db):
        conn, path = search_db
        fts_rows = [
            ("a", "t", "c", 1, 0, -1.0, "", "", "owner", "private", "{}", None, "fact", 0, "a"),
            ("b", "t", "c", 1, 0, -2.0, "", "", "owner", "private", "{}", None, "decision", 0, "b"),
        ]
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=(fts_rows, False)),
            patch.object(
                search_primitives, "semantic_search", return_value=[("a", 0.1), ("b", 0.2)]
            ),
            patch.object(reranker_service, "is_cross_encoder_enabled", return_value=True),
            patch.object(
                orchestrator.ranking,
                "_build_cross_encoder_candidate_texts",
                return_value={"a": "needle a"},
            ),
            patch.object(reranker_service, "score_pairs", return_value=[0.9]) as score_pairs,
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                limit=2,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert score_pairs.call_args.args[1] == ["needle a"]
        assert [row["id"] for row in result] == ["a", "b"]

    def test_preview_path_uses_fallback_db_path_when_empty(self, search_db):
        conn, path = search_db
        fts_row = (
            "a",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "fts",
        )
        with (
            patch.object(orchestrator, "get_db_path", return_value=path) as get_path,
            patch.object(search_primitives, "_run_fts_search", return_value=([fts_row], False)),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]),
            patch.object(
                search_primitives,
                "get_relevance_preview_data",
                return_value={"a": {"text": "preview"}},
            ) as get_preview,
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                db_path="",
                include_related=False,
                limit=1,
                db_connection=conn,
            )
        assert result[0]["id"] == "a"
        assert get_path.call_count >= 1
        assert get_preview.call_args.args[2] == path

    def test_strict_fallback_supersession_scores_only_fts_predecessors(self, search_db):
        conn, path = search_db
        fts_row = (
            "a",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "a",
        )
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([fts_row], True)),
            patch.object(search_primitives, "semantic_search", return_value=[("b", 0.1)]),
            patch.object(
                search_primitives, "_score_topics_with_fallback", return_value={}
            ) as score_topics,
        ):
            memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert score_topics.call_args_list[0].args[1] == ["a"]

    def test_retrieval_exhaustion_controls_strict_window_growth(self, search_db):
        conn, path = search_db
        calls = []
        fts_row = (
            "a",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "a",
        )

        def fts_rows(*args, **kwargs):
            calls.append(args[4])
            if len(calls) > 1:
                raise AssertionError("strict overfetch ignored retrieval exhaustion")
            return [fts_row], True

        with (
            patch.object(search_primitives, "_run_fts_search", side_effect=fts_rows),
            patch.object(search_primitives, "semantic_search", return_value=[]),
            patch.object(search_primitives, "_run_retrieval_fts_search", return_value=[]),
            patch.object(search_primitives, "retrieval_vector_search", return_value=[]),
            patch.object(search_primitives, "_score_topics_with_fallback", return_value={}),
            patch.object(orchestrator, "STRICT_OVERFETCH_CANDIDATE_CAP", 40),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=1.0,
                retrieval_vector_weight=1.0,
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []
        assert calls == [20]

    def test_core_pool_prevents_retrieval_shortfall_early_stop(self, search_db):
        conn, path = search_db
        calls = []

        def fts_rows(*args, **kwargs):
            window = args[4]
            calls.append(window)
            if len(calls) > 2:
                raise AssertionError("strict overfetch exceeded configured cap")
            return (
                [
                    (
                        f"core-{index}",
                        "t",
                        "c",
                        1,
                        0,
                        -1.0,
                        "",
                        "",
                        "owner",
                        "private",
                        "{}",
                        None,
                        "fact",
                        0,
                        "fts",
                    )
                    for index in range(window)
                ],
                True,
            )

        def semantic_rows(*args, **kwargs):
            window = args[3]
            return [(f"core-{index}", 0.1) for index in range(window)]

        with (
            patch.object(search_primitives, "_run_fts_search", side_effect=fts_rows),
            patch.object(search_primitives, "semantic_search", side_effect=semantic_rows),
            patch.object(search_primitives, "_run_retrieval_fts_search", return_value=[]),
            patch.object(search_primitives, "retrieval_vector_search", return_value=[]),
            patch.object(search_primitives, "_score_topics_with_fallback", return_value={}),
            patch.object(orchestrator, "STRICT_OVERFETCH_CANDIDATE_CAP", 40),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=1.0,
                retrieval_vector_weight=1.0,
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []
        assert calls == [20, 40]

    def test_empty_string_query_does_not_build_preview(self, search_db):
        conn, path = search_db
        with patch.object(search_primitives, "get_relevance_preview_data") as get_preview:
            result = memory_service.search_memory(
                query_keywords="",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["id"] == "b"
        get_preview.assert_not_called()

    def test_no_rows_does_not_build_preview(self, search_db):
        conn, path = search_db
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
            patch.object(search_primitives, "get_relevance_preview_data") as get_preview,
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []
        get_preview.assert_not_called()

    def test_nonempty_db_path_does_not_call_fallback_for_preview(self, search_db):
        conn, path = search_db
        fts_row = (
            "a",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "fts",
        )
        with (
            patch.object(orchestrator, "get_db_path", return_value="/sentinel"),
            patch.object(search_primitives, "_run_fts_search", return_value=([fts_row], False)),
            patch.object(search_primitives, "semantic_search", return_value=[("a", 0.1)]),
            patch.object(
                search_primitives,
                "get_relevance_preview_data",
                return_value={"a": {"text": "preview"}},
            ) as get_preview,
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                db_path=path,
                include_related=False,
                limit=1,
                db_connection=conn,
            )
        assert result[0]["id"] == "a"
        assert get_preview.call_args.args[2] == path

    def test_error_logging_retains_exception_context(self, search_db, caplog):
        conn, path = search_db
        caplog.set_level("ERROR")
        result = memory_service.search_memory(
            query_keywords=None,
            metadata_filter={"bad.key": "x"},
            return_diagnostics=True,
            db_connection=conn,
            db_path=path,
        )
        assert "metadata_filter key" in result["error"]
        assert "Error searching memory: metadata_filter key" in caplog.text

    def test_multi_chunk_preview_literal_pins_selection_expansion_and_order(self, search_db):
        conn, path = search_db
        paragraphs = [f"Paragraph {index} needle detail" for index in range(8)]
        content = "\n\n".join(paragraphs)
        content_hash = "preview-content-hash"
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
            "VALUES ('preview-long','Preview Long',?,?,?,?,?,'fact',0,'2020-01-01','2020-01-01','2020-01-01')",
            (content, content_hash, "owner", "private", "raw"),
        )
        vector = sqlite_vec.serialize_float32([1.0] + [0.0] * 383)
        offset = 0
        for index, paragraph in enumerate(paragraphs):
            conn.execute(
                "INSERT INTO entity_chunk_embeddings (id,entity_id,embedding,chunk_index,char_start,char_end,content_hash) VALUES (?,?,?,?,?,?,?)",
                (
                    f"preview-long::{index}",
                    "preview-long",
                    vector,
                    index,
                    offset,
                    offset + len(paragraph),
                    content_hash,
                ),
            )
            offset += len(paragraph) + 2
        conn.commit()
        fts_row = (
            "preview-long",
            "t",
            "c",
            1,
            0,
            -1.0,
            "",
            "",
            "owner",
            "private",
            "{}",
            None,
            "fact",
            0,
            "<mark>needle</mark>",
        )
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([fts_row], False)),
            patch.object(
                search_primitives, "semantic_search", return_value=[("preview-long", 0.1)]
            ),
            patch.object(
                embedding_service, "embed_query_texts", return_value=[[1.0] + [0.0] * 383]
            ),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result[0]["relevance_preview"] == (
            "[UNVERIFIED PREVIEW -- call get_memory before citing as fact] "
            "Paragraph 0 needle detail\n\nParagraph 1 needle detail"
        )
        assert result[0]["relevance_preview_meta"] == {
            "auto_generated": True,
            "extractive": True,
            "query_specific": True,
            "complete": False,
        }

    def test_enabled_chunk_default_weight_is_one_and_affects_rrf_score(self, search_db):
        conn, path = search_db
        with (
            patch.object(
                search_primitives,
                "chunk_candidate_search",
                return_value=(
                    [("b", 0.1)],
                    {
                        "requested_chunk_rows": 1,
                        "candidate_window": 20,
                        "oversampling_multiplier": 4,
                        "raw_chunk_rows": 1,
                        "unique_fresh_entities": 1,
                        "returned_entities": 1,
                        "candidate_shortfall": 0,
                        "executed": True,
                    },
                ),
            ),
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                use_chunk_candidates=True,
                candidate_window=20,
                oversampling_multiplier=4,
                limit=1,
                include_related=False,
                return_diagnostics=True,
                db_connection=conn,
                db_path=path,
            )
        assert validation._validate_chunk_candidate_controls(True, 4, 20, None) == (
            4,
            20,
            1.0,
        )
        assert result["results"][0]["id"] == "b"
        assert result["results"][0]["score"] == 0.016393

    def test_cross_encoder_chunk_text_uses_exclusive_end(self, search_db):
        content = "A" * 100 + "B" * 200 + "C" * 100
        best = (0.1, 100, 300, "ignored")
        assert ranking._cross_encoder_evidence(content, best) == content[100:300]

    def test_non_linear_supersedes_fork_has_no_chain_head(self, search_db):
        class OrderedSet(set):
            def __iter__(self):
                return iter(("c", "d"))

        source_to_targets = {"a": {"b"}, "b": OrderedSet(("c", "d")), "c": {"d"}}
        target_to_sources = {"b": {"a"}, "c": {"b"}, "d": {"c", "b"}}
        assert (
            ranking._supersedes_chain_head(
                {"a", "b", "c", "d"}, source_to_targets, target_to_sources
            )
            is None
        )

    def test_cross_encoder_fallback_text_keeps_two_thousand_chars(self, search_db):
        content = "A" * 2500
        best = (0.1, None, None, "")
        assert ranking._cross_encoder_evidence(content, best) == content[:2000]

    def test_preview_opening_chunk_added_at_exact_k_plus_one_boundary(self, search_db):
        content = "0" * 100 + "1" * 200 + "2" * 200
        chunks = [(0, 0, 100, 1.0), (1, 300, 400, 0.0)]
        with patch.object(search_primitives, "RELEVANCE_PREVIEW_MAX_CHUNKS", 1):
            result = search_primitives._select_relevance_preview_text(content, chunks)
        assert result == content

    def test_preview_expansion_caps_are_exactly_two_hundred_chars(self, search_db):
        content = "P" * 300 + "Q" * 100 + "R" * 300
        chunks = [(1, 300, 400, 0.0)]
        with patch.object(search_primitives, "RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS", 200):
            result = search_primitives._select_relevance_preview_text(content, chunks)
        assert result == content[100:600]

    def test_omitted_limit_defaults_to_five_with_six_rows(self, search_db):
        conn, path = search_db
        for index in range(6):
            conn.execute(
                "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
                "VALUES (?,?,?,?,?,'shared','raw','fact',0,?,?,?)",
                (
                    f"limit-{index}",
                    f"Limit {index}",
                    "needle",
                    f"limit-hash-{index}",
                    "other",
                    "2020-02-01",
                    f"2020-02-{10 - index:02d}",
                    "2020-02-01",
                ),
            )
        conn.commit()
        result = memory_service.search_memory(
            query_keywords=None, include_related=False, db_connection=conn, db_path=path
        )
        assert [row["id"] for row in result] == [
            "b",
            "limit-0",
            "limit-1",
            "limit-2",
            "limit-3",
        ]

    def test_deep_cursor_sets_strict_base_window_to_offset_plus_limit(self, search_db):
        conn, path = search_db
        calls = []

        def fts_rows(*args, **kwargs):
            calls.append(args[4])
            return [], False

        with (
            patch.object(search_primitives, "_run_fts_search", side_effect=fts_rows),
            patch.object(search_primitives, "semantic_search", return_value=[]),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                mode="strict",
                cursor="offset:20",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert result == []
        assert calls == [21]

    def test_truthy_non_dict_metadata_filter_is_ignored(self, search_db):
        conn, path = search_db
        result = memory_service.search_memory(
            query_keywords=None,
            metadata_filter=["ignored"],
            limit=3,
            include_related=False,
            db_connection=conn,
            db_path=path,
        )
        assert [row["id"] for row in result] == ["b", "a", "c"]

    def test_preview_budget_one_keeps_only_first_preview(self, search_db):
        conn, path = search_db
        with patch.object(orchestrator, "RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS", 1):
            result = _search_case(conn, path, {"limit": 3, "preview": True})
        assert "relevance_preview" in result[0]
        assert "relevance_preview" not in result[1]
        assert "relevance_preview" not in result[2]

    def test_disabled_optional_channels_report_false_execution_flags(self, search_db):
        conn, path = search_db
        result = _search_case(conn, path, {"limit": 1, "return_diagnostics": True})
        diagnostics = result["diagnostics"]
        assert diagnostics["chunk_candidate"]["executed"] is False
        assert diagnostics["retrieval_text"]["executed"] is False
        assert diagnostics["cross_encoder"]["requested"] is True
        assert diagnostics["cross_encoder"]["executed"] is False

    def test_initial_optional_channel_diagnostics_are_zero(self, search_db):
        conn, path = search_db
        result = _search_case(conn, path, {"limit": 1, "return_diagnostics": True})
        diagnostics = result["diagnostics"]
        assert diagnostics["chunk_candidate"]["candidate_shortfall"] == 0
        assert diagnostics["retrieval_text"]["fts_candidate_count"] == 0
        assert diagnostics["retrieval_text"]["vector_candidate_count"] == 0

    def test_include_related_empty_page_returns_empty_list(self, search_db):
        conn, path = search_db
        result = _search_case(
            conn, path, {"scenario": "empty", "include_related": True, "limit": 1}
        )
        assert result == []

    def test_chunk_candidate_call_arguments_are_exact(self, search_db):
        conn, path = search_db
        with (
            patch.object(
                search_primitives,
                "chunk_candidate_search",
                return_value=([], {"candidate_shortfall": 0}),
            ) as chunk_search,
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
        ):
            _search_case(
                conn,
                path,
                {
                    "use_chunk_candidates": True,
                    "candidate_window": 20,
                    "oversampling_multiplier": 4,
                    "chunk_weight": 1.5,
                },
            )
        assert chunk_search.call_args.args[0] == "needle"
        assert chunk_search.call_args.args[3:6] == (20, 4, path)
        assert isinstance(chunk_search.call_args.args[1], list)
        assert isinstance(chunk_search.call_args.args[2], list)

    def test_invalid_cross_encoder_caps_raise(self, search_db):
        conn, path = search_db
        with pytest.raises(ValueError):
            _search_case(conn, path, {"cross_encoder_candidate_cap": 0})
        with pytest.raises(ValueError):
            _search_case(conn, path, {"cross_encoder_text_cap_chars": 0})

    def test_vector_rank_one_has_positive_rrf_contribution(self, search_db):
        conn, path = search_db
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
            patch.object(search_primitives, "_run_retrieval_fts_search", return_value=[("a", 0.1)]),
            patch.object(
                search_primitives, "retrieval_vector_search", return_value=[("a", 0.1), ("b", 0.2)]
            ),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=1.0,
                retrieval_vector_weight=1.0,
                return_diagnostics=True,
                limit=2,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        evidence = result["diagnostics"]["retrieval_text"]["candidate_evidence"]
        assert evidence["b"]["vector_rank"] == 1
        assert evidence["b"]["rrf_contribution"] == 0.016129032

    def test_float_chunk_controls_raise_validation_errors(self, search_db):
        conn, path = search_db
        with pytest.raises(ValueError):
            _search_case(
                conn,
                path,
                {"use_chunk_candidates": True, "oversampling_multiplier": 4.0},
            )
        with pytest.raises(ValueError):
            _search_case(
                conn,
                path,
                {"use_chunk_candidates": True, "candidate_window": 20.0},
            )

    def test_disabled_retrieval_weights_are_zero(self, search_db):
        conn, path = search_db
        result = _search_case(
            conn,
            path,
            {
                "retrieval_fts_weight": 0.0,
                "retrieval_vector_weight": 0.0,
                "return_diagnostics": True,
            },
        )
        assert result["diagnostics"]["retrieval_text"]["retrieval_fts_weight"] == 0.0
        assert result["diagnostics"]["retrieval_text"]["retrieval_vector_weight"] == 0.0
        assert result["diagnostics"]["retrieval_text"]["fts_candidate_count"] == 0
        assert result["diagnostics"]["retrieval_text"]["vector_candidate_count"] == 0

    def test_enabled_chunk_omitted_weight_uses_default(self, search_db):
        conn, path = search_db
        with (
            patch.object(
                search_primitives,
                "chunk_candidate_search",
                return_value=([], {"candidate_shortfall": 0}),
            ) as chunk_search,
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
        ):
            _search_case(
                conn,
                path,
                {
                    "use_chunk_candidates": True,
                    "candidate_window": 20,
                    "oversampling_multiplier": 4,
                },
            )
        assert chunk_search.call_args.args[3:5] == (20, 4)

    def test_dangling_supersedes_edge_keeps_search_payload_valid(self, search_db):
        conn, path = search_db
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,memory_type,is_core,created_at,updated_at,last_accessed_at) "
            "VALUES ('deleted-successor','Deleted','body','deleted-hash','other','shared','archived','fact',0,'2020-01-01','2020-01-01','2020-01-01')"
        )
        conn.execute(
            "INSERT INTO relations (id,source_id,target_id,predicate) VALUES ('dangling','b','deleted-successor','supersedes')"
        )
        conn.execute("DELETE FROM entities WHERE id='deleted-successor'")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        result = _search_case(conn, path, {"limit": 3})
        assert [row["id"] for row in result] == ["a", "c", "b"]

    def test_cross_encoder_receives_exact_candidate_texts(self, search_db):
        conn, path = search_db
        fts_rows = [
            ("a", "t", "c", 1, 0, -1.0, "", "", "owner", "private", "{}", None, "fact", 0, "a"),
            ("b", "t", "c", 1, 0, -2.0, "", "", "owner", "private", "{}", None, "decision", 0, "b"),
        ]
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=(fts_rows, False)),
            patch.object(
                search_primitives, "semantic_search", return_value=[("a", 0.1), ("b", 0.2)]
            ),
            patch.object(reranker_service, "is_cross_encoder_enabled", return_value=True),
            patch.object(
                orchestrator.ranking,
                "_build_cross_encoder_candidate_texts",
                return_value={"a": "Title a\nneedle evidence for a"},
            ),
            patch.object(reranker_service, "score_pairs", return_value=[0.9]) as score_pairs,
        ):
            memory_service.search_memory(
                query_keywords="needle",
                limit=2,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        assert score_pairs.call_args.args[1] == ["Title a\nneedle evidence for a"]

    def test_fts_stopword_filter_keeps_non_stopword_terms(self, search_db):
        conn, path = search_db
        conn.execute(
            "INSERT INTO entities_fts (id,title,full_content,search_aliases) VALUES ('a','Title a','needle evidence for a','')"
        )

        class FtsProxy:
            def __init__(self):
                self.queries = []

            def execute(self, sql, params=()):
                self.queries.append(params[0])
                return type(
                    "Rows",
                    (),
                    {"fetchall": lambda self: [("stopword-only", "t", "the", "owner")]},
                )()

        proxy = FtsProxy()
        stopword_only = search_primitives._run_fts_search(proxy, "the", [], [], 5, 0)
        assert stopword_only[0][0] == "stopword-only"
        assert proxy.queries == ['"the"*']
        with patch.object(search_primitives, "semantic_search", return_value=[]):
            memory_service.search_memory(
                query_keywords="the needle",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        direct_rows = search_primitives._run_fts_search(conn, "the needle", [], [], 5, 0)
        assert direct_rows and direct_rows[0][0] == "a"

    def test_retrieval_fts_successful_multi_term_and_is_not_retried_as_or(self, search_db):
        conn, path = search_db
        retrieval_text = "the needle evidence"
        retrieval_hash = "retrieval-text-hash"
        conn.execute(
            "UPDATE entities SET retrieval_text=?,retrieval_text_hash=? WHERE id='a'",
            (retrieval_text, retrieval_hash),
        )
        conn.execute(
            "INSERT INTO retrieval_fts (id,retrieval_text) VALUES ('a',?)",
            (retrieval_text,),
        )
        conn.commit()
        search_primitives._run_retrieval_fts_search(conn, "the needle", [], [], 5, 0)
        stopword_rows = search_primitives._run_retrieval_fts_search(conn, "the", [], [], 5, 0)
        assert stopword_rows and all(row[0] == "a" for row in stopword_rows)
        statements = []
        conn.set_trace_callback(statements.append)
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
            patch.object(search_primitives, "retrieval_vector_search", return_value=[]),
            patch.object(search_primitives, "get_connection", return_value=conn),
            patch.object(search_primitives, "close_connection"),
        ):
            result = memory_service.search_memory(
                query_keywords="needle evidence",
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=1.0,
                retrieval_vector_weight=1.0,
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
        conn.set_trace_callback(None)
        assert result[0]["id"] == "a"
        assert not any(
            " OR " in statement and "retrieval_fts" in statement for statement in statements
        )

    def test_retrieval_vector_pagination_uses_limit_plus_offset(self, search_db):
        conn, path = search_db
        calls = []

        class RetrievalProxy:
            def enable_load_extension(self, enabled):
                return None

            def load_extension(self, path):
                return None

            def execute(self, sql, params=()):
                if "FROM retrieval_embeddings" in sql:
                    calls.append(params[1])
                    return type(
                        "Rows",
                        (),
                        {
                            "fetchall": lambda self: [
                                ("a", 0.1, "retrieval-a"),
                                ("b", 0.2, "retrieval-b"),
                            ]
                        },
                    )()
                if "JOIN retrieval_embedding_jobs" in sql:
                    return type(
                        "Rows",
                        (),
                        {
                            "fetchall": lambda self: [
                                ("a", "retrieval-a", "raw"),
                                ("b", "retrieval-b", "raw"),
                            ]
                        },
                    )()
                return conn.execute(sql, params)

        proxy = RetrievalProxy()
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "semantic_search", return_value=[]),
            patch.object(search_primitives, "get_connection", return_value=proxy),
            patch("saltmdb.db.connection.get_connection", return_value=proxy),
            patch("saltmdb.domain.services.memory_service.search_primitives.close_connection"),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                cursor="offset:1",
                limit=1,
                use_retrieval_text_candidates=True,
                retrieval_fts_weight=1.0,
                retrieval_vector_weight=1.0,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
            direct = search_primitives.retrieval_vector_search("needle", [], [], 1, path, offset=1)
        assert direct == [("b", 0.2)]
        assert calls == [20, 2]
        assert result[0]["id"] == "b"

    def test_semantic_vector_pagination_uses_limit_plus_offset(self, search_db):
        conn, path = search_db
        calls = []

        class SemanticProxy:
            def enable_load_extension(self, enabled):
                return None

            def load_extension(self, path):
                return None

            def execute(self, sql, params=()):
                if "FROM entity_embeddings" in sql:
                    calls.append(params[1])
                    rows = [("a", 0.1), ("b", 0.2)]
                    page_limit, page_offset = params[-2:]
                    return type(
                        "Rows",
                        (),
                        {"fetchall": lambda self: rows[page_offset : page_offset + page_limit]},
                    )()
                return conn.execute(sql, params)

        proxy = SemanticProxy()
        with (
            patch.object(search_primitives, "_run_fts_search", return_value=([], False)),
            patch.object(search_primitives, "retrieval_vector_search", return_value=[]),
            patch.object(search_primitives, "get_connection", return_value=proxy),
            patch("saltmdb.db.connection.get_connection", return_value=proxy),
            patch("saltmdb.domain.services.memory_service.search_primitives.close_connection"),
            patch.object(embedding_service, "embed_query_text", return_value=[1.0] + [0.0] * 383),
        ):
            result = memory_service.search_memory(
                query_keywords="needle",
                cursor="offset:1",
                limit=1,
                include_related=False,
                db_connection=conn,
                db_path=path,
            )
            direct = search_primitives.semantic_search("needle", [], [], 1, path, offset=1)
        assert direct == [("b", 0.2)]
        assert calls == [20, 2]
        assert result[0]["id"] == "b"

    def test_collapse_fork_does_not_emit_a_synthetic_head(self, search_db):
        conn, path = search_db
        conn.execute(
            "INSERT INTO relations (id,source_id,target_id,predicate) VALUES "
            "('fork-1','c','b','supersedes'),('fork-2','c','a','supersedes')"
        )
        conn.commit()
        result = _search_case(conn, path, {"limit": 3, "collapse_supersedes_families": True})
        assert [row["id"] for row in result] == ["a", "c", "b"]

    def test_missing_topic_embedding_fallback_defaults_to_zero_and_different_topics(
        self, search_db
    ):
        _, path = search_db
        with (
            patch.object(search_primitives, "rerank_candidates_by_topic", return_value={}),
            patch.object(search_primitives, "_batch_semantic_similarities", return_value={}),
            patch.object(embedding_service, "embed_query_text", return_value=[1.0]),
        ):
            result = search_primitives._score_topics_with_fallback("needle", ["a"], path)
        assert result == {"a": {"topic_score": 0.0, "semantic_verdict": "DIFFERENT_TOPICS"}}

    def test_preview_chunk_count_upper_bound_is_respected(self, search_db):
        content = "".join(str(index) * 10 + ("x" * 60 if index < 4 else "") for index in range(5))
        starts = [index * 70 for index in range(5)]
        chunks = [(index, starts[index], starts[index] + 10, float(index)) for index in range(5)]
        with (
            patch.object(search_primitives, "RELEVANCE_PREVIEW_MAX_CHUNKS", 2),
            patch.object(search_primitives, "RELEVANCE_PREVIEW_MIN_CHUNKS", 1),
            patch.object(search_primitives, "RELEVANCE_PREVIEW_CHUNK_PERCENT", 0.8),
            patch.object(search_primitives, "RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS", 0),
        ):
            result = search_primitives._select_relevance_preview_text(content, chunks)
        assert result == "0000000000 ... 1111111111"

    def test_preview_chunk_count_lower_bound_is_respected(self, search_db):
        content = "".join(str(index) * 10 + ("x" * 60 if index < 4 else "") for index in range(5))
        starts = [index * 70 for index in range(5)]
        chunks = [(index, starts[index], starts[index] + 10, float(index)) for index in range(5)]
        with (
            patch.object(search_primitives, "RELEVANCE_PREVIEW_MAX_CHUNKS", 4),
            patch.object(search_primitives, "RELEVANCE_PREVIEW_MIN_CHUNKS", 2),
            patch.object(search_primitives, "RELEVANCE_PREVIEW_CHUNK_PERCENT", 0.2),
            patch.object(search_primitives, "RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS", 0),
        ):
            result = search_primitives._select_relevance_preview_text(content, chunks)
        assert result == "0000000000 ... 1111111111"

    def test_rrf_missing_score_default_is_zero(self, search_db):
        result = search_primitives.reciprocal_rank_fusion([("a", 0.0)], [("b", 0.0)], limit=2, k=60)
        assert result == {"a": 1 / 61, "b": 1 / 61}

    def test_dangling_supersession_sort_key_defaults_missing_entity(self, search_db):
        assert ranking._supersession_sort_key("deleted", {}) == ("", "", "deleted")
