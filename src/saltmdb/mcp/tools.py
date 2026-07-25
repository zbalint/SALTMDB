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

@mcp.tool()
def log_event(agent_id: str = None, type: str = None, content: str = None, error_code: str = None, session_id: str = None, context_id: str = None, **kwargs) -> str:
    """Appends an event to the append-only events ledger."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    agent_id_ = agent_id or kw.get("agent_id") or kw.get("agent") or kwargs.get("agent_id") or kwargs.get("agent") or "system"
    type_ = type or kw.get("type") or kw.get("event_type") or kwargs.get("type") or kwargs.get("event_type") or "event"
    content_ = content or kw.get("content") or kw.get("message") or kw.get("description") or kwargs.get("content") or kwargs.get("message") or kwargs.get("description") or ""
    error_code_ = error_code or kw.get("error_code") or kwargs.get("error_code")
    session_id_ = session_id or kw.get("session_id") or kwargs.get("session_id")
    context_id_ = context_id or kw.get("context_id") or kw.get("project_id") or kw.get("project") or kwargs.get("context_id") or kwargs.get("project_id") or kwargs.get("project")
    return event_service.log_event(agent_id=agent_id_, type=type_, content=content_, error_code=error_code_, session_id=session_id_, context_id=context_id_)

@mcp.tool()
def get_canonical_tags(query: str = None, domain: str = None, **kwargs) -> list:
    """Queries the database to suggest existing canonical tags matching a search query/substring, to prevent tag fragmentation. Use query='auth' to filter by tag name substring."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    query_ = query or domain or kw.get("query") or kw.get("domain") or kw.get("substring") or kw.get("tag_filter") or kwargs.get("query") or kwargs.get("domain") or kwargs.get("substring") or kwargs.get("tag_filter")
    return memory_service.get_canonical_tags(domain=query_)

@mcp.tool()
def store_memory(
    content: str = None,
    title: str = None,
    tags: list = None,
    is_core: bool = None,
    owner_id: str = None,
    context_id: str = None,
    scope: Literal['private', 'shared'] = "shared",
    check_duplicates_only: bool = False,
    **kwargs
) -> str | dict:
    """Stores a consolidated Markdown fact chunk as long-term memory.
    
    If check_duplicates_only is True, returns duplicate detection results without writing to the database.
    """
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    content_ = content or kw.get("content") or kw.get("text") or kwargs.get("content") or kwargs.get("text") or ""
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    project_id = kwargs.get("project_id")
    context_id_ = context_id or project_id or kw.get("context_id") or kw.get("project_id") or kw.get("context") or kw.get("project") or kwargs.get("context_id") or kwargs.get("project_id") or kwargs.get("context") or kwargs.get("project")
    project_id_ = context_id_
    title_ = title or kw.get("title") or kwargs.get("title")

    raw_tag = tags if tags is not None else (kw.get("tags") or kw.get("tag") or kwargs.get("tags") or kwargs.get("tag"))
    tags_ = _normalize_list_or_str(raw_tag)

    raw_is_core = is_core if is_core is not None else (kw.get("is_core") if "is_core" in kw else kwargs.get("is_core"))
    if raw_is_core is not None:
        is_core_ = raw_is_core in (True, 1, "true", "1", "True")
    else:
        is_core_ = None

    if check_duplicates_only or kw.get("check_duplicates_only"):
        return memory_service.check_duplicate_memories(title=title_, content=content_, owner_id=owner_id_, tags=tags_, project_id=project_id_)

    entity_id = kwargs.get("entity_id") or kw.get("entity_id") or kw.get("id")
    weight = kwargs.get("weight", 1)
    relevance = kwargs.get("relevance") or kw.get("relevance")
    impact = kwargs.get("impact") or kw.get("impact")
    novelty = kwargs.get("novelty") or kw.get("novelty")
    actionability = kwargs.get("actionability") or kw.get("actionability")
    metadata = kwargs.get("metadata") or kw.get("metadata")
    skip_duplicate_check = kwargs.get("skip_duplicate_check", False) or kw.get("skip_duplicate_check", False)

    return memory_service.store_memory(
        content=content_,
        tags=tags_,
        owner_id=owner_id_,
        scope=scope,
        weight=weight,
        is_core=is_core_,
        title=title_,
        entity_id=entity_id,
        relevance=relevance,
        impact=impact,
        novelty=novelty,
        actionability=actionability,
        metadata=metadata,
        skip_duplicate_check=skip_duplicate_check,
        project_id=project_id_,
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
    cursor: str = None,
    include_related: bool = True,
    **kwargs
) -> list | dict | str:
    """Performs full-text keyword & dense vector hybrid search in long-term memory.
    
    If entity_id or fetch_full is specified, retrieves full Markdown text chunk directly.
    """
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    entity_id_ = entity_id or kw.get("entity_id") or kw.get("id") or kwargs.get("entity_id") or kwargs.get("id")
    if entity_id_ or fetch_full or kw.get("fetch_full"):
        if entity_id_:
            return memory_service.fetch_memory_chunk(entity_id=entity_id_)

    query_keywords_ = (
        query_keywords or kwargs.get("query") or kwargs.get("q") or kwargs.get("keywords")
        or kw.get("query_keywords") or kw.get("query") or kw.get("q") or kw.get("keywords")
    )
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    project_id = kwargs.get("project_id")
    context_id_ = context_id or project_id or kw.get("context_id") or kw.get("project_id") or kw.get("context") or kw.get("project") or kwargs.get("context_id") or kwargs.get("project_id") or kwargs.get("context") or kwargs.get("project")
    raw_tags = tags_filter or kw.get("tags_filter") or kw.get("tags") or kwargs.get("tags_filter") or kwargs.get("tags")
    tags_filter_ = _normalize_list_or_str(raw_tags) if raw_tags else None
    metadata_filter_ = kwargs.get("metadata_filter") or kw.get("metadata_filter")
    explain_mode = kwargs.get("explain_mode", False) or kw.get("explain_mode", False)
    tag_operator = kwargs.get("tag_operator", "AND") or kw.get("tag_operator", "AND")

    return memory_service.search_memory(
        owner_id=owner_id_,
        query_keywords=query_keywords_,
        tags_filter=tags_filter_,
        metadata_filter=metadata_filter_,
        explain_mode=explain_mode,
        limit=limit,
        project_id=context_id_,
        context_id=context_id_,
        is_core=is_core,
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
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    action_ = action or kw.get("action") or kwargs.get("action") or "get"
    key_ = key or kw.get("key") or kwargs.get("key")
    value_ = value or kw.get("value") or kwargs.get("value")

    if action_ == "store" or value_ is not None:
        return ephemeral_service.store_ephemeral_memory(key=key_, value=value_)
    return ephemeral_service.get_ephemeral_memory(key=key_)

@mcp.tool()
def archive_memory(entity_id: str | list[str] = None, owner_id: str = None, **kwargs) -> str | list:
    """Explicitly archives (retires) one or multiple long-term memories.
    
    Accepts entity_id as a single string ID OR a list of string IDs.
    """
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    raw_target = entity_id or kw.get("entity_id") or kw.get("archive_requests") or kw.get("id") or kwargs.get("archive_requests")
    target = _normalize_list_or_str(raw_target)
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")

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
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    relations_ = relations or kw.get("relations") or kwargs.get("relations")
    if relations_:
        if isinstance(relations_, str):
            relations_ = _normalize_list_or_str(relations_)
        return relation_service.bulk_store_relations(relations=relations_)

    source_id_ = source_id or kw.get("source_id") or kw.get("source") or kwargs.get("source_id") or kwargs.get("source")
    target_id_ = target_id or kw.get("target_id") or kw.get("target") or kwargs.get("target_id") or kwargs.get("target")
    predicate_ = predicate or kw.get("predicate") or kw.get("relation") or kwargs.get("predicate") or kwargs.get("relation")
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
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    consolidations_ = consolidations or kw.get("consolidations") or kwargs.get("consolidations")
    if consolidations_:
        if isinstance(consolidations_, str):
            consolidations_ = _normalize_list_or_str(consolidations_)
        return relation_service.bulk_commit_consolidation(consolidations=consolidations_)

    raw_parents = parent_ids or kw.get("parent_ids") or kwargs.get("parent_ids")
    parent_ids_ = _normalize_list_or_str(raw_parents)
    title_ = title or kw.get("title") or kwargs.get("title")
    content_ = content or kw.get("content") or kw.get("text") or kwargs.get("content") or kwargs.get("text")
    raw_tags = tags or kw.get("tags") or kwargs.get("tags")
    tags_ = _normalize_list_or_str(raw_tags)
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    context_id_ = context_id or kw.get("context_id") or kw.get("project_id") or kwargs.get("context_id") or kwargs.get("project_id")
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
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    entity_id_ = entity_id or kw.get("entity_id") or kw.get("root_entity_id") or kw.get("root_id") or kw.get("id") or kwargs.get("root_entity_id") or kwargs.get("root_id")
    mode_ = mode or kw.get("mode") or kwargs.get("mode") or "dependencies"
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")

    if mode_ == "lineage":
        return relation_service.analyze_lineage(entity_id=entity_id_)
    elif mode_ == "orphans":
        return memory_service.detect_orphaned_memories(owner_id=owner_id_)
    else:
        max_depth_ = max_depth or kw.get("max_depth") or kwargs.get("max_depth") or 5
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
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    mode_ = mode or kw.get("mode") or kwargs.get("mode") or "events"
    session_id_ = session_id or kw.get("session_id") or kwargs.get("session_id")

    if session_id_ or mode_ == "session":
        return event_service.get_session_summary(session_id=session_id_)
    elif mode_ == "memories":
        owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
        status_filter_ = status_filter or kw.get("status_filter") or kwargs.get("status_filter")
        return memory_service.scan_memories(owner_id=owner_id_, status_filter=status_filter_, limit=limit, offset=offset)
    else:
        agent_id_ = agent_id or kw.get("agent_id") or kw.get("agent") or kwargs.get("agent_id") or kwargs.get("agent")
        type_filter_ = type_filter or kw.get("type_filter") or kw.get("type") or kwargs.get("type_filter") or kwargs.get("type")
        return event_service.get_recent_events(agent_id=agent_id_, type_filter=type_filter_, limit=limit)

