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

from saltmdb.config import MEMORY_REF_MAX_CHARS
from saltmdb.domain.services import (
    core_governance_service,
    event_service,
    librarian_service,
    memory_service,
    relation_service,
    retrieve_context_service,
    telemetry_service,
    trace_service,
)
from saltmdb.domain.services.memory_service import Provenance, CoreDeclaration
from typing import Any, Literal

from saltmdb.utils import error_codes
from saltmdb.utils.envelope import error, rejected
from saltmdb.utils.pagination import cursor_offset

logger = logging.getLogger(__name__)


class _DispatchValidationError(Exception):
    """Carry a rejected envelope for caller-input validation failures.

    Dispatch helpers raise this local exception so each dispatcher can convert validation
    failures into a normal tool result. Unexpected service faults keep propagating.
    """

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload)
        self.payload: dict[str, Any] = payload


def _optional_bool(kw: dict[str, Any], key: str, default: bool) -> bool:
    value = kw.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} must be a boolean", key)])
        )
    return value


def _optional_int(
    kw: dict[str, Any],
    key: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = kw.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} must be an integer", key)])
        )
    if minimum is not None and value < minimum:
        raise _DispatchValidationError(
            rejected(
                [error(error_codes.VALIDATION_ERROR, f"{key} must be at least {minimum}", key)]
            )
        )
    if maximum is not None and value > maximum:
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} must be at most {maximum}", key)])
        )
    return value


def _optional_int_or_none(kw: dict[str, Any], key: str) -> int | None:
    value = kw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} must be an integer", key)])
        )
    return value


def _optional_float_or_none(kw: dict[str, Any], key: str) -> float | None:
    value = kw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} must be numeric", key)])
        )
    return float(value)


def _optional_scope(kw: dict[str, Any], key: str = "scope") -> Literal["private", "shared"]:
    value = kw.get(key)
    if value is None:
        return "shared"
    if value not in {"private", "shared"}:
        raise _DispatchValidationError(
            rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        f"{key} must be 'private' or 'shared'",
                        key,
                    )
                ]
            )
        )
    return value


def _optional_mode(kw: dict[str, Any]) -> Literal["strict", "broad", "history"]:
    value = kw.get("mode")
    if value is None:
        return "broad"
    if value not in {"strict", "broad", "history"}:
        raise _DispatchValidationError(
            rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        "mode must be 'strict', 'broad', or 'history'",
                        "mode",
                    )
                ]
            )
        )
    return value


def _optional_strategy(kw: dict[str, Any]) -> Literal["local", "global"]:
    value = kw.get("strategy")
    if value is None:
        return "local"
    if value not in {"local", "global"}:
        raise _DispatchValidationError(
            rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        "strategy must be 'local' or 'global'",
                        "strategy",
                    )
                ]
            )
        )
    return value


def _optional_direction(kw: dict[str, Any]) -> Literal["outbound", "inbound", "both"]:
    value = kw.get("direction")
    if value is None:
        return "both"
    if value not in {"outbound", "inbound", "both"}:
        raise _DispatchValidationError(
            rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        "direction must be 'outbound', 'inbound', or 'both'",
                        "direction",
                    )
                ]
            )
        )
    return value


def _optional_lineage_direction(kw: dict[str, Any]) -> Literal["ancestors", "descendants"]:
    value = kw.get("direction") or "ancestors"
    if value == "ancestors":
        return "ancestors"
    if value == "descendants":
        return "descendants"
    raise _DispatchValidationError(
        rejected(
            [
                error(
                    error_codes.VALIDATION_ERROR,
                    "direction must be 'ancestors' or 'descendants'",
                    "direction",
                )
            ]
        )
    )


def _optional_detail(kw: dict[str, Any]) -> Literal["full", "summary"]:
    value = kw.get("detail")
    if value is None:
        return "full"
    if value not in {"full", "summary"}:
        raise _DispatchValidationError(
            rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        "detail must be 'full' or 'summary'",
                        "detail",
                    )
                ]
            )
        )
    return value


def _required_str(kw: dict[str, Any], key: str) -> str:
    value = kw.get(key)
    if not isinstance(value, str) or not value:
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} is required", key)])
        )
    return value


def _required_str_list(kw: dict[str, Any], key: str) -> list[str]:
    value = kw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _DispatchValidationError(
            rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        f"{key} must be a list of strings",
                        key,
                    )
                ]
            )
        )
    return value


def _optional_tag_operator(kw: dict[str, Any]) -> Literal["AND", "OR"]:
    value = kw.get("tag_operator")
    if value is None:
        return "AND"
    if value not in {"AND", "OR"}:
        raise _DispatchValidationError(
            rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        "tag_operator must be 'AND' or 'OR'",
                        "tag_operator",
                    )
                ]
            )
        )
    return value


def _required_list(kw: dict[str, Any], key: str) -> list[Any]:
    value = kw.get(key)
    if not isinstance(value, list):
        raise _DispatchValidationError(
            rejected([error(error_codes.VALIDATION_ERROR, f"{key} must be a list", key)])
        )
    return value


def _dispatch_store_memory(**kw):
    retrieval_text_provided = kw.get("retrieval_text_provided", "retrieval_text" in kw)
    try:
        scope = _optional_scope(kw)
        weight = _optional_int(kw, "weight", 1)
    except _DispatchValidationError as exc:
        return exc.payload
    return memory_service.store_memory(
        content=kw.get("content"),
        tags=kw.get("tags"),
        agent_id=kw.get("agent_id"),
        scope=scope,
        weight=weight,
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
        probe_sink=kw.get("probe_sink"),
    )


def _dispatch_search_memory(**kw):
    try:
        explain_mode = _optional_bool(kw, "explain_mode", False)
        limit = _optional_int(kw, "limit", 5)
        tag_operator = _optional_tag_operator(kw)
        mode = _optional_mode(kw)
        include_related = _optional_bool(kw, "include_related", True)
        prefer_durable_types = _optional_bool(kw, "prefer_durable_types", False)
        demote_superseded = _optional_bool(kw, "demote_superseded", False)
        cross_encoder_candidate_cap = _optional_int_or_none(kw, "cross_encoder_candidate_cap")
        cross_encoder_text_cap_chars = _optional_int_or_none(kw, "cross_encoder_text_cap_chars")
        use_chunk_candidates = _optional_bool(kw, "use_chunk_candidates", False)
        oversampling_multiplier = _optional_int_or_none(kw, "oversampling_multiplier")
        candidate_window = _optional_int_or_none(kw, "candidate_window")
        chunk_weight = _optional_float_or_none(kw, "chunk_weight")
        collapse_supersedes_families = _optional_bool(kw, "collapse_supersedes_families", False)
        return_diagnostics = _optional_bool(kw, "return_diagnostics", False)
        use_retrieval_text_candidates = _optional_bool(kw, "use_retrieval_text_candidates", False)
        retrieval_fts_weight = _optional_float_or_none(kw, "retrieval_fts_weight")
        retrieval_vector_weight = _optional_float_or_none(kw, "retrieval_vector_weight")
    except _DispatchValidationError as exc:
        return exc.payload
    return memory_service.search_memory(
        agent_id=kw.get("agent_id"),
        query_keywords=kw.get("query_keywords"),
        tags_filter=kw.get("tags_filter"),
        metadata_filter=kw.get("metadata_filter"),
        explain_mode=explain_mode,
        limit=limit,
        context_id=kw.get("context_id"),
        agent_session_id=kw.get("agent_session_id"),
        is_core=kw.get("is_core"),
        memory_type_filter=kw.get("memory_type_filter"),
        tag_operator=tag_operator,
        cursor=kw.get("cursor"),
        mode=mode,
        include_related=include_related,
        prefer_durable_types=prefer_durable_types,
        demote_superseded=demote_superseded,
        cross_encoder_candidate_cap=cross_encoder_candidate_cap,
        cross_encoder_text_cap_chars=cross_encoder_text_cap_chars,
        use_chunk_candidates=use_chunk_candidates,
        oversampling_multiplier=oversampling_multiplier,
        candidate_window=candidate_window,
        chunk_weight=chunk_weight,
        collapse_supersedes_families=collapse_supersedes_families,
        return_diagnostics=return_diagnostics,
        disable_semantic=kw.get("disable_semantic", False),
        use_retrieval_text_candidates=use_retrieval_text_candidates,
        retrieval_fts_weight=retrieval_fts_weight,
        retrieval_vector_weight=retrieval_vector_weight,
    )


def _dispatch_get_memory(**kw):
    """Fetch one entity through the explicit-ID service contract, owner-scoped."""
    try:
        entity_id = _required_str(kw, "entity_id")
        detail = _optional_detail(kw)
        include_trace_provenance = _optional_bool(kw, "include_trace_provenance", False)
    except _DispatchValidationError as exc:
        return exc.payload
    service = memory_service.get_memory if detail == "full" else memory_service.inspect_memory
    return service(
        entity_id=entity_id,
        agent_id=kw.get("agent_id"),
        include_trace_provenance=include_trace_provenance,
    )


def _validate_memory_ids(memory_ids: list[str]) -> dict[str, Any] | None:
    if not 1 <= len(memory_ids) <= 20:
        return rejected(
            [
                error(
                    error_codes.VALIDATION_ERROR,
                    "memory_ids must contain between 1 and 20 ids",
                    "memory_ids",
                )
            ]
        )
    normalized_ids = [memory_id.strip() for memory_id in memory_ids]
    for index, memory_id in enumerate(normalized_ids):
        if not memory_id or len(memory_id) > MEMORY_REF_MAX_CHARS:
            return rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        f"memory_ids entries must be 1 to {MEMORY_REF_MAX_CHARS} characters after stripping",
                        f"memory_ids[{index}]",
                    )
                ]
            )
    if len(set(normalized_ids)) != len(normalized_ids):
        return rejected(
            [
                error(
                    error_codes.VALIDATION_ERROR,
                    "memory_ids must not contain duplicate ids",
                    "memory_ids",
                )
            ]
        )
    return None


def _apply_inline_budget(data: dict[str, Any], remaining: int) -> int:
    content = data.get("content")
    if not isinstance(content, str):
        return remaining
    from saltmdb.utils.text import large_content_descriptor

    descriptor = large_content_descriptor(content, threshold=remaining)
    if "content" not in descriptor:
        data.pop("content", None)
    data.update(descriptor)
    return remaining - len(content) if "content" in descriptor else remaining


def _dispatch_get_memories(**kw):
    try:
        memory_ids = _required_str_list(kw, "memory_ids")
        detail = _optional_detail(kw)
        include_trace_provenance = _optional_bool(kw, "include_trace_provenance", False)
    except _DispatchValidationError as exc:
        return exc.payload
    validation = _validate_memory_ids(memory_ids)
    if validation is not None:
        return validation
    from saltmdb.config import GET_MEMORIES_INLINE_BUDGET_CHARS, get_db_path
    from saltmdb.db.connection import close_connection, get_connection

    conn = get_connection(get_db_path())
    remaining = GET_MEMORIES_INLINE_BUDGET_CHARS
    service = memory_service.get_memory if detail == "full" else memory_service.inspect_memory
    items: list[dict[str, Any]] = []
    try:
        for requested_id in memory_ids:
            result = service(
                entity_id=requested_id,
                db_connection=conn,
                agent_id=kw.get("agent_id"),
                include_trace_provenance=include_trace_provenance,
            )
            if detail == "full" and result.get("status") == "ok":
                data = result.get("data")
                if isinstance(data, dict):
                    remaining = _apply_inline_budget(data, remaining)
            items.append({"requested_id": requested_id, "result": result})
    finally:
        close_connection(conn)
    return items


def _dispatch_archive_memory(**kw):
    # The adapter preserves the original list-vs-single shape through ``mode``.
    mode = kw.get("mode")
    if mode == "batch":
        try:
            archive_requests = _required_list(kw, "archive_requests")
        except _DispatchValidationError as exc:
            return exc.payload
        from saltmdb.domain.services.memory_service.lifecycle import archive_memories

        return archive_memories(
            archive_requests=archive_requests,
            agent_id=kw.get("agent_id"),
            coordinator=kw.get("coordinator"),
        )
    if mode == "bulk":
        try:
            archive_requests = _required_list(kw, "archive_requests")
        except _DispatchValidationError as exc:
            return exc.payload
        return memory_service.bulk_archive_memory(archive_requests=archive_requests)
    if mode == "single":
        return memory_service.archive_memory(
            entity_id=kw.get("entity_id"), agent_id=kw.get("agent_id")
        )
    return memory_service.archive_memory(entity_id=None, agent_id=kw.get("agent_id"))


def _dispatch_manage_relation(**kw):
    if kw.get("mode") == "batch":
        try:
            relations = _required_list(kw, "relations")
        except _DispatchValidationError as exc:
            return exc.payload
        return relation_service.apply_relations(
            relations=relations,
            agent_id=kw.get("agent_id"),
            coordinator=kw.get("coordinator"),
        )
    if kw.get("relations"):
        try:
            relations = _required_list(kw, "relations")
        except _DispatchValidationError as exc:
            return exc.payload
        return relation_service.bulk_store_relations(
            relations=relations,
            agent_id=kw.get("agent_id"),
            invalidate=bool(kw.get("invalidate")),
        )
    if kw.get("invalidate"):
        return relation_service.invalidate_relation(
            source_id=kw.get("source_id"),
            target_id=kw.get("target_id"),
            predicate=kw.get("predicate"),
            invalid_at=kw.get("invalid_at"),
            coordinator=kw.get("coordinator"),
        )
    return relation_service.store_relation(
        source_id=kw.get("source_id"),
        target_id=kw.get("target_id"),
        predicate=kw.get("predicate"),
        valid_at=kw.get("valid_at"),
        override_justification=kw.get("override_justification"),
        agent_id=kw.get("agent_id"),
        coordinator=kw.get("coordinator"),
    )


def _dispatch_replacement(**kw):
    """Dispatch an immutable lifecycle replacement to the DB-worker service."""
    try:
        required = ("entity_id", "title", "content", "reason")
        for key in required:
            _required_str(kw, key)
        tags = kw.get("tags")
        if tags is not None:
            tags = _required_str_list(kw, "tags")
    except _DispatchValidationError as exc:
        return exc.payload
    from saltmdb.domain.services.memory_service import CoreDeclaration, ReplacementRequest

    core_fields = (
        "is_core",
        "core_reason",
        "core_exit_condition",
        "core_review_after",
        "detail_memory_ids",
    )
    core = None
    if kw.get("demote_core"):
        core = CoreDeclaration(is_core=False)
    elif any(kw.get(field) is not None for field in core_fields):
        core = CoreDeclaration(
            is_core=kw.get("is_core"),
            core_reason=kw.get("core_reason"),
            core_exit_condition=kw.get("core_exit_condition"),
            core_review_after=kw.get("core_review_after"),
            detail_memory_ids=kw.get("detail_memory_ids"),
        )
    return memory_service.replace_memory(
        "revises" if kw.get("_service_name") == "revise_memory" else "supersedes",
        ReplacementRequest(
            entity_id=kw["entity_id"],
            title=kw["title"],
            content=kw["content"],
            tags=tags,
            reason=kw["reason"],
            context_id=kw.get("context_id"),
            scope=kw.get("scope"),
            memory_type=kw.get("memory_type"),
            retrieval_text=kw.get("retrieval_text"),
            core=core,
            repoint_relations=bool(kw.get("repoint_relations", False)),
            agent_id=kw.get("agent_id"),
            agent_session_id=kw.get("agent_session_id"),
        ),
    )


def _dispatch_revise_memory(**kw):
    return _dispatch_replacement(**{**kw, "_service_name": "revise_memory"})


def _dispatch_supersede_memory(**kw):
    return _dispatch_replacement(**{**kw, "_service_name": "supersede_memory"})


def _dispatch_consolidate_memories(**kw):
    if kw.get("items") is not None:
        return relation_service.apply_consolidations(
            items=kw["items"],
            agent_id=kw.get("agent_id"),
            context_id=kw.get("context_id"),
            agent_session_id=kw.get("agent_session_id"),
        )
    if kw.get("consolidations"):
        # The singular Phase-4 worker entry point is new; its bulk companion is still named
        # ``bulk_commit_consolidation`` until the relation-service migration lands.  Keep this
        # fallback local to the daemon adapter; neither spelling is a public MCP method.
        bulk = getattr(
            relation_service,
            "bulk_consolidate_memories",
            relation_service.bulk_commit_consolidation,
        )
        try:
            consolidations = _required_list(kw, "consolidations")
        except _DispatchValidationError as exc:
            return exc.payload
        return bulk(
            consolidations=consolidations,
            agent_id=kw.get("agent_id"),
            context_id=kw.get("context_id"),
            agent_session_id=kw.get("agent_session_id"),
        )
    try:
        parent_ids = _required_str_list(kw, "parent_ids")
        title = _required_str(kw, "title")
        content = _required_str(kw, "content")
        scope = _optional_scope(kw)
        weight = _optional_int(kw, "weight", 1)
    except _DispatchValidationError as exc:
        return exc.payload
    consolidate = getattr(relation_service, "consolidate_memories")
    return consolidate(
        parent_ids=parent_ids,
        title=title,
        content=content,
        tags=kw.get("tags"),
        scope=scope,
        weight=weight,
        override_justification=kw.get("override_justification"),
        provenance=Provenance(
            agent_id=kw.get("agent_id"),
            context_id=kw.get("context_id"),
            agent_session_id=kw.get("agent_session_id"),
        ),
        core=CoreDeclaration(
            is_core=kw.get("is_core"),
            core_reason=kw.get("core_reason"),
            core_exit_condition=kw.get("core_exit_condition"),
            core_review_after=kw.get("core_review_after"),
            detail_memory_ids=kw.get("detail_memory_ids"),
        ),
    )


# Private compatibility name for focused adapter tests and downstream daemon extensions.  It is
# intentionally absent from DISPATCH_TABLE and therefore is not a public MCP method.
_dispatch_commit_consolidation = _dispatch_consolidate_memories


def _dispatch_set_core_memory(**kw):
    try:
        entity_id = _required_str(kw, "entity_id")
        agent_id = _required_str(kw, "agent_id")
        core_reason = _required_str(kw, "core_reason")
        core_exit_condition = _required_str(kw, "core_exit_condition")
        detail_memory_ids = kw.get("detail_memory_ids")
        if detail_memory_ids is not None:
            detail_memory_ids = _required_str_list(kw, "detail_memory_ids")
        if kw.get("core_review_after") is not None:
            raise _DispatchValidationError(
                rejected(
                    [
                        error(
                            error_codes.VALIDATION_ERROR,
                            "the review date is set by review_core_memory",
                            "core_review_after",
                        )
                    ]
                )
            )
    except _DispatchValidationError as exc:
        return exc.payload
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection

    conn = get_connection(get_db_path())
    return core_governance_service.set_core_memory(
        conn,
        entity_id=entity_id,
        agent_id=agent_id,
        core_reason=core_reason,
        core_exit_condition=core_exit_condition,
        detail_memory_ids=detail_memory_ids,
    )


def _dispatch_review_core_memory(**kw):
    # Same pattern as every other DISPATCH_TABLE entry: no explicit connection is threaded
    # through kwargs -- the domain call's own get_connection() resolves to the ambient
    # coordinator-owned connection via connection.py's contextvar when running inside
    # coordinator.submit (see dispatch_tool's MUTATING_TOOLS branch below).
    try:
        entity_id = _required_str(kw, "entity_id")
        outcome = _required_str(kw, "outcome")
        review_rationale = _required_str(kw, "review_rationale")
        agent_id = _required_str(kw, "agent_id")
    except _DispatchValidationError as exc:
        return exc.payload
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection

    conn = get_connection(get_db_path())
    return core_governance_service.review_core_memory(
        conn,
        entity_id=entity_id,
        outcome=outcome,
        review_rationale=review_rationale,
        agent_id=agent_id,
        core_review_after=kw.get("core_review_after"),
    )


def _dispatch_update_memory_metadata(**kw):
    try:
        entity_id = _required_str(kw, "entity_id")
    except _DispatchValidationError as exc:
        return exc.payload
    return memory_service.update_memory_metadata(
        entity_id=entity_id,
        metadata=kw.get("metadata"),
        agent_id=kw.get("agent_id"),
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
    return session_digest_service.render_session_digest(
        conn, kw["cwd"], kw.get("max_chars"), before=kw.get("before")
    )


def _dispatch_find_related_memories(**kw):
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection
    from saltmdb.domain.services import related_memory_service

    db_path = get_db_path()
    conn = get_connection(db_path)
    return related_memory_service.find_related_memories(
        conn,
        db_path,
        kw["text"],
        limit=kw.get("limit"),
        min_score=kw.get("min_score"),
        exclude_ids=kw.get("exclude_ids"),
        agent_id=kw.get("agent_id"),
        with_all=kw.get("with_all", False),
    )


def _dispatch_get_lineage(**kw):
    try:
        entity_id = _required_str(kw, "entity_id")
        direction = _optional_lineage_direction(kw)
        max_depth = _optional_int(kw, "max_depth", 5)
    except _DispatchValidationError as exc:
        return exc.payload

    return relation_service.get_lineage(
        entity_id=entity_id,
        direction=direction,
        max_depth=max_depth,
        agent_id=kw.get("agent_id"),
    )


def _dispatch_get_related_memories(**kw):
    try:
        entity_id = _required_str(kw, "entity_id")
        max_depth = _optional_int(kw, "max_depth", 5)
        direction = _optional_direction(kw)
        related_kwargs = {
            "entity_id": entity_id,
            "max_depth": max_depth,
            "direction": direction,
            "agent_id": kw.get("agent_id"),
        }
        include_inspect = False
        if "include_inspect" in kw:
            include_inspect = _optional_bool(kw, "include_inspect", False)
            related_kwargs["include_inspect"] = include_inspect
    except _DispatchValidationError as exc:
        return exc.payload
    get_related = getattr(relation_service, "get_related_memories", None)
    if get_related is not None:
        return get_related(**related_kwargs)
    return relation_service.analyze_dependencies(
        root_entity_id=entity_id,
        max_depth=max_depth,
        direction=direction,
        include_inspect=include_inspect,
        agent_id=kw.get("agent_id"),
    )


def _dispatch_get_events(**kw):
    try:
        limit = _optional_int(kw, "limit", 20, 1, 101)
        offset = _optional_int(kw, "offset", 0, 0, 10000)
    except _DispatchValidationError as exc:
        return exc.payload
    return event_service.get_recent_events(
        context_id=kw.get("context_id"),
        agent_id=kw.get("agent_id"),
        type_filter=kw.get("event_type"),
        agent_session_id=kw.get("agent_session_id"),
        event_id=kw.get("event_id"),
        order=kw.get("order") or "newest_first",
        limit=limit,
        offset=offset,
        full_content=bool(kw.get("full_content")),
    )


def _dispatch_search_tags(**kw):
    try:
        limit = _optional_int(kw, "limit", 50, 1, 101)
        offset = _optional_int(kw, "offset", 0, 0, 10000)
    except _DispatchValidationError as exc:
        return exc.payload
    return memory_service.search_tags(domain=kw.get("domain"), limit=limit, offset=offset)


def _dispatch_list_predicates(**kw):
    try:
        limit = _optional_int(kw, "limit", 50, 1, 101)
        offset = _optional_int(kw, "offset", 0, 0, 10000)
    except _DispatchValidationError as exc:
        return exc.payload
    return relation_service.list_predicates(query=kw.get("query"), limit=limit, offset=offset)


def _dispatch_search_traces(**kw):
    try:
        limit = _optional_int(kw, "limit", 5, 1, 51)
        _, cursor_error = cursor_offset(kw.get("cursor"), 10000)
        if cursor_error is not None:
            raise _DispatchValidationError(
                rejected([error(error_codes.VALIDATION_ERROR, cursor_error, "cursor")])
            )
    except _DispatchValidationError as exc:
        return exc.payload
    return trace_service.search_traces(
        agent_session_id=kw.get("agent_session_id"),
        entity_id=kw.get("entity_id"),
        query_keywords=kw.get("query_keywords"),
        limit=limit,
        cursor=kw.get("cursor"),
        coordinator=kw.get("coordinator"),
    )


def _dispatch_retrieve_context(**kw):
    try:
        strategy = _optional_strategy(kw)
        budget_tokens = _optional_int_or_none(kw, "budget_tokens")
    except _DispatchValidationError as exc:
        return exc.payload
    if strategy == "global":
        if kw.get("entity_ids") is not None:
            return rejected(
                [
                    error(
                        error_codes.VALIDATION_ERROR,
                        'entity_ids is not valid for strategy="global"',
                        "entity_ids",
                    )
                ]
            )
        query = kw.get("query")
        if not isinstance(query, str):
            return rejected([error(error_codes.VALIDATION_ERROR, "query is required", "query")])
        return retrieve_context_service.assemble_retrieve_context(
            query=query,
            budget_tokens=budget_tokens,
            strategy="global",
            agent_id=kw.get("agent_id"),
        )
    entity_ids = kw.get("entity_ids")
    entity_ids_invalid = (
        not isinstance(entity_ids, list)
        or not entity_ids
        or not all(isinstance(item, str) for item in entity_ids)
    )
    if kw.get("query") is not None or entity_ids_invalid:
        if kw.get("query") is not None:
            field, message = "query", 'query is not valid for strategy="local"'
        else:
            field, message = (
                "entity_ids",
                "entity_ids is required and must be a non-empty list of strings",
            )
        return rejected([error(error_codes.VALIDATION_ERROR, message, field)])
    return retrieve_context_service.assemble_retrieve_context(
        entity_ids=entity_ids,
        budget_tokens=budget_tokens,
        strategy="local",
        agent_id=kw.get("agent_id"),
    )


DISPATCH_TABLE = {
    # One-liners
    "log_event": lambda **kw: event_service.log_event(**kw),
    "search_tags": _dispatch_search_tags,
    "list_predicates": _dispatch_list_predicates,
    "merge_tags": lambda **kw: librarian_service.merge_tags(**kw),
    # Multi-branch
    "store_memory": _dispatch_store_memory,
    "search_memory": _dispatch_search_memory,
    "get_memory": _dispatch_get_memory,
    "get_memories": _dispatch_get_memories,
    "archive_memory": _dispatch_archive_memory,
    "manage_relation": _dispatch_manage_relation,
    "revise_memory": _dispatch_revise_memory,
    "supersede_memory": _dispatch_supersede_memory,
    "consolidate_memories": _dispatch_consolidate_memories,
    "get_lineage": _dispatch_get_lineage,
    "get_related_memories": _dispatch_get_related_memories,
    "get_events": _dispatch_get_events,
    "retrieve_context": _dispatch_retrieve_context,
    "set_core_memory": _dispatch_set_core_memory,
    "review_core_memory": _dispatch_review_core_memory,
    "update_memory_metadata": _dispatch_update_memory_metadata,
    "get_core_bootstrap_digest": _dispatch_get_core_bootstrap_digest,
    "get_last_session_digest": _dispatch_get_last_session_digest,
    "find_related_memories": _dispatch_find_related_memories,
    "capture_trace_start": lambda **kw: trace_service.capture_trace_start(**kw),
    "capture_trace_memory_link": lambda **kw: trace_service.capture_trace_memory_link(**kw),
    "capture_trace_complete": lambda **kw: trace_service.capture_trace_complete(**kw),
    "search_traces": _dispatch_search_traces,
    "get_trace": lambda **kw: trace_service.get_trace(**kw),
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
        "set_core_memory",
        "update_memory_metadata",
        "capture_trace_start",
        "capture_trace_memory_link",
        "capture_trace_complete",
    }
)


def _dispatch_tool_inner(tool: str, kwargs: dict[str, Any], coordinator):
    fn = DISPATCH_TABLE[tool]
    if tool in MUTATING_TOOLS:
        if tool == "store_memory":
            sink: list[dict[str, Any]] = []
            store_kwargs: dict[str, Any] = {
                **kwargs,
                "coordinator": coordinator,
                "probe_sink": sink,
            }

            def store_job(_conn):
                sink.clear()
                return fn(**store_kwargs)

            result = coordinator.submit(
                f"tool:{tool}",
                store_job,
                priority="foreground",
            )
            if sink and isinstance(result, dict) and result.get("status") == "ok":
                try:
                    memory_service.apply_duplicate_probe(result, sink[0])
                except Exception:
                    logger.warning("Deferred store_memory duplicate probe failed", exc_info=True)
            return result
        if tool in {"log_event", "manage_relation", "archive_memory"}:
            kwargs = {**kwargs, "coordinator": coordinator}
        return coordinator.submit(f"tool:{tool}", lambda _conn: fn(**kwargs), priority="foreground")
    if tool in {"search_traces", "get_trace"}:
        kwargs = {**kwargs, "coordinator": coordinator}
    return fn(**kwargs)


def dispatch_tool(tool: str, kwargs: dict[str, Any], coordinator):
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
        # get_events' agent_id is a read filter, not the caller -- record no caller for it.
        agent_id = (
            kwargs.get("agent_id") if isinstance(kwargs, dict) and tool != "get_events" else None
        )
        param_names = list(kwargs.keys()) if isinstance(kwargs, dict) else []
        try:
            coordinator.submit(
                f"telemetry:{tool}",
                lambda conn: telemetry_service.record_call(
                    tool,
                    param_names,
                    status,
                    latency_ms,
                    agent_id=agent_id,
                    error_code=error_code,
                    db_connection=conn,
                ),
                priority="background",
                wait=False,
            )
        except Exception as e:  # noqa: BLE001 -- telemetry submission is best-effort, see above
            logger.warning("Could not submit telemetry write for tool '%s': %s", tool, e)
