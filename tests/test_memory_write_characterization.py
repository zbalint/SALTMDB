"""Literal write-path contracts captured before the MEMORY-3 extraction."""

from typing import Any
from unittest.mock import patch

import pytest

from saltmdb.db.schema import init_db
from saltmdb.domain.services import core_governance_service as governance
from saltmdb.domain.services.memory_service import write


BODY = "Administrative updates preserve the original memory identity and authoritative source."


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "write-characterization.db"))
    with patch("saltmdb.domain.services.librarian_service.trigger_librarian"):
        yield conn
    conn.close()


def store(db: Any, **overrides: Any) -> Any:
    return write.store_memory(
        **(
            {
                "entity_id": "fixed-memory",
                "title": "Storage policy",
                "content": BODY,
                "agent_id": "writer",
                "db_connection": db,
                "probe_sink": [],
            }
            | overrides
        )
    )


def success(*, tags=None, scope="shared", context=None, memory_type="fact", session=None):
    return {
        "status": "ok",
        "data": {
            "id": "fixed-memory",
            "message": "Knowledge stored successfully with ID: fixed-memory",
            "submitted_tags": [],
            "effective_tags": tags or [],
            "duplicate_candidates": [],
        },
        "warnings": [],
        "effective": {
            "agent_id": "writer",
            "context_id": context,
            "scope": scope,
            "memory_type": memory_type,
            "agent_session_id": session,
        },
    }


def snapshot(db):
    return {
        table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in ("entities", "entity_tags", "relations", "embedding_jobs", "tags")
    }


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {"agent_id": None},
            "Error: agent_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination.",
        ),
        ({"content": None}, "Error: content is mandatory and cannot be empty."),
        ({"content": "  "}, "Error: content is mandatory and cannot be empty."),
        ({"scope": "public"}, "Error: scope must be either 'private' or 'shared'"),
        (
            {"memory_type": "invalid"},
            "Error: memory_type must be one of 'fact', 'event', 'procedure', 'decision', 'preference'",
        ),
        ({"title": None}, "Error: title is mandatory and cannot be empty."),
        ({"title": "  "}, "Error: title is mandatory and cannot be empty."),
        (
            {"retrieval_text": None},
            "Error: retrieval_text JSON null is ambiguous; omit the field to preserve or use an empty string to clear.",
        ),
        ({"retrieval_text": 1}, "Error: retrieval_text must be a string"),
        (
            {"retrieval_text": "x" * 4001},
            "Error: retrieval_text exceeds the maximum length of 4000 Unicode characters",
        ),
    ],
)
def test_validation_returns_exact_error_without_writes(db, overrides, expected):
    before = snapshot(db)
    assert store(db, **overrides) == expected
    assert snapshot(db) == before


@pytest.mark.parametrize("size,accepted", [(86, True), (87, False)])
def test_content_limit_equality(db, size, accepted):
    with patch.object(write, "MAX_MEMORY_CONTENT_CHARS", 86):
        result = store(db, content=BODY + " " * (size - len(BODY)))
    if accepted:
        assert result == success()
    else:
        assert result == "Error: content exceeds 86 characters (got 87)."
        assert db.execute("SELECT id FROM entities").fetchall() == []


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"title": "Changed policy"}, "title"),
        ({"content": BODY + " Changed."}, "full_content"),
        ({"agent_id": "other"}, "agent_id"),
        ({"scope": "private"}, "scope"),
        ({"memory_type": "event"}, "memory_type"),
        ({"context_id": "other"}, "context_id"),
        ({"tags": ["#other"]}, "tags"),
    ],
)
def test_each_frozen_field_rejects_without_any_side_effect(db, overrides, field):
    assert store(db) == success()
    before = snapshot(db)
    assert store(db, **overrides) == {
        "status": "rejected",
        "errors": [
            {
                "code": "IMMUTABLE_MEMORY",
                "message": "Frozen field(s) cannot be changed by store_memory; use revise_memory or supersede_memory to create a new version: "
                + field,
                "field": field,
            }
        ],
        "warnings": [],
    }
    assert snapshot(db) == before


def test_frozen_guard_preserves_error_order(db):
    store(db)
    assert store(
        db,
        title="Changed title",
        content=BODY + " Changed.",
        agent_id="other",
        scope="private",
        memory_type="event",
        context_id="other",
        tags=["#other"],
    ) == {
        "status": "rejected",
        "errors": [
            {
                "code": "IMMUTABLE_MEMORY",
                "message": "Frozen field(s) cannot be changed by store_memory; use revise_memory or supersede_memory to create a new version: title, full_content, agent_id, scope, memory_type, context_id, tags",
                "field": "title",
            }
        ],
        "warnings": [],
    }


def test_private_update_preserves_omitted_frozen_values_and_metadata(db):
    store(
        db,
        scope="private",
        memory_type="decision",
        context_id="project",
        metadata={"old": 1},
        tags=["#original"],
    )
    before = db.execute("SELECT created_at,valid_from,content_hash FROM entities").fetchall()
    assert store(db, weight=5, metadata={"new": 2}) == success(
        tags=["#original"], scope="private", context="project", memory_type="decision"
    )
    assert db.execute(
        "SELECT weight,scope,memory_type,context_id,metadata FROM entities"
    ).fetchall() == [(5, "private", "decision", "project", '{"old": 1, "new": 2}')]
    assert (
        db.execute("SELECT created_at,valid_from,content_hash FROM entities").fetchall() == before
    )
    assert store(db) == success(
        tags=["#original"], scope="private", context="project", memory_type="decision"
    )
    assert db.execute("SELECT id FROM entities ORDER BY id").fetchall() == [("fixed-memory",)]


@pytest.mark.parametrize("raw,normalized", [("[]", "{}"), ("{}", "{}"), (None, None)])
def test_metadata_recovery_and_shallow_merge(db, raw, normalized):
    store(db)
    db.execute("UPDATE entities SET metadata=? WHERE id='fixed-memory'", (raw,))
    db.commit()
    assert store(db) == success()
    assert db.execute("SELECT metadata FROM entities").fetchone() == (normalized,)
    assert store(db, metadata={"replacement": 2}) == success()
    assert db.execute("SELECT metadata FROM entities").fetchone() == ('{"replacement": 2}',)


@pytest.mark.parametrize("key", ["project", "project_id"])
def test_metadata_context_fallback(db, key):
    assert store(db, metadata={key: "project"}) == success(context="project")
    assert db.execute("SELECT context_id FROM entities").fetchall() == [("project",)]


@pytest.mark.parametrize(
    "scores,expected",
    [
        ({"relevance": 0}, 2),
        ({"impact": 0}, 2),
        ({"novelty": 0}, 2),
        ({"actionability": 0}, 2),
        ({"relevance": 2, "impact": 3, "novelty": 3, "actionability": 3}, 2),
        ({"relevance": -20}, 1),
        ({"relevance": 100}, 5),
        ({"weight": 0}, 1),
    ],
)
def test_weight_defaults_floor_and_clamps(db, scores, expected):
    assert store(db, **scores) == success()
    assert db.execute("SELECT weight FROM entities").fetchall() == [(expected,)]


def test_retrieval_preserve_replace_clear_and_durable_jobs(db):
    assert store(db, retrieval_text="  ＡＢＣ  ") == success()
    assert db.execute("SELECT retrieval_text,retrieval_text_hash FROM entities").fetchall() == [
        ("ABC", "b5d4045c3f466fa91fe2cc6abe79232a1a57cdf104f7a26e716e0a1e2789df78")
    ]
    assert db.execute("SELECT source_hash,state FROM retrieval_embedding_jobs").fetchall() == [
        ("b5d4045c3f466fa91fe2cc6abe79232a1a57cdf104f7a26e716e0a1e2789df78", "queued")
    ]
    db.execute("UPDATE embedding_jobs SET state='succeeded'")
    db.execute("UPDATE retrieval_embedding_jobs SET state='succeeded'")
    db.commit()
    assert store(db) == success()
    assert db.execute("SELECT job_kind,state FROM embedding_jobs ORDER BY job_kind").fetchall() == [
        ("chunk", "succeeded"),
        ("entity", "succeeded"),
    ]
    assert db.execute("SELECT source_hash,state FROM retrieval_embedding_jobs").fetchall() == [
        ("b5d4045c3f466fa91fe2cc6abe79232a1a57cdf104f7a26e716e0a1e2789df78", "succeeded")
    ]
    assert store(db, retrieval_text="new") == success()
    assert db.execute("SELECT retrieval_text FROM entities").fetchall() == [("new",)]
    assert db.execute("SELECT job_kind,state FROM embedding_jobs ORDER BY job_kind").fetchall() == [
        ("chunk", "succeeded"),
        ("entity", "succeeded"),
    ]
    assert db.execute(
        "SELECT source_hash,state FROM retrieval_embedding_jobs ORDER BY source_hash"
    ).fetchall() == [
        ("11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437", "queued"),
        ("b5d4045c3f466fa91fe2cc6abe79232a1a57cdf104f7a26e716e0a1e2789df78", "succeeded"),
    ]
    assert store(db, retrieval_text="") == success()
    assert db.execute("SELECT retrieval_text,retrieval_text_hash FROM entities").fetchall() == [
        (None, None)
    ]
    assert db.execute(
        "SELECT source_hash,state FROM retrieval_embedding_jobs ORDER BY source_hash"
    ).fetchall() == [
        ("11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437", "cancelled"),
        ("b5d4045c3f466fa91fe2cc6abe79232a1a57cdf104f7a26e716e0a1e2789df78", "succeeded"),
    ]


def test_equal_tags_normalize_and_do_not_rewrite_identity(db):
    store(db, tags=["#original"])
    assert store(db, tags=["ORIGINAL", "#Original"]) == success(tags=["#original"])
    assert db.execute("SELECT id FROM entities").fetchall() == [("fixed-memory",)]


@pytest.mark.parametrize(
    "payload", ["concurrent-state", {"status": "rejected", "errors": [], "warnings": []}]
)
def test_transaction_governance_rejection_rolls_back(db, payload):
    original = governance.resolve_store_core_state
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise governance.CoreGovernanceRejected(payload)
        return original(*args, **kwargs)

    before = snapshot(db)
    with patch.object(governance, "resolve_store_core_state", side_effect=resolve):
        result = store(db)
    assert result == (payload if isinstance(payload, dict) else "Error: concurrent-state")
    assert snapshot(db) == before


def test_transaction_value_error_is_returned_without_writes(db):
    original = governance.resolve_store_core_state
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("transaction validation")
        return original(*args, **kwargs)

    before = snapshot(db)
    with patch.object(governance, "resolve_store_core_state", side_effect=resolve):
        assert store(db) == "Error: transaction validation"
    assert snapshot(db) == before


def test_internal_error_envelope(db):
    with patch.object(
        write, "evaluate_memory_quality", side_effect=RuntimeError("quality unavailable")
    ):
        assert store(db) == {
            "status": "rejected",
            "errors": [{"code": "INTERNAL_ERROR", "message": "quality unavailable"}],
            "warnings": [],
        }
    assert db.execute("SELECT id FROM entities").fetchall() == []


def test_quality_rejection_fallback_and_warning_envelope(db):
    with patch.object(
        write,
        "evaluate_memory_quality",
        return_value={"status": "REJECT", "hard_errors": [], "warnings": ["ADVISORY"]},
    ):
        assert store(db) == {
            "status": "rejected",
            "errors": [
                {
                    "code": "MEMORY_QUALITY_REJECTED",
                    "message": "Memory quality check rejected by MEMORY_QUALITY_REJECTED.",
                    "field": "content",
                }
            ],
            "warnings": [
                {
                    "code": "QUALITY_WARNING",
                    "message": "Advisory quality finding: ADVISORY.",
                    "detail": {"rule": "ADVISORY"},
                }
            ],
        }
    assert db.execute("SELECT id FROM entities").fetchall() == []


@pytest.mark.parametrize("overdue", [False, True])
def test_archived_target_core_promotion_ignores_active_governance_limits(db, overdue):
    assert store(db) == success()
    db.execute("UPDATE entities SET status='archived' WHERE id='fixed-memory'")
    db.commit()
    active = store(
        db,
        entity_id="active-core",
        title="Active incident",
        content="The active incident requires operators to inspect the bounded diagnostic report.",
        is_core=True,
        core_reason="An active incident requires immediate operator attention.",
        core_exit_condition="Remove this warning after the incident is resolved.",
    )
    assert active["status"] == "ok"
    if overdue:
        db.execute(
            "UPDATE entities SET core_review_after='2000-01-01T00:00:00+00:00' "
            "WHERE id='active-core'"
        )
        db.commit()
    with patch.object(governance, "CORE_MAX_ACTIVE", 1):
        assert (
            store(
                db,
                is_core=True,
                core_reason="This archived warning retains its historical governance.",
                core_exit_condition="Keep archived until its historical incident is resolved.",
            )
            == success()
        )
    assert db.execute("SELECT id,status,is_core FROM entities ORDER BY id").fetchall() == [
        ("active-core", "raw", 1),
        ("fixed-memory", "archived", 1),
    ]
    assert [row["id"] for row in governance.load_active_cores(db)] == ["active-core"]


def test_core_update_excludes_itself_from_capacity_admission(db):
    assert (
        store(
            db,
            is_core=True,
            core_reason="An active incident requires immediate operator attention.",
            core_exit_condition="Remove this warning after the incident is resolved.",
        )
        == success()
    )
    before = db.execute(
        "SELECT core_reason,core_exit_condition,core_review_after FROM entities"
    ).fetchall()
    with patch.object(governance, "CORE_MAX_ACTIVE", 1):
        assert store(db, weight=4) == success()
    assert db.execute("SELECT id,status,is_core,weight FROM entities").fetchall() == [
        ("fixed-memory", "raw", 1, 4)
    ]
    assert (
        db.execute(
            "SELECT core_reason,core_exit_condition,core_review_after FROM entities"
        ).fetchall()
        == before
    )


def test_preserving_retrieval_text_preserves_its_hash(db):
    assert store(db, retrieval_text="  ＡＢＣ  ") == success()
    original_hash = db.execute("SELECT retrieval_text_hash FROM entities").fetchone()[0]
    db.execute("UPDATE retrieval_embedding_jobs SET state='succeeded'")
    db.commit()
    assert store(db) == success()
    assert db.execute("SELECT retrieval_text_hash FROM entities").fetchone() == (original_hash,)


def test_identical_retrieval_text_force_requeues_embedding(db):
    assert store(db, retrieval_text="same retrieval text") == success()
    source_hash = db.execute("SELECT source_hash FROM retrieval_embedding_jobs").fetchone()[0]
    db.execute("UPDATE retrieval_embedding_jobs SET state='succeeded'")
    db.commit()
    assert store(db, retrieval_text="same retrieval text") == success()
    assert db.execute("SELECT source_hash,state FROM retrieval_embedding_jobs").fetchall() == [
        (source_hash, "queued")
    ]


def test_empty_agent_id_is_rejected_without_writes(db):
    before = snapshot(db)
    assert store(db, agent_id="") == (
        "Error: agent_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."
    )
    assert snapshot(db) == before


def test_partial_weight_dimensions_use_neutral_defaults(db):
    assert store(db, impact=2) == success()
    assert db.execute("SELECT weight FROM entities").fetchone() == (2,)


def test_metadata_project_precedes_project_id(db):
    assert store(db, metadata={"project": "primary", "project_id": "fallback"}) == success(
        context="primary"
    )
    assert db.execute("SELECT context_id FROM entities").fetchone() == ("primary",)


def test_whitespace_only_tags_are_ignored_on_update(db):
    assert store(db) == success()
    result = store(db, tags=["   "])
    assert result["status"] == "ok"
    assert result["data"]["effective_tags"] == []
    assert db.execute("SELECT * FROM entity_tags").fetchall() == []


def test_duplicate_spelling_tags_insert_once(db):
    result = store(db, tags=["#task", "#tasks"])
    assert result["status"] == "ok"
    assert result["data"]["effective_tags"] == ["#task"]
    assert db.execute(
        "SELECT t.name,COUNT(*) FROM entity_tags et JOIN tags t ON t.id=et.tag_id "
        "WHERE et.entity_id='fixed-memory' GROUP BY t.name"
    ).fetchall() == [("#task", 1)]


def test_tag_sanitization_advisory_event_keeps_writer_attribution(db):
    from saltmdb.domain.services import event_service

    with patch.object(event_service, "log_event") as log_event:
        result = store(db, tags=["#tag:value"])
    assert result["data"]["submitted_tags"] == ["#tag:value"]
    assert result["data"]["effective_tags"] == ["#tag-value"]
    assert result["warnings"] == [
        {
            "code": "TAG_NEAR_MISS",
            "message": "Submitted tag #tag:value resolved near #tag-value.",
            "detail": {
                "submitted": "#tag:value",
                "effective": "#tag-value",
                "similarity": 0.941,
            },
        }
    ]
    log_event.assert_called_once()
    assert log_event.call_args.kwargs["agent_id"] == "writer"
    assert log_event.call_args.kwargs["type"] == "issue"


def test_legacy_null_valid_from_uses_created_at_for_history(db):
    assert store(db) == success()
    created_at = db.execute("SELECT created_at FROM entities").fetchone()[0]
    db.execute("UPDATE entities SET valid_from=NULL WHERE id='fixed-memory'")
    db.commit()
    with patch.object(write, "_legacy_update_guard", return_value=(None, None)):
        assert store(db, weight=4) == success()
    assert db.execute(
        "SELECT valid_from FROM entities WHERE id LIKE 'fixed-memory_h_%'"
    ).fetchone() == (created_at,)
