"""Server-side per-tool dispatch table (daemon-owned; the daemon's RPC handler and
DirectDispatchBackend, mcp/tools.py, both call into this module).

One function per MCP tool, taking ALREADY-NORMALIZED kwargs (alias resolution/defaulting already
happened adapter-side in mcp/tools.py). For the 5 true one-liners this is a trivial forward. For
the 7 multi-branch tools, this reproduces that tool's entire post-normalization branch structure
verbatim -- see scratch/plans/track_b_daemon_detailed.md §8/§9 for the full design and the exact
per-tool branch condition each of these mirrors, verified against mcp/tools.py's real code across
Codex review rounds 3-4.

This is the only module, alongside daemon/server.py and viewer/routes.py (which runs in-daemon as
a thread), that imports domain.services.* in the new architecture's steady state -- the
process-scoped DB-access-boundary invariant, not a module-scoped one (round-2 correction).
ephemeral_memory is deliberately absent from DISPATCH_TABLE -- it never goes over RPC (§8's
exemption; EPHEMERAL_CONN is a separate in-memory-only connection that never touches the
persistent DB, so it was never in scope for this boundary to begin with).
"""

import logging

from saltmdb.domain.services import (
    core_governance_service,
    event_service,
    librarian_service,
    memory_service,
    relation_service,
    telemetry_service,
)
from typing import Any, Literal

logger = logging.getLogger(__name__)


def _optional_bool(kw: dict[str, Any], key: str, default: bool) -> bool:
    value = kw.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _optional_int(kw: dict[str, Any], key: str, default: int) -> int:
    value = kw.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_int_or_none(kw: dict[str, Any], key: str) -> int | None:
    value = kw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_float_or_none(kw: dict[str, Any], key: str) -> float | None:
    value = kw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def _optional_scope(kw: dict[str, Any], key: str = "scope") -> Literal["private", "shared"]:
    value = kw.get(key)
    if value is None:
        return "shared"
    if value not in {"private", "shared"}:
        raise ValueError(f"{key} must be 'private' or 'shared'")
    return value


def _optional_mode(kw: dict[str, Any]) -> Literal["strict", "broad", "history"]:
    value = kw.get("mode")
    if value is None:
        return "broad"
    if value not in {"strict", "broad", "history"}:
        raise ValueError("mode must be 'strict', 'broad', or 'history'")
    return value


def _optional_direction(kw: dict[str, Any]) -> Literal["outbound", "inbound", "both"]:
    value = kw.get("direction")
    if value is None:
        return "both"
    if value not in {"outbound", "inbound", "both"}:
        raise ValueError("direction must be 'outbound', 'inbound', or 'both'")
    return value


def _required_str(kw: dict[str, Any], key: str) -> str:
    value = kw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _required_str_list(kw: dict[str, Any], key: str) -> list[str]:
    value = kw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


def _optional_tag_operator(kw: dict[str, Any]) -> Literal["AND", "OR"]:
    value = kw.get("tag_operator")
    if value is None:
        return "AND"
    if value not in {"AND", "OR"}:
        raise ValueError("tag_operator must be 'AND' or 'OR'")
    return value


def _required_list(kw: dict[str, Any], key: str) -> list[Any]:
    value = kw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _dispatch_store_memory(**kw):
    retrieval_text_provided = kw.get("retrieval_text_provided", "retrieval_text" in kw)
    return memory_service.store_memory(
        content=kw.get("content"),
        tags=kw.get("tags"),
        owner_id=kw.get("owner_id"),
        scope=_optional_scope(kw),
        weight=_optional_int(kw, "weight", 1),
        is_core=kw.get("is_core"),
        memory_type=kw.get("memory_type"),
        title=kw.get("title"),
        entity_id=kw.get("entity_id"),
        relevance=kw.get("relevance"),
        impact=kw.get("impact"),
        novelty=kw.get("novelty"),
        actionability=kw.get("actionability"),
        metadata=kw.get("metadata"),
        context_id=kw.get("context_id"),
        agent_session_id=kw.get("agent_session_id"),
        retrieval_text=(
            kw.get("retrieval_text")
            if retrieval_text_provided
            else memory_service.RETRIEVAL_TEXT_UNSET
        ),
        coordinator=kw.get("coordinator"),
        core_reason=kw.get("core_reason"),
        core_exit_condition=kw.get("core_exit_condition"),
        core_review_after=kw.get("core_review_after"),
        detail_memory_ids=kw.get("detail_memory_ids"),
    )


def _dispatch_search_memory(**kw):
    return memory_service.search_memory(
        owner_id=kw.get("owner_id"),
        query_keywords=kw.get("query_keywords"),
        tags_filter=kw.get("tags_filter"),
        metadata_filter=kw.get("metadata_filter"),
        explain_mode=_optional_bool(kw, "explain_mode", False),
        limit=_optional_int(kw, "limit", 5),
        context_id=kw.get("context_id"),
        agent_session_id=kw.get("agent_session_id"),
        is_core=kw.get("is_core"),
        memory_type_filter=kw.get("memory_type_filter"),
        tag_operator=_optional_tag_operator(kw),
        cursor=kw.get("cursor"),
        mode=_optional_mode(kw),
        include_related=_optional_bool(kw, "include_related", True),
        prefer_durable_types=_optional_bool(kw, "prefer_durable_types", False),
        demote_superseded=_optional_bool(kw, "demote_superseded", False),
        cross_encoder_candidate_cap=_optional_int_or_none(kw, "cross_encoder_candidate_cap"),
        cross_encoder_text_cap_chars=_optional_int_or_none(kw, "cross_encoder_text_cap_chars"),
        use_chunk_candidates=_optional_bool(kw, "use_chunk_candidates", False),
        oversampling_multiplier=_optional_int_or_none(kw, "oversampling_multiplier"),
        candidate_window=_optional_int_or_none(kw, "candidate_window"),
        chunk_weight=_optional_float_or_none(kw, "chunk_weight"),
        collapse_supersedes_families=_optional_bool(kw, "collapse_supersedes_families", False),
        return_diagnostics=_optional_bool(kw, "return_diagnostics", False),
        disable_semantic=kw.get("disable_semantic", False),
        use_retrieval_text_candidates=_optional_bool(kw, "use_retrieval_text_candidates", False),
        retrieval_fts_weight=_optional_float_or_none(kw, "retrieval_fts_weight"),
        retrieval_vector_weight=_optional_float_or_none(kw, "retrieval_vector_weight"),
    )


def _dispatch_get_memory(**kw):
    """Fetch one entity through the explicit-ID service contract.

    ``get_memory`` is introduced by Phase 3.  Keep a narrow fallback to the existing fetch
    primitive while the domain service is migrated, so the adapter/daemon surface can land
    independently of the service implementation.  The fallback is deliberately not a redirect:
    ``fetch_memory_chunk`` addresses exactly the supplied entity and includes archived rows.
    """
    entity_id = _required_str(kw, "entity_id")
    fetch = getattr(memory_service, "get_memory", None)
    if fetch is not None:
        return fetch(entity_id=entity_id)
    return memory_service.fetch_memory_chunk(entity_id=entity_id)


def _dispatch_inspect_memory(**kw):
    entity_id = _required_str(kw, "entity_id")
    return memory_service.inspect_memory(entity_id=entity_id)


def _dispatch_archive_memory(**kw):
    # archive_memory's bulk/single/none decision depends on the ORIGINAL request shape (did the
    # caller pass a list, even a 1-item one?), which is pre-normalization information -- mcp/
    # tools.py resolves that into an explicit "mode" tag before calling the backend, a narrow,
    # documented exception to the "dispatch.py owns branching" split (self-caught during
    # implementation, not from a Codex review round -- logged as an implementation-grounding
    # addendum per standing practice).
    mode = kw.get("mode")
    if mode == "bulk":
        return memory_service.bulk_archive_memory(
            archive_requests=_required_list(kw, "archive_requests")
        )
    elif mode == "single":
        return memory_service.archive_memory(
            entity_id=kw.get("entity_id"), owner_id=kw.get("owner_id")
        )
    return memory_service.archive_memory(entity_id=None, owner_id=kw.get("owner_id"))


def _dispatch_manage_relation(**kw):
    if kw.get("relations"):
        return relation_service.bulk_store_relations(
            relations=_required_list(kw, "relations"),
            owner_id=kw.get("owner_id"),
            invalidate=bool(kw.get("invalidate")),
        )
    if kw.get("invalidate"):
        return relation_service.invalidate_relation(
            source_id=kw.get("source_id"),
            target_id=kw.get("target_id"),
            predicate=kw.get("predicate"),
            invalid_at=kw.get("invalid_at"),
        )
    return relation_service.store_relation(
        source_id=kw.get("source_id"),
        target_id=kw.get("target_id"),
        predicate=kw.get("predicate"),
        valid_at=kw.get("valid_at"),
        override_justification=kw.get("override_justification"),
        owner_id=kw.get("owner_id"),
    )


def _dispatch_replacement(**kw):
    """Dispatch an immutable lifecycle replacement to the DB-worker service.

    The lifecycle services own validation, successor lookup, archival, and edge creation.  The
    daemon only enforces the typed adapter contract before entering the coordinator transaction.
    """
    required = ("entity_id", "title", "content", "reason")
    for key in required:
        _required_str(kw, key)
    tags = _required_str_list(kw, "tags")
    if not tags:
        raise ValueError("tags is required")
    service_name = kw.pop("_service_name")
    service = getattr(memory_service, service_name)
    return service(
        entity_id=kw["entity_id"],
        title=kw["title"],
        content=kw["content"],
        tags=tags,
        reason=kw["reason"],
        owner_id=kw.get("owner_id"),
        context_id=kw.get("context_id"),
        scope=kw.get("scope"),
        memory_type=kw.get("memory_type"),
        agent_session_id=kw.get("agent_session_id"),
    )


def _dispatch_revise_memory(**kw):
    return _dispatch_replacement(**{**kw, "_service_name": "revise_memory"})


def _dispatch_supersede_memory(**kw):
    return _dispatch_replacement(**{**kw, "_service_name": "supersede_memory"})


def _dispatch_consolidate_memories(**kw):
    if kw.get("consolidations"):
        # The singular Phase-4 worker entry point is new; its bulk companion is still named
        # ``bulk_commit_consolidation`` until the relation-service migration lands.  Keep this
        # fallback local to the daemon adapter; neither spelling is a public MCP method.
        bulk = getattr(
            relation_service,
            "bulk_consolidate_memories",
            relation_service.bulk_commit_consolidation,
        )
        return bulk(
            consolidations=_required_list(kw, "consolidations"),
            owner_id=kw.get("owner_id"),
            context_id=kw.get("context_id"),
            agent_session_id=kw.get("agent_session_id"),
        )
    consolidate = getattr(relation_service, "consolidate_memories")
    return consolidate(
        parent_ids=_required_str_list(kw, "parent_ids"),
        title=_required_str(kw, "title"),
        content=_required_str(kw, "content"),
        is_core=kw.get("is_core"),
        tags=kw.get("tags"),
        scope=_optional_scope(kw),
        weight=_optional_int(kw, "weight", 1),
        owner_id=kw.get("owner_id"),
        context_id=kw.get("context_id"),
        agent_session_id=kw.get("agent_session_id"),
        override_justification=kw.get("override_justification"),
        core_reason=kw.get("core_reason"),
        core_exit_condition=kw.get("core_exit_condition"),
        core_review_after=kw.get("core_review_after"),
        detail_memory_ids=kw.get("detail_memory_ids"),
    )


# Private compatibility name for focused adapter tests and downstream daemon extensions.  It is
# intentionally absent from DISPATCH_TABLE and therefore is not a public MCP method.
_dispatch_commit_consolidation = _dispatch_consolidate_memories


def _dispatch_review_core_memory(**kw):
    # Same pattern as every other DISPATCH_TABLE entry: no explicit connection is threaded
    # through kwargs -- the domain call's own get_connection() resolves to the ambient
    # coordinator-owned connection via connection.py's contextvar when running inside
    # coordinator.submit (see dispatch_tool's MUTATING_TOOLS branch below).
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection

    conn = get_connection(get_db_path())
    return core_governance_service.review_core_memory(
        conn,
        entity_id=_required_str(kw, "entity_id"),
        outcome=_required_str(kw, "outcome"),
        review_rationale=_required_str(kw, "review_rationale"),
        owner_id=_required_str(kw, "owner_id"),
        core_review_after=kw.get("core_review_after"),
    )


def _dispatch_update_memory_metadata(**kw):
    return memory_service.update_memory_metadata(
        entity_id=_required_str(kw, "entity_id"),
        metadata=kw.get("metadata"),
        agent_session_id=kw.get("agent_session_id"),
    )


def _dispatch_get_core_bootstrap_digest(**kw):
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection

    conn = get_connection(get_db_path())
    return core_governance_service.render_bootstrap_response(conn)


def _dispatch_get_last_session_digest(**kw):
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection
    from saltmdb.domain.services import session_digest_service

    conn = get_connection(get_db_path())
    return session_digest_service.render_last_session_digest(conn, kw["cwd"])


def _dispatch_get_lineage(**kw):
    entity_id = _required_str(kw, "entity_id")
    direction = kw.get("direction") or "ancestors"
    if direction not in {"ancestors", "descendants"}:
        raise ValueError("direction must be 'ancestors' or 'descendants'")
    max_depth = _optional_int(kw, "max_depth", 5)

    # The Phase-3 service entry point supports both directions and all lifecycle predicates.  The
    # fallback preserves ancestor behaviour against the pre-Phase-3 service while development is
    # split across the daemon and domain layers.
    get_lineage = getattr(relation_service, "get_lineage", None)
    if get_lineage is not None:
        return get_lineage(entity_id=entity_id, direction=direction, max_depth=max_depth)
    if direction == "descendants":
        raise RuntimeError("descendant lineage is unavailable until the lineage service is updated")
    return relation_service.analyze_lineage(entity_id=entity_id)


def _dispatch_get_related_memories(**kw):
    entity_id = _required_str(kw, "entity_id")
    max_depth = _optional_int(kw, "max_depth", 5)
    direction = _optional_direction(kw)
    related_kwargs = {
        "entity_id": entity_id,
        "max_depth": max_depth,
        "direction": direction,
    }
    if "include_inspect" in kw:
        related_kwargs["include_inspect"] = _optional_bool(kw, "include_inspect", False)
    get_related = getattr(relation_service, "get_related_memories", None)
    if get_related is not None:
        return get_related(**related_kwargs)
    return relation_service.analyze_dependencies(
        root_entity_id=entity_id,
        max_depth=max_depth,
        direction=direction,
        **(
            {"include_inspect": related_kwargs["include_inspect"]}
            if "include_inspect" in related_kwargs
            else {}
        ),
    )


def _dispatch_get_events(**kw):
    return event_service.get_recent_events(
        context_id=kw.get("context_id"),
        agent_id=kw.get("agent_id"),
        type_filter=kw.get("event_type"),
        agent_session_id=kw.get("agent_session_id"),
        order=kw.get("order") or "newest_first",
        limit=kw.get("limit") or 20,
        offset=kw.get("offset") or 0,
    )


DISPATCH_TABLE = {
    # One-liners
    "log_event": lambda **kw: event_service.log_event(**kw),
    "search_tags": lambda **kw: memory_service.search_tags(**kw),
    "list_predicates": lambda **kw: relation_service.list_predicates(**kw),
    "merge_tags": lambda **kw: librarian_service.merge_tags(**kw),
    # Multi-branch
    "store_memory": _dispatch_store_memory,
    "search_memory": _dispatch_search_memory,
    "get_memory": _dispatch_get_memory,
    "inspect_memory": _dispatch_inspect_memory,
    "archive_memory": _dispatch_archive_memory,
    "manage_relation": _dispatch_manage_relation,
    "revise_memory": _dispatch_revise_memory,
    "supersede_memory": _dispatch_supersede_memory,
    "consolidate_memories": _dispatch_consolidate_memories,
    "get_lineage": _dispatch_get_lineage,
    "get_related_memories": _dispatch_get_related_memories,
    "get_events": _dispatch_get_events,
    "review_core_memory": _dispatch_review_core_memory,
    "update_memory_metadata": _dispatch_update_memory_metadata,
    "get_core_bootstrap_digest": _dispatch_get_core_bootstrap_digest,
    "get_last_session_digest": _dispatch_get_last_session_digest,
}

# Tool calls that can mutate persistent state.  The daemon server calls these
# through ``dispatch_tool`` so even legacy service implementations execute on
# the coordinator-owned SQLite connection.
MUTATING_TOOLS = frozenset(
    {
        "log_event",
        "merge_tags",
        "store_memory",
        "archive_memory",
        "manage_relation",
        "revise_memory",
        "supersede_memory",
        "consolidate_memories",
        "review_core_memory",
        "update_memory_metadata",
    }
)


def _dispatch_tool_inner(tool: str, kwargs: dict, coordinator):
    fn = DISPATCH_TABLE[tool]
    if tool in MUTATING_TOOLS:
        if tool in {"store_memory", "log_event"}:
            kwargs = {**kwargs, "coordinator": coordinator}
        return coordinator.submit(f"tool:{tool}", lambda _conn: fn(**kwargs), priority="foreground")
    return fn(**kwargs)


def dispatch_tool(tool: str, kwargs: dict, coordinator):
    """Invoke one normalized tool with the daemon's single-writer boundary.

    Wrapped with best-effort usage telemetry (agent API redesign plan §5.9): timing and outcome
    are recorded via a background, non-blocking coordinator submission AFTER the real call
    completes or raises -- telemetry must never add latency to, or change the outcome of, the
    request it describes. This is the only call site of _dispatch_tool_inner; it exists solely
    so this wrapper has an inner function to time, not as a public seam.
    """
    timer = telemetry_service.Timer()
    result = None
    raised: BaseException | None = None
    try:
        result = _dispatch_tool_inner(tool, kwargs, coordinator)
        return result
    except BaseException as exc:
        raised = exc
        raise
    finally:
        status, error_code = telemetry_service.classify_result(result, raised)
        latency_ms = timer.elapsed_ms()
        owner_id = kwargs.get("owner_id") if isinstance(kwargs, dict) else None
        param_names = list(kwargs.keys()) if isinstance(kwargs, dict) else []
        try:
            coordinator.submit(
                f"telemetry:{tool}",
                lambda conn: telemetry_service.record_call(
                    tool,
                    param_names,
                    status,
                    latency_ms,
                    owner_id=owner_id,
                    error_code=error_code,
                    db_connection=conn,
                ),
                priority="background",
                wait=False,
            )
        except Exception as e:  # noqa: BLE001 -- telemetry submission is best-effort, see above
            logger.warning("Could not submit telemetry write for tool '%s': %s", tool, e)
