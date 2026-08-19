from typing import Any, Literal
import json
import logging
import re
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


_YAML_FRONT_MATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)


def _front_matter_identity_fields(content: str) -> tuple[list[str], str]:
    """Return forbidden identity keys and the body with leading YAML metadata removed."""
    if not isinstance(content, str):
        return [], content
    match = _YAML_FRONT_MATTER_RE.match(content)
    if match is None:
        return [], content
    fields: list[str] = []
    for line in match.group("header").splitlines():
        key_match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
        if key_match and key_match.group(1).lower() in {"title", "tags"}:
            fields.append(key_match.group(1).lower())
    return sorted(set(fields)), content[match.end() :].lstrip("\r\n")


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
    event_type: str,
    content: str,
    context_id: str | None = None,
    error_code: str | None = None,
    owner_id: str | None = None,
) -> str:
    """Appends an event to the append-only events ledger.

    The bound owner (§4.5's per-session identity) is injected as the event's `agent_id` --
    log_event has no separate agent_id parameter of its own. `get_events` keeps `agent_id` as a
    FILTER for cross-agent coordination, which is why the two tools are asymmetric on this
    field: an agent always logs as itself, but may read what any agent logged.

    `session_id` is not a parameter here either -- the adapter auto-populates it from the host
    harness's own session id (an opaque, advisory pointer; see get_events' docstring).
    """
    owner_id_ = _effective_owner(owner_id, tool_func=log_event, submitted=locals())
    from saltmdb.mcp.identity import SESSION_IDENTITY

    return _backend_or_raise().call(
        "log_event",
        {
            "agent_id": owner_id_,
            "type": event_type,
            "content": content,
            "error_code": error_code,
            "context_id": context_id,
            "session_id": SESSION_IDENTITY.host_session_id,
        },
    )


@mcp.tool()
def search_tags(query: str | None = None, limit: int | None = None) -> list:
    """Queries the database to suggest existing canonical tags matching a search query/substring, to prevent tag fragmentation. Use query='auth' to filter by tag name substring.

    Advisory discovery, not a prerequisite -- tags need not pre-exist; a new domain still
    creates a tag automatically on write. limit caps the number of tags returned (default 50),
    including when query is omitted -- the full canonical tag table is never dumped unbounded."""
    return _backend_or_raise().call(
        "search_tags", {"domain": query, "limit": limit if limit is not None else 50}
    )


@mcp.tool()
def list_predicates(query: str | None = None, limit: int | None = None) -> list:
    """Lists the closed relation-predicate vocabulary manage_relation accepts, optionally
    filtered by a search substring (e.g. query='resolve').

    11 agent-selectable predicates (elaborates_on, related_to, resolves, depends_on, verifies,
    corrects, caused_by, derived_from, distinguishes_from, part_of, contradicts); 3 reserved/
    system-owned predicates (supersedes, consolidated_from, revises) created only by their
    matching lifecycle tool; and similar_to, legacy and read-only. A non-canonical or drifted
    spelling submitted to manage_relation is rejected with a corrected call rather than silently
    accepted, so this list is advisory discovery, not something an agent must memorize.

    limit caps the number of predicates returned (default 50)."""
    return _backend_or_raise().call(
        "list_predicates", {"query": query, "limit": limit if limit is not None else 50}
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

    Exact content-hash duplicates are rejected with the existing entity ID. Near duplicates are
    stored and returned inline as `duplicate_candidates`, with guidance to call
    `supersede_memory` or `consolidate_memories` when the relationship is confirmed.

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
    retrieval_text: str | None = _RETRIEVAL_TEXT_UNSET,
    core_reason: str | None = None,
    core_exit_condition: str | None = None,
    core_review_after: str | None = None,
    detail_memory_ids: list | None = None,
) -> str | dict:
    submitted = locals().copy()
    owner_id_ = _effective_owner(owner_id, tool_func=store_memory, submitted=locals())
    tags_ = _normalize_list_or_str(tags)
    front_matter_fields, body_without_front_matter = _front_matter_identity_fields(content)
    if front_matter_fields:
        from saltmdb.utils.corrected_call import build_corrected_call
        from saltmdb.utils.envelope import error, rejected

        corrected_call = build_corrected_call(
            store_memory,
            submitted,
            {"content": body_without_front_matter},
        )
        return rejected(
            [
                error(
                    "IDENTITY_IN_YAML_FRONT_MATTER",
                    "title and tags belong only in tool parameters; remove them from YAML front matter and retry the corrected call.",
                    "content",
                )
            ],
            corrected_call=corrected_call,
        )
    try:
        # Strict tri-state parse (core-memory governance resolved gap #6): an unrecognized value
        # like "yes" or an integer is rejected outright here, at the adapter boundary, rather
        # than silently coerced to False the way the old `in (True, 1, "true", "1", "True")`
        # membership check did.
        is_core_ = core_governance_service.parse_is_core(is_core)
    except ValueError as e:
        return f"Error: {e}"

    memory_type_ = memory_type
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
            "context_id": context_id,
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


def _predicate_disposition_error(
    predicate: str, field: str
) -> tuple[dict, str | None, bool] | None:
    """Classifies one submitted predicate against the closed vocabulary (plan §5.8) for
    manage_relation's pre-flight gate. Returns None when the predicate is fine as submitted.
    Otherwise returns (error_dict, canonical_or_None, swap): canonical is the mechanically
    derivable replacement for an "alias" disposition (None for reserved/legacy_readonly/unknown,
    since there is nothing manage_relation's own schema can substitute for those)."""
    from saltmdb.utils.predicate_vocabulary import AGENT_SELECTABLE_PREDICATES, classify_predicate

    disposition = classify_predicate(predicate)
    if disposition.status == "selectable":
        return None
    if disposition.status == "reserved":
        return (
            {
                "code": "RESERVED_PREDICATE",
                "message": (
                    f"predicate '{predicate}' is reserved; it is created only by "
                    f"{disposition.lifecycle_tool}, never directly via manage_relation."
                ),
                "field": field,
            },
            None,
            False,
        )
    if disposition.status == "legacy_readonly":
        return (
            {
                "code": "LEGACY_READONLY_PREDICATE",
                "message": (
                    f"predicate '{predicate}' is legacy and read-only; existing edges remain "
                    "readable but no new ones may be created."
                ),
                "field": field,
            },
            None,
            False,
        )
    if disposition.status == "alias":
        return (
            {
                "code": "NONCANONICAL_PREDICATE",
                "message": (
                    f"predicate '{predicate}' is not canonical; the canonical form is "
                    f"'{disposition.canonical}'"
                    + (" with source_id/target_id swapped" if disposition.swap else "")
                    + "."
                ),
                "field": field,
            },
            disposition.canonical,
            disposition.swap,
        )
    return (
        {
            "code": "UNKNOWN_PREDICATE",
            "message": (
                f"predicate '{predicate}' is not part of the closed predicate vocabulary. "
                f"Valid predicates: {sorted(AGENT_SELECTABLE_PREDICATES)}."
            ),
            "field": field,
        },
        None,
        False,
    )


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
) -> str | list | dict:
    """Stores one or multiple directional semantic relationship edges between memory nodes, or invalidates an existing edge (invalidate=True).

    `predicate` must be one of the 11 agent-selectable closed-vocabulary predicates
    (elaborates_on, related_to, resolves, depends_on, verifies, corrects, caused_by,
    derived_from, distinguishes_from, part_of, contradicts) -- see `list_predicates`. A drifted
    spelling is rejected with a `corrected_call` using the canonical name (and source_id/
    target_id swapped, when the drift reversed direction); `supersedes`/`consolidated_from`/
    `revises` are reserved and rejected, naming the lifecycle tool that creates them instead
    (`supersede_memory`/`consolidate_memories`/`revise_memory`); `similar_to` is legacy and
    read-only. This gate applies only to creating a new edge, never to `invalidate=True`.

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
    declaration (via `store_memory`/`consolidate_memories`) may create one. Re-submitting an
    edge that already exists stays an idempotent no-op regardless.
    """
    submitted = locals().copy()
    owner_id_ = _effective_owner(owner_id, tool_func=manage_relation, submitted=locals())
    relations_ = relations
    if relations_ and isinstance(relations_, str):
        relations_ = _normalize_list_or_str(relations_)

    if relations_:
        from saltmdb.utils.corrected_call import build_corrected_call
        from saltmdb.utils.envelope import error as env_error
        from saltmdb.utils.envelope import rejected

        errors: list[dict] = []
        corrected_items: list = []
        can_fully_correct = True
        for idx, item in enumerate(relations_):
            item_predicate = item.get("predicate") if isinstance(item, dict) else None
            check = (
                _predicate_disposition_error(item_predicate, f"relations[{idx}].predicate")
                if item_predicate
                else None
            )
            if check is None:
                corrected_items.append(item)
                continue
            error_entry, canonical, swap = check
            errors.append(error_entry)
            if canonical is not None:
                fixed_item = dict(item)
                fixed_item["predicate"] = canonical
                if swap:
                    fixed_item["source_id"] = item.get("target_id")
                    fixed_item["target_id"] = item.get("source_id")
                corrected_items.append(fixed_item)
            else:
                can_fully_correct = False
                corrected_items.append(item)

        if errors:
            corrected_call = (
                build_corrected_call(manage_relation, submitted, {"relations": corrected_items})
                if can_fully_correct
                else None
            )
            return rejected(
                [env_error(e["code"], e["message"], e.get("field")) for e in errors],
                corrected_call=corrected_call,
            )
    elif not invalidate and predicate:
        check = _predicate_disposition_error(predicate, "predicate")
        if check is not None:
            from saltmdb.utils.corrected_call import build_corrected_call
            from saltmdb.utils.envelope import error as env_error
            from saltmdb.utils.envelope import rejected

            error_entry, canonical, swap = check
            corrected_call = None
            if canonical is not None:
                fixes: dict[str, Any] = {"predicate": canonical}
                if swap:
                    fixes["source_id"] = target_id
                    fixes["target_id"] = source_id
                corrected_call = build_corrected_call(manage_relation, submitted, fixes)
            return rejected(
                [env_error(error_entry["code"], error_entry["message"], error_entry.get("field"))],
                corrected_call=corrected_call,
            )

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
def consolidate_memories(
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
    """Creates a canonical memory from two or more explicit parents.

    The parents are archived unchanged and linked with ``consolidated_from``. Semantic
    relations are never repointed; the response may include an optional orphaned-edge worklist
    whose items are safe, optional follow-up declarations. The new memory's identity is
    immutable after creation.

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
    owner_id_ = _effective_owner(owner_id, tool_func=consolidate_memories, submitted=locals())
    consolidations_ = consolidations
    if consolidations_ and isinstance(consolidations_, str):
        consolidations_ = _normalize_list_or_str(consolidations_)

    parent_ids_ = _normalize_list_or_str(parent_ids)
    tags_ = _normalize_list_or_str(tags)
    detail_memory_ids_ = (
        _normalize_list_or_str(detail_memory_ids) if detail_memory_ids is not None else None
    )

    return _backend_or_raise().call(
        "consolidate_memories",
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


# Python-level compatibility for callers migrating from the pre-Phase-4 adapter.  This alias is
# deliberately not decorated, so ``commit_consolidation`` cannot be invoked as a public MCP tool.
commit_consolidation = consolidate_memories


def _replacement_payload(
    *,
    entity_id: str,
    title: str,
    content: str,
    tags: list[str],
    reason: str,
    owner_id: str | None,
    context_id: str | None,
    scope: Literal["private", "shared"] | None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None,
) -> dict:
    """Build the common replacement request without hidden aliases or front matter parsing."""
    return {
        "entity_id": entity_id,
        "title": title,
        "content": content,
        "tags": _normalize_list_or_str(tags),
        "reason": reason,
        "owner_id": owner_id,
        "context_id": context_id,
        "scope": scope,
        "memory_type": memory_type,
    }


@mcp.tool()
def revise_memory(
    entity_id: str,
    title: str,
    content: str,
    tags: list[str],
    reason: str,
    owner_id: str | None = None,
    context_id: str | None = None,
    scope: Literal["private", "shared"] | None = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
) -> dict | str:
    """Repairs a deficient memory representation using a new immutable entity ID.

    ``entity_id`` is never mutated in place. The predecessor is archived byte-for-byte and the
    new entity links to it with ``revises``. An inactive target is a hard failure: inspect the
    reported successor before retrying. ``owner_id``, ``context_id``, ``scope``, ``memory_type``,
    are inherited when omitted and may be changed deliberately when supplied.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=revise_memory, submitted=locals())
    return _backend_or_raise().call(
        "revise_memory",
        _replacement_payload(
            entity_id=entity_id,
            title=title,
            content=content,
            tags=tags,
            reason=reason,
            owner_id=owner_id_,
            context_id=context_id,
            scope=scope,
            memory_type=memory_type,
        ),
    )


@mcp.tool()
def supersede_memory(
    entity_id: str,
    title: str,
    content: str,
    tags: list[str],
    reason: str,
    owner_id: str | None = None,
    context_id: str | None = None,
    scope: Literal["private", "shared"] | None = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
) -> dict | str:
    """Replaces valid knowledge with newer knowledge using a new immutable entity ID.

    The predecessor remains byte-identical and is linked with ``supersedes``. An inactive target
    is never silently redirected; the error reports known active successors and lineage. Optional
    administrative fields are inherited unless explicitly supplied.
    """
    owner_id_ = _effective_owner(owner_id, tool_func=supersede_memory, submitted=locals())
    return _backend_or_raise().call(
        "supersede_memory",
        _replacement_payload(
            entity_id=entity_id,
            title=title,
            content=content,
            tags=tags,
            reason=reason,
            owner_id=owner_id_,
            context_id=context_id,
            scope=scope,
            memory_type=memory_type,
        ),
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
    context_id: str | None = None,
    agent_id: str | None = None,
    event_type: str | None = None,
    session_id: str | None = None,
    order: Literal["newest_first", "oldest_first"] = "newest_first",
    limit: int | None = None,
    offset: int | None = None,
) -> list:
    """Retrieves events from the append-only events ledger, for multi-agent coordination and
    wrap-up thread review.

    `context_id` is the headline filter -- reading back every event logged under a shared
    thread handle, survivable across a power cut. `agent_id` filters to one agent's events, for
    "what did the other agent just decide" in a multi-agent DB (this tool has no notion of "my
    own events" the way log_event has a bound owner -- it is a read across the whole ledger,
    narrowed by whichever filters are supplied). `session_id` filters to one host harness
    session (an opaque, advisory pointer set by log_event automatically; agents never set it,
    only pass one back here if they already have it from elsewhere).

    `order`: "newest_first" (default, for discovery) or "oldest_first" (for chronological
    wrap-up synthesis) -- always explicit, never inferred from which filter was passed.
    """
    return _backend_or_raise().call(
        "get_events",
        {
            "context_id": context_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "session_id": session_id,
            "order": order,
            "limit": limit if limit is not None else 20,
            "offset": offset if offset is not None else 0,
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
