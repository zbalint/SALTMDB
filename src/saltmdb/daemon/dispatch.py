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

from saltmdb.domain.services import (
    event_service,
    librarian_service,
    memory_service,
    relation_service,
)
from typing import Any, Literal


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


def _required_str_or_list(kw: dict[str, Any], key: str) -> str | list[str]:
    value = kw.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError(f"{key} must be a string or list of strings")


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
    if kw.get("check_duplicates_only"):
        return memory_service.check_duplicate_memories(
            title=kw.get("title"),
            content=kw.get("content"),
            owner_id=kw.get("owner_id"),
            tags=kw.get("tags"),
            context_id=kw.get("context_id"),
        )
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
        skip_duplicate_check=_optional_bool(kw, "skip_duplicate_check", False),
        context_id=kw.get("context_id"),
        review_token=kw.get("review_token"),
        dispositions=kw.get("dispositions"),
        coordinator=kw.get("coordinator"),
    )


def _dispatch_search_memory(**kw):
    # Round-4 precision fix: only entity_id presence routes to fetch_memory_chunk. fetch_full=True
    # with no entity_id falls through to the normal search_memory call, unchanged.
    entity_id = kw.get("entity_id")
    if entity_id:
        return memory_service.fetch_memory_chunk(entity_id=entity_id)
    return memory_service.search_memory(
        owner_id=kw.get("owner_id"),
        query_keywords=kw.get("query_keywords"),
        tags_filter=kw.get("tags_filter"),
        metadata_filter=kw.get("metadata_filter"),
        explain_mode=_optional_bool(kw, "explain_mode", False),
        limit=_optional_int(kw, "limit", 5),
        context_id=kw.get("context_id"),
        is_core=kw.get("is_core"),
        memory_type_filter=kw.get("memory_type_filter"),
        tag_operator=_optional_tag_operator(kw),
        cursor=kw.get("cursor"),
        mode=_optional_mode(kw),
        include_related=_optional_bool(kw, "include_related", True),
        rerank_by_topic=_optional_bool(kw, "rerank_by_topic", False),
        prefer_durable_types=_optional_bool(kw, "prefer_durable_types", False),
        demote_superseded=_optional_bool(kw, "demote_superseded", False),
        use_cross_encoder=_optional_bool(kw, "use_cross_encoder", False),
        disable_semantic=kw.get("disable_semantic", False),
    )


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
        return relation_service.bulk_store_relations(relations=_required_list(kw, "relations"))
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


def _dispatch_commit_consolidation(**kw):
    if kw.get("consolidations"):
        return relation_service.bulk_commit_consolidation(
            consolidations=_required_list(kw, "consolidations")
        )
    return relation_service.commit_consolidation(
        parent_ids=_required_str_list(kw, "parent_ids"),
        title=_required_str(kw, "title"),
        content=_required_str(kw, "content"),
        is_core=kw.get("is_core"),
        tags=kw.get("tags"),
        scope=_optional_scope(kw),
        weight=_optional_int(kw, "weight", 1),
        owner_id=kw.get("owner_id"),
        context_id=kw.get("context_id"),
        override_justification=kw.get("override_justification"),
    )


def _dispatch_inspect_graph(**kw):
    mode = kw.get("mode") or "dependencies"
    if mode == "lineage":
        return relation_service.analyze_lineage(
            entity_id=kw.get("entity_id"), point_in_time=kw.get("point_in_time")
        )
    elif mode == "orphans":
        return memory_service.detect_orphaned_memories(owner_id=kw.get("owner_id"))
    else:
        return relation_service.analyze_dependencies(
            root_entity_id=kw.get("entity_id"),
            max_depth=kw.get("max_depth") or 5,
            point_in_time=kw.get("point_in_time"),
        )


def _dispatch_get_events(**kw):
    mode = kw.get("mode") or "events"
    session_id = kw.get("session_id")
    if session_id or mode == "session":
        return event_service.get_session_summary(session_id=_required_str(kw, "session_id"))
    elif mode == "memories":
        return memory_service.scan_memories(
            owner_id=kw.get("owner_id"),
            status_filter=kw.get("status_filter"),
            limit=kw.get("limit") or 20,
            offset=kw.get("offset") or 0,
        )
    else:
        return event_service.get_recent_events(
            agent_id=kw.get("agent_id"),
            type_filter=kw.get("type_filter"),
            limit=kw.get("limit") or 20,
            offset=kw.get("offset") or 0,
            status_filter=kw.get("status_filter"),
        )


DISPATCH_TABLE = {
    # One-liners
    "log_event": lambda **kw: event_service.log_event(**kw),
    "get_canonical_tags": lambda **kw: memory_service.get_canonical_tags(**kw),
    "get_canonical_predicates": lambda **kw: relation_service.get_canonical_predicates(**kw),
    "merge_tags": lambda **kw: librarian_service.merge_tags(**kw),
    "dismiss_event": lambda **kw: event_service.dismiss_events(
        _required_str_or_list(kw, "event_ids"),
        _required_str(kw, "reason"),
        kw.get("agent_id") or "system",
    ),
    # Multi-branch
    "store_memory": _dispatch_store_memory,
    "search_memory": _dispatch_search_memory,
    "archive_memory": _dispatch_archive_memory,
    "manage_relation": _dispatch_manage_relation,
    "commit_consolidation": _dispatch_commit_consolidation,
    "inspect_graph": _dispatch_inspect_graph,
    "get_events": _dispatch_get_events,
}

# Tool calls that can mutate persistent state.  The daemon server calls these
# through ``dispatch_tool`` so even legacy service implementations execute on
# the coordinator-owned SQLite connection.
MUTATING_TOOLS = frozenset(
    {
        "log_event",
        "merge_tags",
        "dismiss_event",
        "store_memory",
        "archive_memory",
        "manage_relation",
        "commit_consolidation",
    }
)


def dispatch_tool(tool: str, kwargs: dict, coordinator):
    """Invoke one normalized tool with the daemon's single-writer boundary."""
    fn = DISPATCH_TABLE[tool]
    if tool in MUTATING_TOOLS:
        if tool in {"store_memory", "log_event"}:
            kwargs = {**kwargs, "coordinator": coordinator}
        return coordinator.submit(f"tool:{tool}", lambda _conn: fn(**kwargs), priority="foreground")
    if tool == "search_memory" and kwargs.get("entity_id"):
        content = memory_service.fetch_memory_chunk(entity_id=kwargs["entity_id"], touch=False)
        coordinator.submit(
            "touch_memory_access",
            lambda conn: memory_service.touch_memory_access(kwargs["entity_id"], conn),
            priority="foreground",
        )
        return content
    return fn(**kwargs)
