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
        scope=kw.get("scope"),
        weight=kw.get("weight"),
        is_core=kw.get("is_core"),
        memory_type=kw.get("memory_type"),
        title=kw.get("title"),
        entity_id=kw.get("entity_id"),
        relevance=kw.get("relevance"),
        impact=kw.get("impact"),
        novelty=kw.get("novelty"),
        actionability=kw.get("actionability"),
        metadata=kw.get("metadata"),
        skip_duplicate_check=kw.get("skip_duplicate_check"),
        context_id=kw.get("context_id"),
        review_token=kw.get("review_token"),
        dispositions=kw.get("dispositions"),
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
        explain_mode=kw.get("explain_mode"),
        limit=kw.get("limit"),
        context_id=kw.get("context_id"),
        is_core=kw.get("is_core"),
        memory_type_filter=kw.get("memory_type_filter"),
        tag_operator=kw.get("tag_operator"),
        cursor=kw.get("cursor"),
        mode=kw.get("mode"),
        include_related=kw.get("include_related"),
        rerank_by_topic=kw.get("rerank_by_topic"),
        prefer_durable_types=kw.get("prefer_durable_types"),
        demote_superseded=kw.get("demote_superseded"),
        use_cross_encoder=kw.get("use_cross_encoder"),
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
        return memory_service.bulk_archive_memory(archive_requests=kw.get("archive_requests"))
    elif mode == "single":
        return memory_service.archive_memory(entity_id=kw.get("entity_id"), owner_id=kw.get("owner_id"))
    return memory_service.archive_memory(entity_id=None, owner_id=kw.get("owner_id"))


def _dispatch_manage_relation(**kw):
    if kw.get("relations"):
        return relation_service.bulk_store_relations(relations=kw.get("relations"))
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
        return relation_service.bulk_commit_consolidation(consolidations=kw.get("consolidations"))
    return relation_service.commit_consolidation(
        parent_ids=kw.get("parent_ids"),
        title=kw.get("title"),
        content=kw.get("content"),
        is_core=kw.get("is_core"),
        tags=kw.get("tags"),
        scope=kw.get("scope"),
        weight=kw.get("weight"),
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
        return event_service.get_session_summary(session_id=session_id)
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
        kw.get("event_ids"), kw.get("reason"), kw.get("agent_id")
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
