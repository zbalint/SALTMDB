import inspect
from unittest.mock import patch

import pytest

from saltmdb.daemon import dispatch
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    Provenance,
    lifecycle,
    revise_memory,
    store_memory,
    supersede_memory,
)


@pytest.fixture
def replacement_db(tmp_path, monkeypatch):
    path = str(tmp_path / "provenance.db")
    monkeypatch.setenv("SALTMDB_DB_PATH", path)
    conn = init_db(path)
    yield conn
    conn.close()


def _seed(
    conn,
    *,
    title="Provenance predecessor",
    agent_id="original-owner",
    context_id="original-context",
    agent_session_id=None,
):
    result = store_memory(
        title=title,
        content=f"A complete predecessor memory for provenance coverage: {title}.",
        tags=["#provenance"],
        agent_id=agent_id,
        context_id=context_id,
        agent_session_id=agent_session_id,
        db_connection=conn,
    )
    assert result["status"] == "ok"
    return result["data"]["id"]


def _replace(service, conn, old_id, *, provenance=None, title="Provenance replacement"):
    return service(
        entity_id=old_id,
        title=title,
        content="A complete replacement memory with enough detail for lifecycle provenance tests.",
        tags=["#replacement"],
        reason="Record provenance behavior.",
        provenance=provenance,
        db_connection=conn,
    )


def test_provenance_defaults_is_frozen_and_hashable():
    provenance = Provenance()
    assert provenance == Provenance(agent_id=None, context_id=None, agent_session_id=None)
    assert hash(provenance) == hash((None, None, None))
    with pytest.raises((AttributeError, TypeError)):
        setattr(provenance, "agent_id", "changed")


def test_replacement_provenance_values_are_stored(replacement_db):
    expected = ("replacement-owner", "replacement-context", "replacement-session")
    for service in (revise_memory, supersede_memory):
        old_id = _seed(replacement_db, title=f"{service.__name__} predecessor")
        result = _replace(
            service,
            replacement_db,
            old_id,
            provenance=Provenance(
                agent_id=expected[0], context_id=expected[1], agent_session_id=expected[2]
            ),
            title=f"{service.__name__} replacement",
        )
        assert result["status"] == "ok"
        assert (
            replacement_db.execute(
                "SELECT agent_id, context_id, agent_session_id FROM entities WHERE id = ?",
                (result["data"]["new_id"],),
            ).fetchone()
            == expected
        )


@pytest.mark.parametrize(
    "service",
    [revise_memory, supersede_memory],
    ids=["revise_memory", "supersede_memory"],
)
def test_omitted_provenance_inherits_agent_and_context_only(replacement_db, service):
    old_id = _seed(
        replacement_db,
        agent_id="predecessor-owner",
        context_id="predecessor-context",
        agent_session_id="predecessor-session",
    )
    omitted = _replace(service, replacement_db, old_id)
    assert omitted["status"] == "ok"
    assert replacement_db.execute(
        "SELECT agent_id, context_id, agent_session_id FROM entities WHERE id = ?",
        (omitted["data"]["new_id"],),
    ).fetchone() == ("predecessor-owner", "predecessor-context", None)


def test_partial_provenance_inherits_unset_agent_context_fields(replacement_db):
    old_id = _seed(
        replacement_db,
        title="Partial provenance predecessor",
        agent_id="predecessor-owner",
        context_id="predecessor-context",
        agent_session_id="predecessor-session",
    )
    partial = _replace(
        supersede_memory,
        replacement_db,
        old_id,
        provenance=Provenance(agent_id="replacement-owner"),
        title="Partial provenance replacement",
    )
    assert partial["status"] == "ok"
    assert replacement_db.execute(
        "SELECT agent_id, context_id, agent_session_id FROM entities WHERE id = ?",
        (partial["data"]["new_id"],),
    ).fetchone() == ("replacement-owner", "predecessor-context", None)


def test_public_signatures_reject_old_keywords_and_keep_parameter_limit():
    assert len(inspect.signature(revise_memory).parameters) <= 13
    assert len(inspect.signature(supersede_memory).parameters) <= 13
    with pytest.raises(TypeError):
        getattr(lifecycle, "revise_memory")(**{"agent_id": "old-keyword"})
    with pytest.raises(TypeError):
        getattr(lifecycle, "supersede_memory")(**{"agent_id": "old-keyword"})
    with pytest.raises(TypeError):
        getattr(lifecycle, "revise_memory")(*([None] * 12 + [Provenance()]))
    with pytest.raises(TypeError):
        getattr(lifecycle, "supersede_memory")(*([None] * 11 + [Provenance()]))


@pytest.mark.parametrize(
    ("service_name", "title"),
    [
        ("revise_memory", "Dispatcher revise replacement"),
        ("supersede_memory", "Dispatcher supersede replacement"),
    ],
)
def test_dispatcher_round_trip_preserves_all_provenance_fields(replacement_db, service_name, title):
    old_id = _seed(replacement_db, title=f"{title} predecessor")
    dispatcher = (
        dispatch._dispatch_revise_memory
        if service_name == "revise_memory"
        else dispatch._dispatch_supersede_memory
    )
    with (
        patch("saltmdb.domain.services.embedding_service.cancel_embedding_jobs_for_entity"),
        patch("saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity"),
        patch(
            "saltmdb.domain.services.embedding_service.cancel_retrieval_embedding_jobs_for_entity"
        ),
        patch("saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity"),
        patch(
            "saltmdb.domain.services.embedding_service.enqueue_retrieval_embedding_job_for_entity"
        ),
    ):
        result = dispatcher(
            entity_id=old_id,
            title=title,
            content="A complete dispatcher replacement memory with enough descriptive detail.",
            tags=["#dispatcher"],
            reason="Round-trip provenance coverage.",
            agent_id="dispatcher-agent",
            context_id="dispatcher-context",
            agent_session_id="dispatcher-session",
        )
    assert result["status"] == "ok"
    assert replacement_db.execute(
        "SELECT agent_id, context_id, agent_session_id FROM entities WHERE id = ?",
        (result["data"]["new_id"],),
    ).fetchone() == ("dispatcher-agent", "dispatcher-context", "dispatcher-session")
