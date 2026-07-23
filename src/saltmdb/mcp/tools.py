from typing import Literal
from saltmdb.mcp.server import mcp
from saltmdb.domain.services import (
    event_service,
    memory_service,
    relation_service,
    ephemeral_service,
    librarian_service
)
from saltmdb.db import backup

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
def get_canonical_tags(domain: str = None, **kwargs) -> list:
    """Queries the database to suggest existing canonical tags to prevent fragmentation."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    domain_ = domain or kw.get("domain") or kw.get("query") or kw.get("substring") or kw.get("tag_filter") or kwargs.get("domain") or kwargs.get("query") or kwargs.get("substring") or kwargs.get("tag_filter")
    return memory_service.get_canonical_tags(domain=domain_)

@mcp.tool()
def store_memory(
    content: str = None,
    tags: list = None,
    owner_id: str = None,
    scope: Literal['private', 'shared'] = "shared",
    weight: int = 1,
    is_core: bool = False,
    title: str = None,
    entity_id: str = None,
    relevance: int = None,
    impact: int = None,
    novelty: int = None,
    actionability: int = None,
    metadata: dict = None,
    skip_duplicate_check: bool = False,
    project_id: str = None,
    context_id: str = None,
    **kwargs
) -> str:
    """Stores a consolidated Markdown fact chunk as a long-term memory."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    content_ = content or kw.get("content") or kw.get("text") or kwargs.get("content") or kwargs.get("text") or ""
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    context_id_ = context_id or project_id or kw.get("context_id") or kw.get("project_id") or kw.get("context") or kw.get("project") or kwargs.get("context_id") or kwargs.get("project_id") or kwargs.get("context") or kwargs.get("project")
    project_id_ = context_id_
    raw_tag = tags if tags is not None else (kw.get("tags") or kw.get("tag") or kwargs.get("tags") or kwargs.get("tag"))
    if isinstance(raw_tag, str):
        tags_ = [raw_tag]
    elif isinstance(raw_tag, list):
        tags_ = raw_tag
    else:
        tags_ = []

    return memory_service.store_memory(
        content=content_,
        tags=tags_,
        owner_id=owner_id_,
        scope=scope,
        weight=weight,
        is_core=is_core,
        title=title or kw.get("title") or kwargs.get("title"),
        entity_id=entity_id or kw.get("entity_id") or kw.get("id") or kwargs.get("entity_id") or kwargs.get("id"),
        relevance=relevance,
        impact=impact,
        novelty=novelty,
        actionability=actionability,
        metadata=metadata or kw.get("metadata") or kwargs.get("metadata"),
        skip_duplicate_check=skip_duplicate_check,
        project_id=project_id_,
        context_id=context_id_
    )

@mcp.tool()
def search_memory(
    owner_id: str = None,
    query_keywords: str = None,
    tags_filter: list = None,
    metadata_filter: dict = None,
    explain_mode: bool = False,
    limit: int = 5,
    project_id: str = None,
    context_id: str = None,
    is_core: bool = None,
    tag_operator: Literal['AND', 'OR'] = "AND",
    cursor: str = None,
    include_related: bool = False
) -> list | dict:
    """Performs full-text keyword search and filtering in long-term memory."""
    context_id_ = context_id or project_id
    return memory_service.search_memory(
        owner_id=owner_id,
        query_keywords=query_keywords,
        tags_filter=tags_filter,
        metadata_filter=metadata_filter,
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
def fetch_memory_chunk(entity_id: str = None, **kwargs) -> str:
    """Returns full markdown text of a memory."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    entity_id_ = entity_id or kw.get("entity_id") or kw.get("id") or kwargs.get("entity_id") or kwargs.get("id")
    return memory_service.fetch_memory_chunk(entity_id=entity_id_)

@mcp.tool()
def store_ephemeral_memory(key: str = None, value: str = None, **kwargs) -> str:
    """Saves a volatile secret to the in-memory database."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    key_ = key or kw.get("key") or kwargs.get("key")
    value_ = value or kw.get("value") or kwargs.get("value")
    return ephemeral_service.store_ephemeral_memory(key=key_, value=value_)

@mcp.tool()
def get_ephemeral_memory(key: str = None, **kwargs) -> str:
    """Retrieves a volatile secret."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    key_ = key or kw.get("key") or kwargs.get("key")
    return ephemeral_service.get_ephemeral_memory(key=key_)

@mcp.tool()
def start_db_viewer(port: int = None, **kwargs) -> str:
    """Spawns the local SALTMDB web dashboard/viewer in the background."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    port_ = port or kw.get("port") or kwargs.get("port") or 8080
    from saltmdb.viewer.server import start_viewer
    return start_viewer(port=port_)

@mcp.tool()
def stop_db_viewer(port: int = None, **kwargs) -> str:
    """Stops the running local SALTMDB web dashboard/viewer."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    port_ = port or kw.get("port") or kwargs.get("port") or 8080
    from saltmdb.viewer.server import stop_viewer
    return stop_viewer(port=port_)

@mcp.tool()
def commit_consolidation(
    parent_ids: list = None,
    title: str = None,
    content: str = None,
    tags: list = None,
    scope: Literal['private', 'shared'] = "shared",
    weight: int = 1,
    **kwargs
) -> str:
    """Commits a consolidated memory synthesized by the agent, atomically archiving the raw parents."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    parent_ids_ = parent_ids or kw.get("parent_ids") or kwargs.get("parent_ids") or []
    title_ = title or kw.get("title") or kwargs.get("title")
    content_ = content or kw.get("content") or kw.get("text") or kwargs.get("content") or kwargs.get("text")
    tags_ = tags or kw.get("tags") or kwargs.get("tags") or []
    return relation_service.commit_consolidation(parent_ids=parent_ids_, title=title_, content=content_, tags=tags_, scope=scope, weight=weight)

@mcp.tool()
def create_snapshot(**kwargs) -> str:
    """Safely creates a timestamped database backup in backups/ using SQLite's backup API."""
    return backup.create_snapshot()

@mcp.tool()
def archive_memory(entity_id: str = None, owner_id: str = None, **kwargs) -> str:
    """Explicitly archives (retires) a long-term memory."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    entity_id_ = entity_id or kw.get("entity_id") or kw.get("id") or kwargs.get("entity_id") or kwargs.get("id")
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    return memory_service.archive_memory(entity_id=entity_id_, owner_id=owner_id_)

@mcp.tool()
def detect_orphaned_memories(owner_id: str = None, **kwargs) -> dict:
    """Identifies active memories with zero relationship links."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    return memory_service.detect_orphaned_memories(owner_id=owner_id_)

@mcp.tool()
def check_duplicate_memories(
    title: str = None,
    content: str = None,
    owner_id: str = None,
    tags: list = None,
    project_id: str = None,
    **kwargs
) -> dict:
    """Checks the database for potential near-duplicates of a proposed memory."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    content_ = content or kw.get("content") or kw.get("text") or kwargs.get("content") or kwargs.get("text")
    title_ = title or kw.get("title") or kwargs.get("title")
    tags_ = tags or kw.get("tags") or kwargs.get("tags")
    project_id_ = project_id or kw.get("project_id") or kwargs.get("project_id")
    return memory_service.check_duplicate_memories(title=title_, content=content_, owner_id=owner_id_, tags=tags_, project_id=project_id_)

@mcp.tool()
def store_relation(source_id: str = None, target_id: str = None, predicate: str = None, **kwargs) -> str:
    """Stores a directional semantic relationship edge between two entity nodes."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    source_id_ = source_id or kw.get("source_id") or kw.get("source") or kwargs.get("source_id") or kwargs.get("source")
    target_id_ = target_id or kw.get("target_id") or kw.get("target") or kwargs.get("target_id") or kwargs.get("target")
    predicate_ = predicate or kw.get("predicate") or kw.get("relation") or kwargs.get("predicate") or kwargs.get("relation")
    return relation_service.store_relation(source_id=source_id_, target_id=target_id_, predicate=predicate_)

@mcp.tool()
def analyze_dependencies(root_entity_id: str = None, max_depth: int = 5, **kwargs) -> dict:
    """Traverses relationship trees using recursive SQL CTEs to map downstream components."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    root_entity_id_ = root_entity_id or kw.get("root_entity_id") or kw.get("root_id") or kw.get("entity_id") or kwargs.get("root_entity_id") or kwargs.get("root_id") or kwargs.get("entity_id")
    max_depth_ = max_depth or kw.get("max_depth") or kwargs.get("max_depth") or 5
    return relation_service.analyze_dependencies(root_entity_id=root_entity_id_, max_depth=max_depth_)

@mcp.tool()
def analyze_lineage(entity_id: str = None, **kwargs) -> dict:
    """Traverses full multi-generation consolidation and derivation ancestry."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    entity_id_ = entity_id or kw.get("entity_id") or kw.get("id") or kwargs.get("entity_id") or kwargs.get("id")
    return relation_service.analyze_lineage(entity_id=entity_id_)

@mcp.tool()
def get_recent_events(agent_id: str = None, type_filter: str = None, limit: int = 20, **kwargs) -> list:
    """Retrieves events logged to the short-term ledger."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    agent_id_ = agent_id or kw.get("agent_id") or kw.get("agent") or kwargs.get("agent_id") or kwargs.get("agent")
    type_filter_ = type_filter or kw.get("type_filter") or kw.get("type") or kwargs.get("type_filter") or kwargs.get("type")
    limit_ = limit or kw.get("limit") or kwargs.get("limit") or 20
    return event_service.get_recent_events(agent_id=agent_id_, type_filter=type_filter_, limit=limit_)

@mcp.tool()
def scan_memories(owner_id: str = None, status_filter: str = None, limit: int = 20, offset: int = 0, **kwargs) -> list:
    """Scans and inspects lists/contents of memories."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    owner_id_ = owner_id or kw.get("owner_id") or kw.get("owner") or kwargs.get("owner_id") or kwargs.get("owner")
    status_filter_ = status_filter or kw.get("status_filter") or kwargs.get("status_filter")
    limit_ = limit or kw.get("limit") or kwargs.get("limit") or 20
    offset_ = offset or kw.get("offset") or kwargs.get("offset") or 0
    return memory_service.scan_memories(owner_id=owner_id_, status_filter=status_filter_, limit=limit_, offset=offset_)

@mcp.tool()
def get_session_summary(session_id: str = None, **kwargs) -> list:
    """Retrieves session summary events."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    session_id_ = session_id or kw.get("session_id") or kwargs.get("session_id")
    return event_service.get_session_summary(session_id=session_id_)

@mcp.tool()
def bulk_commit_consolidation(consolidations: list = None, **kwargs) -> list:
    """Bulk commits consolidations."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    consolidations_ = consolidations or kw.get("consolidations") or kwargs.get("consolidations") or []
    return relation_service.bulk_commit_consolidation(consolidations=consolidations_)

@mcp.tool()
def bulk_archive_memory(archive_requests: list = None, **kwargs) -> list:
    """Bulk archives memories."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    archive_requests_ = archive_requests or kw.get("archive_requests") or kwargs.get("archive_requests") or []
    return memory_service.bulk_archive_memory(archive_requests=archive_requests_)

@mcp.tool()
def bulk_store_relations(relations: list = None, **kwargs) -> list:
    """Bulk stores relations."""
    kw = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else kwargs
    relations_ = relations or kw.get("relations") or kwargs.get("relations") or []
    return relation_service.bulk_store_relations(relations=relations_)

