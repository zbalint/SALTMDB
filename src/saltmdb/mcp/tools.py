from typing import Literal
import json
import logging
from saltmdb.mcp.server import mcp
from saltmdb.daemon import client as daemon_client
from saltmdb.daemon import protocol

# ephemeral_service is the sole domain.services import remaining in this module (see
# ephemeral_memory below): EPHEMERAL_CONN never touches the persistent DB, so ephemeral_memory
# never goes over RPC and stays adapter-local, exactly as before Track B.
from saltmdb.domain.services import ephemeral_service, core_governance_service

logger = logging.getLogger(__name__)


class _UnsetRetrievalText(str):
    """Serializable MCP-schema sentinel that still preserves Python identity for omission."""


# A string subclass keeps FastMCP/Pydantic schema generation warning-free while identity (rather
# than equality) distinguishes an omitted field from an explicit JSON null.  The marker is only an
# adapter default; it is never sent to the domain service or persisted.
_RETRIEVAL_TEXT_UNSET = _UnsetRetrievalText("__saltmdb_retrieval_text_omitted__")


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
            except json.JSONDecodeError as exc:
                logger.debug(
                    "Ignoring malformed JSON list input and applying string fallback: %s", exc
                )
        if "," in val_str:
            return [s.strip() for s in val_str.split(",") if s.strip()]
        return [val_str]
    return [val]


def _effective_owner(
    owner_id: str | None,
    *,
    tool_func=None,
    submitted: dict | None = None,
) -> str:
    """Bind the adapter identity at the tool boundary and return its effective value.

    The daemon is shared by all adapter sessions, so identity must be established before a
    request crosses the backend boundary.  A first call without an owner is a hard failure with
    a copyable correction; subsequent calls may omit it and use the immutable binding.
    """
    from saltmdb.mcp.identity import SESSION_IDENTITY

    if owner_id:
        SESSION_IDENTITY.bind(owner_id)
        return owner_id
    if SESSION_IDENTITY.owner_id:
        return SESSION_IDENTITY.owner_id
    from saltmdb.utils.corrected_call import build_corrected_call

    corrected_call = (
        build_corrected_call(tool_func, submitted or {}, {"owner_id": "<agent-id>"})
        if tool_func is not None
        else {"owner_id": "<agent-id>"}
    )
    raise ValueError(
        "owner_id is required on the first MCP tool call; retry with an agent identity. "
        f"corrected_call: {json.dumps(corrected_call)}. "
        "Start a new MCP connection to change it."
    )


class DirectDispatchBackend:
    """Calls daemon/dispatch.py in-process, no network. Two legitimate callers, not test-only
    scaffolding duplicated for two purposes: (a) explicitly injected by tests
    (tests/test_mcp_tools.py, tests/test_tag_merge_tool.py setUp/tearDown) since those exercise
    this module's argument-normalization layer against a temp DB with no daemon involved; (b) the
    daemon's own RPC handler (daemon/server.py), which IS a DirectDispatchBackend instance
    receiving already-normalized kwargs over the wire."""

    def call(self, tool_name: str, kwargs: dict):
        from saltmdb.daemon import dispatch

        return dispatch.DISPATCH_TABLE[tool_name](**kwargs)


class RpcBackend:
    """The only backend used in real production adapter runtime -- configured exactly once, by
    __main__.py's default branch, synchronously, BEFORE mcp.run() is called (not inside
    server_lifespan, which owns only the SessionConnection). Classifies mid-call RPC failures per
    protocol.WRITE_TOOLS/READ_TOOLS (§12): a write tool never silently retries, a read tool does."""

    def call(self, tool_name: str, kwargs: dict):
        from saltmdb.config import get_db_path
        from saltmdb.mcp.identity import SESSION_IDENTITY

        # Per-session owner binding (§4.5): the first call that supplies owner_id binds it for
        # the rest of this adapter process's life; every later call that omits owner_id gets the
        # bound value injected here. A rebind attempt (a different owner_id later) raises
        # IdentityRebindRejected -- deliberately NOT caught here, so it surfaces exactly like any
        # other tool-call exception (Phase 1 scope: bind/inject only, no hard-fail on a MISSING
        # owner_id yet -- ownership-bearing tool bodies now reject that first call before this
        # backend is reached; this guard remains responsible only for real adapter RPC calls.
        if kwargs.get("owner_id"):
            SESSION_IDENTITY.bind(kwargs["owner_id"])
        elif SESSION_IDENTITY.owner_id and "owner_id" in kwargs:
            kwargs = {**kwargs, "owner_id": SESSION_IDENTITY.owner_id}

        db_path = get_db_path()
        try:
            return daemon_client.call(db_path, tool_name, kwargs)
        except daemon_client.DaemonRpcError as e:
            if e.code == "MID_CALL_FAILURE":
                if tool_name in protocol.READ_TOOLS:
                    return daemon_client.call(db_path, tool_name, kwargs)
                if tool_name in protocol.WRITE_TOOLS:
                    return {
                        "status": "DAEMON_CONNECTION_LOST_DURING_WRITE",
                        "tool": tool_name,
                        "advice": (
                            "The daemon connection was lost while this write was in flight. "
                            "Whether it committed is unknown from here -- SQLite's own transaction "
                            "durability means it either fully committed or fully rolled back, "
                            "never partially, but that answer didn't make it back over this "
                            "connection. Re-verify before retrying, to avoid creating a duplicate."
                        ),
                    }
            raise


_backend = None  # unconfigured by default -- calling a tool with no backend set raises clearly


def _backend_or_raise():
    if _backend is None:
        raise RuntimeError(
            "No backend configured -- tools.py must not be called without either "
            "configure_backend() (production, __main__.py) or an explicit test-injected "
            "DirectDispatchBackend (tests)."
        )
    return _backend


def configure_backend(backend) -> None:
    """Production entrypoint: called once by __main__.py, before mcp.run(). Never reset for the
    remaining life of the process -- there is exactly one backend for an adapter process's entire
    run, by construction, so there is nothing to restore."""
    global _backend
    _backend = backend


def _set_backend_for_test(backend):
    """Test-only: returns the previous value so a test's tearDown can restore it. Never called by
    production code -- production uses configure_backend(), which never needs a restore path."""
    global _backend
    prev, _backend = _backend, backend
    return prev


@mcp.tool()
def log_event(
    agent_id: str | None = None,
    type: str = "event",
    content: str = "",
    error_code: str | None = None,
    session_id: str | None = None,
    context_id: str | None = None,
    owner_id: str | None = None,
) -> str:
    """Appends an event to the append-only events ledger."""
    owner_id_ = _effective_owner(owner_id, tool_func=log_event, submitted=locals())
    return _backend_or_raise().call(
        "log_event",
        {
            "agent_id": agent_id or owner_id_,
            "type": type,
            "content": content,
            "error_code": error_code,
            "session_id": session_id,
            "context_id": context_id,
        },
    )


@mcp.tool()
def get_canonical_tags(query: str | None = None, limit: int | None = None) -> list:
    """Queries the database to suggest existing canonical tags matching a search query/substring, to prevent tag fragmentation. Use query='auth' to filter by tag name substring.

    limit caps the number of tags returned (default 50), including when query is omitted --
    the full canonical tag table is never dumped unbounded."""
    return _backend_or_raise().call(
        "get_canonical_tags", {"domain": query, "limit": limit if limit is not None else 50}
    )


@mcp.tool()
def get_canonical_predicates(query: str | None = None, limit: int | None = None) -> list:
    """Queries existing canonical relation predicates matching a search substring, to reduce
    predicate drift (e.g. elaborates_on vs relates_to vs references).

    limit caps the number of predicates returned (default 50)."""
    return _backend_or_raise().call(
        "get_canonical_predicates", {"query": query, "limit": limit if limit is not None else 50}
    )


@mcp.tool()
def merge_tags(
    keep_tag: str | None = None,
    tags_to_merge: list | str | None = None,
) -> str:
    """Merges one or more fragmented/synonym tags into an explicitly chosen canonical tag, repointing all
    affected entities' tag associations. Use to fix folksonomy fragmentation (e.g. keep_tag='#docs',
    tags_to_merge=['#doc', '#documentation'])."""
    tags_to_merge_ = _normalize_list_or_str(tags_to_merge)
    return _backend_or_raise().call(
        "merge_tags", {"keep_tag": keep_tag, "tags_to_merge": tags_to_merge_}
    )


@mcp.tool(
    description="""Stores a consolidated Markdown fact chunk as long-term memory.

    memory_type classifies the memory into one of five fixed kinds (default 'fact' on a new
    memory; omitting it on an update preserves the existing value, same as is_core):
      - fact: semantic, durable, generalized knowledge.
      - event: episodic, something that happened, ideally timestamped.
      - procedure: how-to / runbook / skill.
      - decision: ADR-style rationale record (what was chosen and why).
      - preference: durable user/agent preference statement.

    is_core is the single writable source of truth for "always load at session bootstrap."
    The '#core' tag is a derived label the server auto-maintains from is_core on every write --
    do not set '#core' via the tags list, it will be silently overridden to match is_core.

    entity_id (optional) targets an existing memory directly for an update -- when supplied it
    takes precedence over the automatic same-title/owner/scope match and bypasses the
    exact-content-hash duplicate check, so a metadata-only edit (e.g. re-tagging, backfilling
    core_reason/core_exit_condition) doesn't require changing content.

    If check_duplicates_only is True, returns duplicate detection results without writing to the database.

    Store-time disposition (Track A): every call is preflighted before persistence. If
    evidence-gathering finds no flagged candidates, this behaves exactly as always -- a single
    call, plain string result. If it finds one or more (a possible duplicate, supersession, or
    stale-consolidated-node signal), nothing is written; instead this returns a dict with
    `status: "REVIEW_REQUIRED"`, an opaque `review_token`, and the flagged `candidates`, each with
    an advisory `suggested_label` (never authoritative -- use your own judgment, including calling
    it a false alarm) and the `available_dispositions` for it. Resend the identical call with
    `review_token` and `dispositions` (one `{"candidate_id": ..., "disposition": "distinct" |
    "supersede" | "consolidate"}` per flagged candidate) to commit. A stale/expired token, or a
    resend that no longer matches what was previewed, returns `status: "REVIEW_STALE"` instead --
    call again without `review_token` for a fresh preflight.

    Core-memory bootstrap governance: `is_core=True` marks a memory for injection into every
    future session's bootstrap context -- it is a SCARCE, TEMPORARY mechanism for urgent
    cross-session hazards an agent must know before it could reasonably search for them, not a
    general "important knowledge" tier. Stable coding rules/preferences belong in AGENTS.md/
    CLAUDE.md/skills instead. Creating or promoting a core memory requires `scope="shared"` (no
    private cores), `core_reason` (20-500 chars: the harm that could occur before natural
    retrieval), `core_exit_condition` (20-500 chars: the observable condition that ends the
    urgency), and admits three independent hard caps: at most 5 active cores globally, at most
    2,500 Unicode characters of `content` per core, and a 15,000-character exact rendered
    bootstrap digest. A capacity failure returns `status: "REJECTED"` with `error_code:
    "CORE_CAPACITY_EXCEEDED"`, a balanced inventory of every active core (no full content), and
    zero side effects -- rebalance (demote/archive/shorten/consolidate existing cores) and retry;
    this never requires a human decision. `core_review_after` defaults to 14 days out and may
    never exceed 30 days; while ANY core is overdue for review, creating/promoting a new core,
    enlarging an existing core's content, or changing its review timestamp is blocked (use
    `review_core_memory` to resolve the overdue review first) -- demote/archive/non-expanding
    edits stay allowed. Omitting `core_reason`/`core_exit_condition`/`core_review_after`/
    `detail_memory_ids` on an update to an already-core memory preserves the existing values;
    supplying any of them when the effective memory is NOT core is rejected, never silently
    ignored. `detail_memory_ids` (at most 3 full UUIDs of existing shared, non-core memories whose
    canonical title+UUID must appear in `content`) atomically maintains `elaborates_on` edges from
    each detail memory to this core -- `None` preserves the current declaration, `[]` clears it.
    A core must stay directly actionable on its own even if a weaker agent never follows a detail
    link; move rationale/chronology/evidence into the linked detail memories instead.
    """
)
def store_memory(
    title: str,
    content: str,
    tags: list[str],
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] = "fact",
    owner_id: str | None = None,
    context_id: str | None = None,
    entity_id: str | None = None,
    is_core: bool | None = None,
    scope: Literal["private", "shared"] = "shared",
    check_duplicates_only: bool = False,
    skip_duplicate_check: bool = False,
    review_token: str | None = None,
    dispositions: list | None = None,
    retrieval_text: str | None = _RETRIEVAL_TEXT_UNSET,
    core_reason: str | None = None,
    core_exit_condition: str | None = None,
    core_review_after: str | None = None,
    detail_memory_ids: list | None = None,
) -> str | dict:
    owner_id_ = _effective_owner(owner_id, tool_func=store_memory, submitted=locals())
    tags_ = _normalize_list_or_str(tags)
    try:
        # Strict tri-state parse (core-memory governance resolved gap #6): an unrecognized value
        # like "yes" or an integer is rejected outright here, at the adapter boundary, rather
        # than silently coerced to False the way the old `in (True, 1, "true", "1", "True")`
        # membership check did.
        is_core_ = core_governance_service.parse_is_core(is_core)
    except ValueError as e:
        return f"Error: {e}"

    memory_type_ = memory_type
    check_duplicates_only_ = check_duplicates_only
    review_token_ = review_token
    dispositions_ = dispositions
    retrieval_text_provided = retrieval_text is not _RETRIEVAL_TEXT_UNSET
    retrieval_text_ = retrieval_text if retrieval_text_provided else None
    detail_memory_ids_ = (
        _normalize_list_or_str(detail_memory_ids) if detail_memory_ids is not None else None
    )

    return _backend_or_raise().call(
        "store_memory",
        {
            "content": content,
            "tags": tags_,
            "owner_id": owner_id_,
            "scope": scope,
            "is_core": is_core_,
            "memory_type": memory_type_,
            "title": title,
            "entity_id": entity_id,
            "skip_duplicate_check": skip_duplicate_check,
            "context_id": context_id,
            "review_token": review_token_,
            "dispositions": dispositions_,
            "check_duplicates_only": check_duplicates_only_,
            "retrieval_text": retrieval_text_,
            "retrieval_text_provided": retrieval_text_provided,
            "core_reason": core_reason,
            "core_exit_condition": core_exit_condition,
            "core_review_after": core_review_after,
            "detail_memory_ids": detail_memory_ids_,
        },
    )


@mcp.tool(
    description="""Performs full-text keyword & dense vector hybrid search in long-term memory.

    Search by query, tags, context, memory type, or core status. `mode="strict"` resolves
    superseded matches and applies relevance abstention; `mode="history"` keeps matched history
    visible and labels superseded results; `mode="broad"` preserves ordinary retrieval.

    Explicit-ID retrieval is provided by the dedicated get_memory tool. Experimental ranking and benchmark controls remain
    available through internal services/evaluation tooling, but are intentionally absent here.
    """
)
def search_memory(
    query_keywords: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    context_id: str | None = None,
    tags_filter: list[str] | None = None,
    memory_type_filter: Literal["fact", "event", "procedure", "decision", "preference"]
    | None = None,
    is_core: bool | None = None,
    include_related: bool | None = None,
    mode: Literal["strict", "broad", "history"] | None = None,
    owner_id: str | None = None,
) -> list | dict | str:
    owner_id_ = _effective_owner(owner_id, tool_func=search_memory, submitted=locals())
    tags_filter_ = _normalize_list_or_str(tags_filter) if tags_filter else None

    return _backend_or_raise().call(
        "search_memory",
        {
            "owner_id": owner_id_,
            "query_keywords": query_keywords,
            "tags_filter": tags_filter_,
            "limit": limit if limit is not None else 5,
            "context_id": context_id,
            "is_core": is_core,
            "memory_type_filter": memory_type_filter,
            "cursor": cursor,
            "mode": mode if mode is not None else "broad",
            "include_related": include_related if include_related is not None else True,
        },
    )


@mcp.tool()
def ephemeral_memory(
    action: Literal["get", "store"] = "get", key: str | None = None, value: str | None = None
) -> str:
    """Manages volatile in-memory secret storage (get or store)."""

    # Deliberately NOT routed through _backend_or_raise() -- EPHEMERAL_CONN is a separate
    # in-memory-only sqlite3 connection that never touches the persistent DB, so this tool was
    # never in scope for the DB-access-boundary invariant to begin with. Routing it through the
    # daemon would silently turn per-agent-process-isolated volatile secrets into a cross-agent-
    # shared store (Codex Track-B plan-review round-2 finding) -- calling ephemeral_service
    # directly, in-process, exactly as before Track B, preserves today's isolation exactly.
    if action == "store" or value is not None:
        return ephemeral_service.store_ephemeral_memory(key=key or "", value=value or "")
    return ephemeral_service.get_ephemeral_memory(key=key or "")


@mcp.tool()
def archive_memory(
    entity_id: str | list[str] | None = None, owner_id: str | None = None
) -> str | list:
    """Explicitly archives (retires) one or multiple long-term memories.

    Accepts entity_id as a single string ID OR a list of string IDs.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=archive_memory, submitted=locals())
    raw_target = entity_id
    target = _normalize_list_or_str(raw_target)

    # The bulk/single/none decision depends on the ORIGINAL request shape (did the caller pass a
    # list, even a 1-item one?) -- pre-normalization information that daemon/dispatch.py can't
    # reconstruct from the already-normalized `target` list alone, so the mode is resolved here
    # and sent as an explicit tag (self-caught during implementation, see dispatch.py's matching
    # comment).
    if len(target) > 1 or (isinstance(raw_target, list) and len(target) > 0):
        return _backend_or_raise().call(
            "archive_memory", {"mode": "bulk", "archive_requests": target, "owner_id": owner_id_}
        )
    elif len(target) == 1:
        return _backend_or_raise().call(
            "archive_memory", {"mode": "single", "entity_id": target[0], "owner_id": owner_id_}
        )
    return _backend_or_raise().call("archive_memory", {"mode": "none", "owner_id": owner_id_})


@mcp.tool()
def manage_relation(
    relations: list | None = None,
    source_id: str | None = None,
    target_id: str | None = None,
    predicate: str | None = None,
    invalidate: bool = False,
    valid_at: str | None = None,
    invalid_at: str | None = None,
    override_justification: str | None = None,
    owner_id: str | None = None,
) -> str | list:
    """Stores one or multiple directional semantic relationship edges between memory nodes, or invalidates an existing edge (invalidate=True).

    A governance gate rejects "strong" predicates (elaborates_on/resolves/supersedes) whose
    source/target chunk-embedding centroids fail a minimum similarity threshold
    (REJECT_LOW_RELATION_SIMILARITY), and rejects a contradictory predicate pair on the same
    directional edge (REJECT_CONTRADICTORY_PREDICATE), unless override_justification (a
    non-throwaway string explaining why this relation should proceed anyway) is supplied to
    force it through -- the override is atomically audited. For the bulk (`relations`) shape,
    put `override_justification`/`owner_id` on each individual item that needs them, not at the
    top level -- neither is shared across items in the same batch.

    Core-memory governance: a NEW `elaborates_on` edge whose target is an active core memory is
    rejected (`REJECT_CORE_ELABORATES_ON`) -- only that core's own `detail_memory_ids`
    declaration (via `store_memory`/`commit_consolidation`) may create one. Re-submitting an
    edge that already exists stays an idempotent no-op regardless.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=manage_relation, submitted=locals())
    relations_ = relations
    if relations_ and isinstance(relations_, str):
        relations_ = _normalize_list_or_str(relations_)

    return _backend_or_raise().call(
        "manage_relation",
        {
            "relations": relations_,
            "source_id": source_id,
            "target_id": target_id,
            "predicate": predicate,
            "invalidate": invalidate,
            "invalid_at": invalid_at,
            "valid_at": valid_at,
            "override_justification": override_justification,
            "owner_id": owner_id_,
        },
    )


@mcp.tool()
def commit_consolidation(
    consolidations: list | None = None,
    parent_ids: list | None = None,
    title: str | None = None,
    content: str | None = None,
    tags: list | None = None,
    owner_id: str | None = None,
    context_id: str | None = None,
    scope: Literal["private", "shared"] = "shared",
    weight: int | float = 1,
    is_core: bool | None = None,
    override_justification: str | None = None,
    core_reason: str | None = None,
    core_exit_condition: str | None = None,
    core_review_after: str | None = None,
    detail_memory_ids: list | None = None,
) -> str | list:
    """Commits single or multiple consolidated memories, archiving raw parents and creating lineage edges.

    A pairwise-cohesion gate rejects parent sets whose chunk-embedding centroids fail a minimum
    similarity threshold (REJECT_LOW_COHESION), unless override_justification (a non-throwaway
    string explaining why this merge should proceed anyway) is supplied to force it through --
    the override is baked into the committed content and atomically audited. For the bulk
    (`consolidations`) shape, put `override_justification` on each individual item that needs
    it, not at the top level -- it is never shared across items in the same batch.

    Core-memory governance: `is_core` is NEVER inherited from parents. If any resolved parent is
    currently an active core (is_core=1, not archived) and `is_core` is omitted, the commit is
    rejected with an actionable error -- pass explicit `is_core=True` (with `core_reason`/
    `core_exit_condition`, each 20-500 chars, plus optionally `core_review_after`/
    `detail_memory_ids`) to keep the result core, or `is_core=False` to let it become an ordinary
    memory. The same capacity caps and detail-relation rules as `store_memory` apply. For the
    bulk shape, put every `core_*`/`detail_memory_ids` field on each individual item.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=commit_consolidation, submitted=locals())
    consolidations_ = consolidations
    if consolidations_ and isinstance(consolidations_, str):
        consolidations_ = _normalize_list_or_str(consolidations_)

    parent_ids_ = _normalize_list_or_str(parent_ids)
    tags_ = _normalize_list_or_str(tags)
    detail_memory_ids_ = (
        _normalize_list_or_str(detail_memory_ids) if detail_memory_ids is not None else None
    )

    return _backend_or_raise().call(
        "commit_consolidation",
        {
            "consolidations": consolidations_,
            "parent_ids": parent_ids_,
            "title": title,
            "content": content,
            "is_core": is_core,
            "tags": tags_,
            "scope": scope,
            "weight": weight,
            "owner_id": owner_id_,
            "context_id": context_id,
            "core_reason": core_reason,
            "core_exit_condition": core_exit_condition,
            "core_review_after": core_review_after,
            "detail_memory_ids": detail_memory_ids_,
            "override_justification": override_justification,
        },
    )


@mcp.tool()
def get_memory(entity_id: str, owner_id: str | None = None) -> dict:
    """Retrieves one memory by full ID or an unambiguous ID prefix.

    Explicit retrieval includes archived memories and returns the memory's status and lineage;
    an archived ID is never silently redirected to a successor.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=get_memory, submitted=locals())
    return _backend_or_raise().call("get_memory", {"entity_id": entity_id, "owner_id": owner_id_})


@mcp.tool()
def get_lineage(
    entity_id: str,
    direction: Literal["ancestors", "descendants"] = "ancestors",
    max_depth: int = 5,
    owner_id: str | None = None,
) -> dict:
    """Traverses memory lineage in either direction.

    ``ancestors`` shows where an entity came from; ``descendants`` shows what it became.
    Archived nodes remain visible so historical provenance is never hidden.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=get_lineage, submitted=locals())
    return _backend_or_raise().call(
        "get_lineage",
        {
            "entity_id": entity_id,
            "direction": direction,
            "max_depth": max_depth,
            "owner_id": owner_id_,
        },
    )


@mcp.tool()
def get_related_memories(entity_id: str, max_depth: int = 5, owner_id: str | None = None) -> dict:
    """Traverses semantic relations from one memory for up to ``max_depth`` hops."""
    owner_id_ = _effective_owner(owner_id, tool_func=get_related_memories, submitted=locals())
    return _backend_or_raise().call(
        "get_related_memories",
        {"entity_id": entity_id, "max_depth": max_depth, "owner_id": owner_id_},
    )


@mcp.tool()
def get_events(
    agent_id: str | None = None,
    type_filter: str | None = None,
    session_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    status_filter: str | None = None,
    owner_id: str | None = None,
    mode: Literal["events", "session", "memories"] = "events",
) -> list:
    """Retrieves operational events, session summary events, or scans memory logs."""
    owner_id_ = _effective_owner(owner_id, tool_func=get_events, submitted=locals())

    return _backend_or_raise().call(
        "get_events",
        {
            "mode": mode,
            "limit": limit if limit is not None else 20,
            "offset": offset if offset is not None else 0,
            "session_id": session_id,
            "agent_id": agent_id,
            "type_filter": type_filter,
            "status_filter": status_filter,
            "owner_id": owner_id_,
        },
    )


@mcp.tool()
def export_corpus_snapshot(
    owner_id: str | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
    snapshot_hash: str | None = None,
    include_archived: bool = False,
) -> dict:
    """Exports authoritative entity pages for an immutable evaluation corpus snapshot.

    The first call omits cursor/snapshot_hash.  Subsequent calls pass both values returned by the
    previous page; a changed production corpus or schema fails closed instead of mixing pages.
    The service owns the SQLite read transaction and benchmark callers must not query SQL.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=export_corpus_snapshot, submitted=locals())
    return _backend_or_raise().call(
        "export_corpus_snapshot",
        {
            "owner_id": owner_id_,
            "page_size": page_size,
            "cursor": cursor,
            "snapshot_hash": snapshot_hash,
            "include_archived": include_archived,
        },
    )


@mcp.tool()
def dismiss_event(
    event_id: str | list[str] | None = None,
    reason: str | None = None,
    agent_id: str | None = None,
    owner_id: str | None = None,
) -> str:
    """Dismisses review events to prevent them from remaining pending."""
    owner_id_ = _effective_owner(owner_id, tool_func=dismiss_event, submitted=locals())
    if not event_id:
        raise ValueError("Missing 'event_id' parameter.")
    return _backend_or_raise().call(
        "dismiss_event",
        {"event_ids": event_id, "reason": reason, "agent_id": agent_id or owner_id_},
    )


@mcp.tool()
def review_core_memory(
    entity_id: str | None = None,
    outcome: Literal["retain", "demote", "archive"] | None = None,
    review_rationale: str | None = None,
    owner_id: str | None = None,
    core_review_after: str | None = None,
) -> str:
    """Reviews an active core memory: retain (extend its next review date), demote (turn it back
    into an ordinary searchable memory), or archive (retire it) -- a direct, synchronous
    operation, never a request/queue/event.

    `owner_id` identifies the REVIEWING agent; it need not match the entity's own owner and never
    transfers ownership. `review_rationale` (20-1,000 chars) is stored for provenance but never
    injected into the bootstrap digest. `retain` requires an absolute `core_review_after`
    timestamp in the future and no more than 30 days out (omit it to default to 14 days from
    now); `demote`/`archive` must not supply `core_review_after`. Meaningful CONTENT revision is
    still a `store_memory` update -- this tool changes lifecycle state only. Repeating `demote`/
    `archive` on an already-non-core/already-archived memory is a no-op, not an error; `retain`
    against a non-core or archived memory is rejected.
    """
    return _backend_or_raise().call(
        "review_core_memory",
        {
            "entity_id": entity_id,
            "outcome": outcome,
            "review_rationale": review_rationale,
            "owner_id": owner_id,
            "core_review_after": core_review_after,
        },
    )
