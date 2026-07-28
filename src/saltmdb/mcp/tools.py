from typing import Literal
import json
from saltmdb.mcp.server import mcp
from saltmdb.domain.services import (
    event_service,
    memory_service,
    relation_service,
    ephemeral_service,
    librarian_service
)

def _normalize_list_or_str(val) -> list:
    """Helper to convert stringified lists, comma-separated strings, or single string values into a Python list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        val_str = val.strip()
        if val_str.startswith("[") and val_str.endswith("]"):
            try:
                parsed = json.loads(val_str)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        if "," in val_str:
            return [s.strip() for s in val_str.split(",") if s.strip()]
        return [val_str]
    return [val]

def _unwrap_kwargs(kwargs: dict) -> dict:
    """FastMCP emits a required 'kwargs' schema field for bare **kwargs params; some clients
    nest their actual payload under it. Unwrap that nested dict when present, else use kwargs as-is."""
    return kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs

def _resolve(explicit, kw: dict, raw_kwargs: dict, *aliases: str):
    """Resolve a parameter value: explicit arg wins, then each alias checked against
    the unwrapped kwargs dict, then against the raw kwargs dict, first alias wins within each."""
    if explicit is not None:
        return explicit
    for source in (kw, raw_kwargs):
        for alias in aliases:
            val = source.get(alias)
            if val is not None:
                return val
    return None

@mcp.tool()
def log_event(agent_id: str = None, type: str = None, content: str = None, error_code: str = None, session_id: str = None, context_id: str = None, **kwargs) -> str:
    """Appends an event to the append-only events ledger."""
    kw = _unwrap_kwargs(kwargs)
    agent_id_ = _resolve(agent_id, kw, kwargs, "agent_id", "agent") or "system"
    type_ = _resolve(type, kw, kwargs, "type", "event_type") or "event"
    content_ = _resolve(content, kw, kwargs, "content", "message", "description") or ""
    error_code_ = _resolve(error_code, kw, kwargs, "error_code")
    session_id_ = _resolve(session_id, kw, kwargs, "session_id")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id", "project")
    return event_service.log_event(agent_id=agent_id_, type=type_, content=content_, error_code=error_code_, session_id=session_id_, context_id=context_id_)

@mcp.tool()
def get_canonical_tags(query: str = None, domain: str = None, **kwargs) -> list:
    """Queries the database to suggest existing canonical tags matching a search query/substring, to prevent tag fragmentation. Use query='auth' to filter by tag name substring."""
    kw = _unwrap_kwargs(kwargs)
    query_ = query or domain or _resolve(None, kw, kwargs, "query", "domain", "substring", "tag_filter")
    return memory_service.get_canonical_tags(domain=query_)

@mcp.tool()
def get_canonical_predicates(query: str = None, **kwargs) -> list:
    """Queries existing canonical relation predicates matching a search substring, to reduce
    predicate drift (e.g. elaborates_on vs relates_to vs references)."""
    kw = _unwrap_kwargs(kwargs)
    query_ = _resolve(query, kw, kwargs, "query", "predicate_filter", "substring")
    return relation_service.get_canonical_predicates(query=query_)

@mcp.tool()
def merge_tags(keep_tag: str = None, tags_to_merge: list = None, **kwargs) -> str:
    """Merges one or more fragmented/synonym tags into an explicitly chosen canonical tag, repointing all
    affected entities' tag associations. Use to fix folksonomy fragmentation (e.g. keep_tag='#docs',
    tags_to_merge=['#doc', '#documentation'])."""
    kw = _unwrap_kwargs(kwargs)
    keep_tag_ = _resolve(keep_tag, kw, kwargs, "keep_tag", "canonical_tag", "keep")
    raw_merge = tags_to_merge if tags_to_merge is not None else _resolve(None, kw, kwargs, "tags_to_merge", "merge_tags", "aliases")
    tags_to_merge_ = _normalize_list_or_str(raw_merge)
    return librarian_service.merge_tags(keep_tag=keep_tag_, tags_to_merge=tags_to_merge_)

@mcp.tool()
def store_memory(
    content: str = None,
    title: str = None,
    tags: list = None,
    is_core: bool = None,
    memory_type: Literal['fact', 'event', 'procedure', 'decision', 'preference'] = None,
    owner_id: str = None,
    context_id: str = None,
    scope: Literal['private', 'shared'] = "shared",
    check_duplicates_only: bool = False,
    **kwargs
) -> str | dict:
    """Stores a consolidated Markdown fact chunk as long-term memory.

    memory_type classifies the memory into one of five fixed kinds (default 'fact' on a new
    memory; omitting it on an update preserves the existing value, same as is_core):
      - fact: semantic, durable, generalized knowledge.
      - event: episodic, something that happened, ideally timestamped.
      - procedure: how-to / runbook / skill.
      - decision: ADR-style rationale record (what was chosen and why).
      - preference: durable user/agent preference statement.

    If check_duplicates_only is True, returns duplicate detection results without writing to the database.
    """
    kw = _unwrap_kwargs(kwargs)
    content_ = _resolve(content, kw, kwargs, "content", "text") or ""
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id", "context", "project")
    title_ = _resolve(title, kw, kwargs, "title")

    raw_tag = tags if tags is not None else _resolve(None, kw, kwargs, "tags", "tag")
    tags_ = _normalize_list_or_str(raw_tag) if raw_tag is not None else None

    raw_is_core = is_core if is_core is not None else _resolve(None, kw, kwargs, "is_core")
    if raw_is_core is not None:
        is_core_ = raw_is_core in (True, 1, "true", "1", "True")
    else:
        is_core_ = None

    memory_type_ = _resolve(memory_type, kw, kwargs, "memory_type", "type", "kind")

    if check_duplicates_only or kw.get("check_duplicates_only"):
        return memory_service.check_duplicate_memories(title=title_, content=content_, owner_id=owner_id_, tags=tags_, context_id=context_id_)

    entity_id = _resolve(None, kw, kwargs, "entity_id", "id")
    weight = kwargs.get("weight", 1)
    relevance = _resolve(None, kw, kwargs, "relevance")
    impact = _resolve(None, kw, kwargs, "impact")
    novelty = _resolve(None, kw, kwargs, "novelty")
    actionability = _resolve(None, kw, kwargs, "actionability")
    metadata = _resolve(None, kw, kwargs, "metadata")
    skip_duplicate_check = kwargs.get("skip_duplicate_check", False) or kw.get("skip_duplicate_check", False)

    return memory_service.store_memory(
        content=content_,
        tags=tags_,
        owner_id=owner_id_,
        scope=scope,
        weight=weight,
        is_core=is_core_,
        memory_type=memory_type_,
        title=title_,
        entity_id=entity_id,
        relevance=relevance,
        impact=impact,
        novelty=novelty,
        actionability=actionability,
        metadata=metadata,
        skip_duplicate_check=skip_duplicate_check,
        context_id=context_id_
    )

@mcp.tool()
def search_memory(
    owner_id: str = None,
    query_keywords: str = None,
    tags_filter: list = None,
    entity_id: str = None,
    fetch_full: bool = False,
    limit: int = 5,
    context_id: str = None,
    is_core: bool = None,
    memory_type_filter: Literal['fact', 'event', 'procedure', 'decision', 'preference'] = None,
    cursor: str = None,
    include_related: bool = True,
    **kwargs
) -> list | dict | str:
    """Performs full-text keyword & dense vector hybrid search in long-term memory.

    If entity_id or fetch_full is specified, retrieves full Markdown text chunk directly.

    memory_type_filter optionally restricts results to one of the five fixed memory_type
    values ('fact', 'event', 'procedure', 'decision', 'preference'); every result item also
    echoes its 'memory_type'.
    """
    kw = _unwrap_kwargs(kwargs)
    entity_id_ = _resolve(entity_id, kw, kwargs, "entity_id", "id")
    if entity_id_ or fetch_full or kw.get("fetch_full"):
        if entity_id_:
            return memory_service.fetch_memory_chunk(entity_id=entity_id_)

    query_keywords_ = _resolve(query_keywords, kw, kwargs, "query_keywords", "query", "q", "keywords")
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id", "context", "project")
    raw_tags = tags_filter or _resolve(None, kw, kwargs, "tags_filter", "tags")
    tags_filter_ = _normalize_list_or_str(raw_tags) if raw_tags else None
    metadata_filter_ = _resolve(None, kw, kwargs, "metadata_filter")
    explain_mode = kwargs.get("explain_mode", False) or kw.get("explain_mode", False)
    tag_operator = kwargs.get("tag_operator", "AND") or kw.get("tag_operator", "AND")
    memory_type_filter_ = _resolve(memory_type_filter, kw, kwargs, "memory_type_filter", "memory_type", "type_filter")

    return memory_service.search_memory(
        owner_id=owner_id_,
        query_keywords=query_keywords_,
        tags_filter=tags_filter_,
        metadata_filter=metadata_filter_,
        explain_mode=explain_mode,
        limit=limit,
        context_id=context_id_,
        is_core=is_core,
        memory_type_filter=memory_type_filter_,
        tag_operator=tag_operator,
        cursor=cursor,
        include_related=include_related
    )

@mcp.tool()
def ephemeral_memory(
    action: Literal['get', 'store'] = "get",
    key: str = None,
    value: str = None,
    **kwargs
) -> str:
    """Manages volatile in-memory secret storage (get or store)."""
    kw = _unwrap_kwargs(kwargs)
    action_ = _resolve(action, kw, kwargs, "action") or "get"
    key_ = _resolve(key, kw, kwargs, "key")
    value_ = _resolve(value, kw, kwargs, "value")

    if action_ == "store" or value_ is not None:
        return ephemeral_service.store_ephemeral_memory(key=key_, value=value_)
    return ephemeral_service.get_ephemeral_memory(key=key_)

@mcp.tool()
def archive_memory(entity_id: str | list[str] = None, owner_id: str = None, **kwargs) -> str | list:
    """Explicitly archives (retires) one or multiple long-term memories.

    Accepts entity_id as a single string ID OR a list of string IDs.
    """
    kw = _unwrap_kwargs(kwargs)
    raw_target = _resolve(entity_id, kw, kwargs, "entity_id", "archive_requests", "id")
    target = _normalize_list_or_str(raw_target)
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")

    if len(target) > 1 or (isinstance(raw_target, list) and len(target) > 0):
        return memory_service.bulk_archive_memory(archive_requests=target)
    elif len(target) == 1:
        return memory_service.archive_memory(entity_id=target[0], owner_id=owner_id_)
    return memory_service.archive_memory(entity_id=None, owner_id=owner_id_)

@mcp.tool()
def manage_relation(
    relations: list = None,
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    **kwargs
) -> str | list:
    """Stores one or multiple directional semantic relationship edges between memory nodes."""
    kw = _unwrap_kwargs(kwargs)
    relations_ = _resolve(relations, kw, kwargs, "relations")
    if relations_:
        if isinstance(relations_, str):
            relations_ = _normalize_list_or_str(relations_)
        return relation_service.bulk_store_relations(relations=relations_)

    source_id_ = _resolve(source_id, kw, kwargs, "source_id", "source")
    target_id_ = _resolve(target_id, kw, kwargs, "target_id", "target")
    predicate_ = _resolve(predicate, kw, kwargs, "predicate", "relation")
    return relation_service.store_relation(source_id=source_id_, target_id=target_id_, predicate=predicate_)

@mcp.tool()
def commit_consolidation(
    consolidations: list = None,
    parent_ids: list = None,
    title: str = None,
    content: str = None,
    tags: list = None,
    owner_id: str = None,
    context_id: str = None,
    **kwargs
) -> str | list:
    """Commits single or multiple consolidated memories, archiving raw parents and creating lineage edges."""
    kw = _unwrap_kwargs(kwargs)
    consolidations_ = _resolve(consolidations, kw, kwargs, "consolidations")
    if consolidations_:
        if isinstance(consolidations_, str):
            consolidations_ = _normalize_list_or_str(consolidations_)
        return relation_service.bulk_commit_consolidation(consolidations=consolidations_)

    raw_parents = _resolve(parent_ids, kw, kwargs, "parent_ids")
    parent_ids_ = _normalize_list_or_str(raw_parents)
    title_ = _resolve(title, kw, kwargs, "title")
    content_ = _resolve(content, kw, kwargs, "content", "text")
    raw_tags = _resolve(tags, kw, kwargs, "tags")
    tags_ = _normalize_list_or_str(raw_tags)
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id")
    scope = kwargs.get("scope", "shared")
    weight = kwargs.get("weight", 1)

    return relation_service.commit_consolidation(
        parent_ids=parent_ids_, title=title_, content=content_, tags=tags_,
        scope=scope, weight=weight, owner_id=owner_id_, context_id=context_id_
    )

@mcp.tool()
def inspect_graph(
    entity_id: str | None = None,
    mode: Literal['dependencies', 'lineage', 'orphans'] = "dependencies",
    max_depth: int = 5,
    owner_id: str = None,
    **kwargs
) -> dict:
    """Inspects memory graph structure (dependencies, consolidation lineage, or orphaned nodes).

    entity_id is optional when mode='orphans'.
    """
    kw = _unwrap_kwargs(kwargs)
    entity_id_ = _resolve(entity_id, kw, kwargs, "entity_id", "root_entity_id", "root_id", "id")
    mode_ = _resolve(mode, kw, kwargs, "mode") or "dependencies"
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")

    if mode_ == "lineage":
        return relation_service.analyze_lineage(entity_id=entity_id_)
    elif mode_ == "orphans":
        return memory_service.detect_orphaned_memories(owner_id=owner_id_)
    else:
        max_depth_ = _resolve(max_depth, kw, kwargs, "max_depth") or 5
        return relation_service.analyze_dependencies(root_entity_id=entity_id_, max_depth=max_depth_)

@mcp.tool()
def get_events(
    agent_id: str = None,
    type_filter: str = None,
    session_id: str = None,
    limit: int = 20,
    offset: int = 0,
    status_filter: str = None,
    owner_id: str = None,
    mode: Literal['events', 'session', 'memories'] = "events",
    **kwargs
) -> list:
    """Retrieves operational events, session summary events, or scans memory logs."""
    kw = _unwrap_kwargs(kwargs)
    mode_ = _resolve(mode, kw, kwargs, "mode") or "events"
    session_id_ = _resolve(session_id, kw, kwargs, "session_id")

    if session_id_ or mode_ == "session":
        return event_service.get_session_summary(session_id=session_id_)
    elif mode_ == "memories":
        owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
        status_filter_ = _resolve(status_filter, kw, kwargs, "status_filter")
        return memory_service.scan_memories(owner_id=owner_id_, status_filter=status_filter_, limit=limit, offset=offset)
    else:
        agent_id_ = _resolve(agent_id, kw, kwargs, "agent_id", "agent")
        type_filter_ = _resolve(type_filter, kw, kwargs, "type_filter", "type")
        return event_service.get_recent_events(agent_id=agent_id_, type_filter=type_filter_, limit=limit)
