from typing import Any, Literal, cast
from typing_extensions import TypedDict

import json
import logging
import re
from saltmdb.mcp.server import mcp
from saltmdb.daemon import client as daemon_client
from saltmdb.daemon import protocol

# core_governance_service.parse_is_core is a pure/stateless helper (no DB access), safe to call
# from the adapter process directly -- same category as saltmdb.utils.corrected_call/envelope.
from saltmdb.domain.services import core_governance_service

logger = logging.getLogger(__name__)


class RelationBatchItem(TypedDict, total=False):
    source_id: str
    target_id: str
    predicate: str
    valid_at: str | None
    invalidate: bool
    invalid_at: str | None
    override_justification: str | None


class ConsolidationBatchItem(TypedDict, total=False):
    parent_ids: list[str]
    title: str
    content: str
    is_core: bool | None
    tags: list[str] | None
    scope: Literal["private", "shared"] | None
    override_justification: str | None


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


def _resolve_content(
    content: str | None, content_file_path: str | None
) -> tuple[str | None, dict | None]:
    """Resolve exactly one of ``content`` (inline) or ``content_file_path`` (read from local disk)
    into a plain content string, at the adapter boundary -- before any domain-service call, so
    every existing content-handling path downstream (front-matter detection, redaction, quality
    gating) is unaffected by which form the caller used. Mirrors this environment's own
    file_path-vs-inline-data convention for large payloads.

    Returns ``(resolved_content, None)`` on success, or ``(None, <rejected() envelope>)`` when
    neither or both were supplied, or the file could not be read.
    """
    from saltmdb.utils.envelope import error, rejected

    if content is not None and content_file_path is not None:
        return None, rejected(
            [
                error(
                    "CONTENT_AND_FILE_PATH_BOTH_SET",
                    "Provide exactly one of content or content_file_path, not both.",
                    "content",
                )
            ]
        )
    if content is None and content_file_path is None:
        return None, rejected(
            [error("MISSING_CONTENT", "Provide content or content_file_path.", "content")]
        )
    if content_file_path is None:
        return content, None
    try:
        with open(content_file_path, encoding="utf-8") as f:
            return f.read(), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, rejected(
            [
                error(
                    "CONTENT_FILE_READ_FAILED",
                    f"Could not read content_file_path '{content_file_path}': {exc}",
                    "content_file_path",
                )
            ]
        )


def _strip_item_owner_id(items: list) -> list:
    """Remove a stray/attacker-supplied ``owner_id`` key from each bulk item before it crosses
    the adapter boundary (manage_relation's ``relations`` and consolidate_memories's
    ``consolidations``, review finding 2026-08-26: was duplicated inline at both call sites).
    Non-dict items are passed through unchanged -- the caller's own validation rejects those."""
    return [
        {key: value for key, value in item.items() if key != "owner_id"}
        if isinstance(item, dict)
        else item
        for item in items
    ]


def _effective_owner() -> str:
    """Return the startup-configured adapter identity for internal dispatch."""
    from saltmdb.mcp.identity import SESSION_IDENTITY

    if SESSION_IDENTITY.owner_id:
        return SESSION_IDENTITY.owner_id
    raise ValueError(
        "SALTMDB owner identity is not configured; set SALTMDB_OWNER_ID in the MCP server "
        "environment and restart the MCP server."
    )


_OWNER_INJECTED_TOOLS = frozenset(
    {
        # NOTE: log_event is deliberately absent. Its own tools.py wrapper already binds
        # ownership via `agent_id = _effective_owner()` before backend.call() is ever
        # invoked, so it never emits an `owner_id` key for this re-assertion to guard --
        # and event_service.log_event()'s signature has no owner_id parameter and no
        # **kwargs catch-all, so injecting one here raised TypeError on every call in
        # production (2026-08-26 live outage, v0.1.0-alpha.87). Do not re-add it without
        # also adding an owner_id parameter to event_service.log_event.
        "store_memory",
        "search_memory",
        "archive_memory",
        "manage_relation",
        "consolidate_memories",
        "revise_memory",
        "supersede_memory",
        "get_memory",
        "inspect_memory",
        "get_lineage",
        "get_related_memories",
        "review_core_memory",
        "retrieve_context",
    }
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

        # The adapter is the trust boundary for owner identity.  Public wrappers already add
        # this field for the daemon, but re-assert it here so an internal caller (or a stale
        # wrapper) cannot smuggle a different owner through the transport envelope.  Tools whose
        # contract is intentionally cross-agent/ownership-neutral must not receive an owner key.
        if tool_name in _OWNER_INJECTED_TOOLS:
            kwargs = {**kwargs, "owner_id": _effective_owner()}
        else:
            kwargs = {key: value for key, value in kwargs.items() if key != "owner_id"}

        if tool_name in {
            "log_event",
            "store_memory",
            "consolidate_memories",
            "revise_memory",
            "supersede_memory",
            "update_memory_metadata",
        }:
            kwargs = {**kwargs, "agent_session_id": SESSION_IDENTITY.agent_session_id}

        # Transport metadata, consumed by daemon/server.py before ordinary tool dispatch.  This
        # intentionally differs from public agent_session_id filters on search/event tools.
        db_path = get_db_path()
        try:
            return daemon_client.call(
                db_path,
                tool_name,
                kwargs,
                caller_agent_session_id=SESSION_IDENTITY.agent_session_id,
            )
        except daemon_client.DaemonRpcError as e:
            if e.code == "MID_CALL_FAILURE":
                if tool_name in protocol.READ_TOOLS:
                    return daemon_client.call(
                        db_path,
                        tool_name,
                        kwargs,
                        caller_agent_session_id=SESSION_IDENTITY.agent_session_id,
                    )
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
) -> dict:
    """Appends one entry to the append-only, immutable events ledger -- for logging your own
    decisions/issues/fixes/attempts in real time, and for multi-agent coordination via a shared
    context_id thread. Not for storing durable knowledge: a fact/decision/procedure worth
    finding again later belongs in store_memory instead; log_event is a lightweight, append-only
    trace, not indexed for semantic search the way memories are.

    Required: event_type (a short free-text label, e.g. "decision"/"issue"/"fix"/"attempt" --
    not schema-constrained, but consistent labels make get_events' event_type filter useful) and
    content (the event body; PII/secrets are scrubbed server-side before storage). Optional:
    context_id (a shared thread handle so another agent's get_events(context_id=...) reads back
    this same event) and error_code (a short machine-readable label for an `issue`-type event,
    read back by get_events, not validated against any enum).

    You are never asked for agent_id or agent_session_id -- both are bound automatically from
    the calling adapter's own configured identity and process session; get_events uses agent_id
    as a read-side FILTER for "what did another agent log," which is why this write-side tool has
    no equivalent parameter of its own.

    Returns `{"status": "ok", "data": {"id": <event id>, "message": "..."}, "warnings": []}` on
    success. A failure (an unexpected server-side fault -- there are no caller-input validation
    checks on this tool beyond the two required parameters, enforced by the schema itself)
    returns `{"status": "rejected", "errors": [{"code": "INTERNAL_ERROR", "message": "..."}],
    "warnings": []}`; check `result["status"]` before trusting `result["data"]`.

    Example: `log_event(event_type="decision", content="Chose X over Y because...",
    context_id="my-session-thread")`.
    """
    owner_id_ = _effective_owner()
    return _backend_or_raise().call(
        "log_event",
        {
            "agent_id": owner_id_,
            "type": event_type,
            "content": content,
            "error_code": error_code,
            "context_id": context_id,
        },
    )


@mcp.tool()
def search_tags(query: str | None = None, limit: int | None = None) -> dict:
    """Looks up existing canonical tags by a name substring, to avoid creating a near-duplicate
    tag (e.g. searching before inventing a new one). Advisory only, never required before a
    write: store_memory/revise_memory/etc. auto-create any tag that doesn't already exist.

    query (optional) filters by substring, e.g. query="auth" matches "#auth-flow". Omitting it
    lists canonical tags up to limit (default 50) -- never an unbounded dump.

    Returns `{"status": "ok", "data": [{"id": ..., "name": ...}, ...], "warnings": []}` --
    `data` is `[]`, not an error, when nothing matches. A genuine fault returns `{"status":
    "rejected", "errors": [{"code": "INTERNAL_ERROR", "message": "..."}]}`.

    Example: `search_tags(query="auth")` before tagging a new memory `#auth-refactor`, to check
    whether `#auth`/`#authentication` already exists as the canonical form.
    """
    return _backend_or_raise().call(
        "search_tags", {"domain": query, "limit": limit if limit is not None else 50}
    )


@mcp.tool()
def list_predicates(query: str | None = None, limit: int | None = None) -> dict:
    """Lists the closed relation-predicate vocabulary manage_relation accepts, each tagged with
    its own disposition -- so you can tell a predicate you may use from one you may only read,
    without memorizing the fixed lists below. Advisory discovery, not a prerequisite: submitting
    an unrecognized or drifted predicate spelling to manage_relation is rejected with a
    corrected_call using the right one, so you don't have to call this tool first.

    query (optional) filters by substring, e.g. query="resolve" matches "resolves". limit caps
    results (default 50).

    Returns `{"status": "ok", "data": [{"id", "name", "disposition", "lifecycle_tool"}, ...]}`.
    `disposition` is one of `"selectable"` (pass it to manage_relation freely), `"reserved"`
    (created only by the tool named in `lifecycle_tool` -- e.g. `supersedes` by supersede_memory,
    `consolidated_from` by consolidate_memories, `revises` by revise_memory -- manage_relation
    rejects a direct attempt), or `"legacy_readonly"` (existing edges remain visible; no new ones
    may be created -- currently only `similar_to`). This tool's own query only ever returns
    canonical predicates, so `disposition` is never `"alias"` here even though manage_relation's
    own drift-correction logic recognizes aliases too -- an alias spelling isn't itself a row in
    the canonical table this tool lists.

    A fault returns `{"status": "rejected", "errors": [{"code": "INTERNAL_ERROR", ...}]}`.

    Example: `list_predicates(query="depend")` to confirm `depends_on` is the right spelling
    before calling `manage_relation(predicate="depends_on", ...)`.
    """
    return _backend_or_raise().call(
        "list_predicates", {"query": query, "limit": limit if limit is not None else 50}
    )


@mcp.tool()
def merge_tags(
    keep_tag: str | None = None,
    tags_to_merge: list | str | None = None,
) -> dict:
    """Merges one or more fragmented/synonym tags into a single canonical tag, repointing every
    affected memory's tag associations. Use when you've noticed folksonomy drift -- e.g. some
    memories tagged '#doc', others '#documentation' -- and want one canonical spelling going
    forward.

    keep_tag: the canonical tag every merged tag repoints to; it must already exist. tags_to_merge:
    one or more existing tag names/IDs to fold into keep_tag (accepts a list or a
    comma-separated string).

    Returns `{"status": "ok", "data": {"merged": [...], "skipped": [...], "canonical_tag":
    keep_tag}}` on success -- `skipped` lists any requested tag that didn't exist or was already
    canonical, without failing the whole call. `{"status": "rejected", "errors": [{"code":
    "NOT_FOUND", "message": "...", "field": "keep_tag"}]}` when keep_tag itself doesn't exist --
    search_tags first if you're not certain of the exact existing spelling.

    Example: `merge_tags(keep_tag="#docs", tags_to_merge=["#doc", "#documentation"])`.
    """
    tags_to_merge_ = _normalize_list_or_str(tags_to_merge)
    return _backend_or_raise().call(
        "merge_tags", {"keep_tag": keep_tag, "tags_to_merge": tags_to_merge_}
    )


@mcp.tool(
    description="""Creates a new long-term memory (a fact/event/procedure/decision/preference),
    or performs a metadata-only update to an existing one via entity_id. For CORRECTING or
    REPLACING an existing memory's content, use revise_memory/supersede_memory instead -- this
    tool's entity_id path only ever touches metadata/tags/governance fields, never content.

    Required: title, and exactly one of content or content_file_path (a local file path SALTMDB
    reads server-side -- use this instead of inlining a large body to avoid round-tripping it
    through your own context). memory_type classifies the memory (default 'fact'; omitted on an
    entity_id update, the existing value is preserved): fact (durable general knowledge), event
    (something that happened), procedure (how-to/runbook), decision (ADR-style rationale),
    preference (a durable user/agent preference).

    entity_id (optional) targets an existing memory for a metadata-only update -- re-tagging,
    updating `metadata` (shallow-merged, existing keys not mentioned are preserved), or
    backfilling core-governance fields. `content`/`content_file_path` is still mandatory on this
    path too (re-supply the unchanged body; omitting both is rejected with `MISSING_CONTENT`
    regardless of entity_id) -- use update_memory_metadata instead for a metadata-only edit that
    doesn't require restating title/content unchanged.

    Returns `{"status": "ok", "data": {"id": ..., "duplicate_candidates": [...] | None, ...},
    "warnings": [...]}` on success. Rejections you'll see in practice: an exact content-hash
    duplicate (`errors[0].code == "REJECT_EXACT_DUPLICATE"`, naming the existing entity -- a near
    duplicate is NOT rejected, it stores and returns `duplicate_candidates` inline instead, with
    guidance to call supersede_memory/consolidate_memories once you've confirmed the
    relationship, or manage_relation to link without merging); `title`/`tags` found inside YAML
    front matter in `content` (`IDENTITY_IN_YAML_FRONT_MATTER`, with a ready-to-resubmit
    `corrected_call`); neither or both of `content`/`content_file_path` supplied
    (`MISSING_CONTENT`/`CONTENT_AND_FILE_PATH_BOTH_SET`); an unreadable/non-UTF-8
    `content_file_path` (`CONTENT_FILE_READ_FAILED`).

    Example: `store_memory(title="[Project] Decision to use X", content="...", tags=["arch"],
    memory_type="decision")`.

    **Core-memory bootstrap governance** (`is_core`): a SCARCE, TEMPORARY mechanism for urgent
    cross-session hazards an agent must know before it could reasonably search for them -- not a
    general "important knowledge" tier (stable rules/preferences belong in AGENTS.md/CLAUDE.md/
    skills instead). `is_core=True` requires `scope="shared"` plus `core_reason`/
    `core_exit_condition` (20-500 chars each) and admits three hard caps: at most 5 active cores
    globally, 2,500 chars of content each, and a 15,000-char rendered bootstrap digest. A capacity
    failure returns `{"status": "rejected", "errors": [{"code": "CORE_CAPACITY_EXCEEDED", ...}],
    "violated_dimensions": [...], "limits": {...}, ...}` with a balanced inventory of every active
    core (no full content) and zero side effects -- rebalance (demote/archive/shorten/consolidate)
    and retry; this never needs a human decision. `core_review_after` defaults to 14 days out, max
    30; while any core is overdue, creating/promoting/enlarging a core or changing its review date
    is blocked (use review_core_memory first). Omitting `core_reason`/`core_exit_condition`/
    `core_review_after`/`detail_memory_ids` on an already-core update preserves existing values;
    supplying any of them on an effectively non-core write is rejected. `detail_memory_ids` (at
    most 3 UUIDs of existing shared, non-core memories whose title+UUID must appear in `content`)
    atomically maintains `elaborates_on` edges into this core -- `None` preserves the current
    declaration, `[]` clears it. `'#core'` in `tags` is a derived label the server auto-maintains
    from `is_core`; setting it directly is silently overridden.
    """
)
def store_memory(
    title: str,
    content: str | None = None,
    tags: list[str] = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
    context_id: str | None = None,
    entity_id: str | None = None,
    metadata: dict | None = None,
    is_core: bool | None = None,
    scope: Literal["private", "shared"] = "shared",
    retrieval_text: str | None = _RETRIEVAL_TEXT_UNSET,
    core_reason: str | None = None,
    core_exit_condition: str | None = None,
    core_review_after: str | None = None,
    detail_memory_ids: list | None = None,
    content_file_path: str | None = None,
) -> dict:
    content, _content_error = _resolve_content(content, content_file_path)
    if _content_error is not None:
        return _content_error
    content = cast(str, content)
    submitted = locals().copy()
    owner_id_ = _effective_owner()
    tags_ = _normalize_list_or_str(tags)
    front_matter_fields, body_without_front_matter = _front_matter_identity_fields(content)
    if front_matter_fields:
        from saltmdb.utils.corrected_call import build_corrected_call
        from saltmdb.utils.envelope import error, rejected

        corrected_call = build_corrected_call(
            store_memory,
            submitted,
            # content_file_path must be cleared here too: `submitted` still carries the caller's
            # original (non-None) content_file_path alongside the now-resolved `content`, and
            # build_corrected_call only drops None-valued fields -- without this override the
            # corrected_call would set both fields and immediately fail
            # CONTENT_AND_FILE_PATH_BOTH_SET if pasted back verbatim.
            {"content": body_without_front_matter, "content_file_path": None},
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
    from saltmdb.utils import error_codes
    from saltmdb.utils.envelope import error, rejected

    try:
        # Strict tri-state parse (core-memory governance resolved gap #6): an unrecognized value
        # like "yes" or an integer is rejected outright here, at the adapter boundary, rather
        # than silently coerced to False the way the old `in (True, 1, "true", "1", "True")`
        # membership check did.
        is_core_ = core_governance_service.parse_is_core(is_core)
    except ValueError as e:
        return rejected([error(error_codes.VALIDATION_ERROR, str(e), "is_core")])

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
            "metadata": metadata,
            "retrieval_text": retrieval_text_,
            "retrieval_text_provided": retrieval_text_provided,
            "core_reason": core_reason,
            "core_exit_condition": core_exit_condition,
            "core_review_after": core_review_after,
            "detail_memory_ids": detail_memory_ids_,
        },
    )


@mcp.tool(
    description="""Find individual memories matching a query or filters -- for locating a
    specific record, a duplicate check before storing, or historical/exploratory search. For
    task context assembled from matches AND their graph connections/lifecycle/conflicts in one
    call, use retrieve_context instead; avoid calling both with the same query, since
    retrieve_context already runs this search internally.

    query_keywords is the search text (omit it to browse by tags_filter/context_id/etc. alone).
    mode controls result filtering: "broad" (default) is ordinary hybrid ranking; "strict"
    resolves superseded matches and abstains (returns []) rather than return a weakly-relevant
    result; "history" keeps superseded results visible, labeled. tags_filter/context_id/
    memory_type_filter/is_core narrow the search; limit caps result count (default 5); cursor
    pages through a larger result set (there is no separate more-pages-remaining or total-count
    signal today -- an empty next page is your only current indicator that pagination is
    exhausted).

    Returns a bare list of memory dicts -- NOT the `{"status": ..., "data": ...}` envelope shape
    other SALTMDB tools use; each list item has its own fields (id, title, score, snippet, etc.)
    directly at the top level. An internal fault returns a one-item list `[{"error": "..."}]`
    rather than raising -- check for an `"id"` key's absence to detect this rare case, since it is
    the one property that reliably distinguishes it from every real result item.

    A result item may carry `relevance_preview`/`relevance_preview_meta` (a hint for whether to
    call get_memory, never a substitute for it -- absence of a detail from the preview is not
    evidence it's absent from the memory) or `drift_flag` (advisory only, re-verify the citation
    yourself rather than trusting either the flag or the original claim).

    Example: `search_memory(query_keywords="DNS incident", mode="broad", limit=5)`.
    """
)
def search_memory(
    query_keywords: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    context_id: str | None = None,
    agent_session_id: str | None = None,
    tags_filter: list[str] | None = None,
    memory_type_filter: Literal["fact", "event", "procedure", "decision", "preference"]
    | None = None,
    is_core: bool | None = None,
    include_related: bool | None = None,
    mode: Literal["strict", "broad", "history"] | None = None,
) -> list:
    owner_id_ = _effective_owner()
    tags_filter_ = _normalize_list_or_str(tags_filter) if tags_filter else None

    return _backend_or_raise().call(
        "search_memory",
        {
            "owner_id": owner_id_,
            "query_keywords": query_keywords,
            "tags_filter": tags_filter_,
            "limit": limit if limit is not None else 5,
            "context_id": context_id,
            "agent_session_id": agent_session_id,
            "is_core": is_core,
            "memory_type_filter": memory_type_filter,
            "cursor": cursor,
            "mode": mode if mode is not None else "broad",
            "include_related": include_related if include_related is not None else True,
        },
    )


@mcp.tool()
def archive_memory(entity_id: str | list[str] | None = None) -> dict | list:
    """Archives (retires) one or more memories -- the reversible-in-spirit-but-not-in-practice
    lifecycle action for a memory that's no longer worth surfacing in ordinary search, without
    deleting its history: an archived memory stays fully visible via get_memory/get_lineage, and
    is excluded from search_memory's normal ranking (but not from `mode="history"`).

    Works uniformly on core and non-core memories, with no rationale required -- `is_core` is
    deliberately never cleared by archiving (only review_core_memory's `demote` outcome clears
    it), so `is_core=1` on an archived row is an intentional "was once core" signal, not
    staleness. For a deliberate, audited core-memory review with a mandatory rationale, use
    review_core_memory(outcome="archive") instead (it requires the target to be an active core).

    entity_id accepts a single ID (or unambiguous ID prefix) OR a list of IDs. **The return
    shape depends on which you pass**: a single ID (or omitted) returns
    `{"status": "ok", "data": {"id": ..., "message": "..."}, "warnings": [...]}`, with an
    `ALREADY_DONE`-coded warning (not a rejection) if it was already archived. A LIST of IDs --
    even a single-item list -- instead returns a bare list of `{"status": "success"/"error",
    "entity_id": ..., "result": ...}` dicts, one per requested ID, run all-or-nothing in one
    transaction (any single item's failure rolls back the whole batch, so a partial-success list
    is never returned -- a batch failure instead returns a single-item list,
    `[{"status": "error", "error": "..."}]`).

    A single-ID call's failure: `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR" |
    "NOT_FOUND" | "CONFLICT", "message": "...", "field": "entity_id" | "owner_id"}]}` --
    `NOT_FOUND` for an unresolvable ID, `CONFLICT` for an owner mismatch.

    Example, single: `archive_memory(entity_id="abc123")`. Example, bulk: `archive_memory
    (entity_id=["abc123", "def456"])` -- note the list wrapper changes the return shape as
    described above, not just the count of things archived.
    """
    owner_id_ = _effective_owner()
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
    relations: list[RelationBatchItem] | None = None,
    source_id: str | None = None,
    target_id: str | None = None,
    predicate: str | None = None,
    invalidate: bool = False,
    valid_at: str | None = None,
    invalid_at: str | None = None,
    override_justification: str | None = None,
) -> dict | list:
    """Creates a directional semantic edge between two memories (source_id -> predicate ->
    target_id), or invalidates one (invalidate=True). For a batch of edges in one call, use
    relations instead of source_id/target_id/predicate.

    predicate must be one of 11 agent-selectable values -- see list_predicates for the current
    list and each one's meaning; a drifted/non-canonical spelling is rejected with a
    corrected_call using the right one, so calling list_predicates first is optional, not
    required. Re-submitting an edge that already exists is an idempotent no-op, not an error.

    **Return shape depends on which form you use**: single-edge (source_id/target_id/predicate)
    returns `{"status": "ok", "data": {"relation_id": ..., "message": "..."}, "warnings":
    [...]}` -- an `ALREADY_DONE`-coded warning, not a rejection, for the idempotent-duplicate
    case. A `relations` BATCH instead returns a bare list of `{"status": "success"/"duplicate",
    "source", "target", "predicate", "action", "result"}` dicts, one per item, all-or-nothing
    (any item's failure aborts the whole batch; a batch failure returns a single-item list,
    `[{"status": "error", "error": "..."}]`, not a partial-success list).

    Rejections you may see (either form): `{"status": "rejected", "errors": [{"code":
    "RESERVED_PREDICATE" | "LEGACY_READONLY_PREDICATE" | "NONCANONICAL_PREDICATE" |
    "UNKNOWN_PREDICATE" | "REJECT_LOW_RELATION_SIMILARITY" | "REJECT_CONTRADICTORY_PREDICATE" |
    "REJECT_CORE_ELABORATES_ON", "message": "...", "field": "..."}], "corrected_call": {...} |
    None}` -- `corrected_call`, when present, is a ready-to-resubmit call with the fix already
    applied (e.g. the canonical predicate name, source/target swapped if the drift reversed
    direction). `RESERVED_PREDICATE` names the lifecycle tool that actually creates that edge
    (`supersede_memory`/`consolidate_memories`/`revise_memory` for `supersedes`/
    `consolidated_from`/`revises`).

    Example: `manage_relation(source_id="abc", target_id="def", predicate="depends_on")`.

    Two rejections are governance gates, not schema errors, and both accept
    override_justification (a non-throwaway explanation) to force the edge through anyway, on
    each affected item for a batch call: `REJECT_LOW_RELATION_SIMILARITY` (a "strong" predicate
    -- elaborates_on/resolves/supersedes -- whose source/target embeddings are too dissimilar)
    and `REJECT_CONTRADICTORY_PREDICATE` (a contradictory predicate pair already exists on this
    directional edge). `REJECT_CORE_ELABORATES_ON` has no override: a new `elaborates_on` edge
    targeting an active core memory can only be created via that core's own `detail_memory_ids`
    declaration (store_memory/consolidate_memories), never directly.
    """
    submitted = locals().copy()
    owner_id_ = _effective_owner()
    relations_ = relations
    if relations_ and isinstance(relations_, str):
        relations_ = _normalize_list_or_str(relations_)

    if relations_:
        relations_ = _strip_item_owner_id(relations_)
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
    consolidations: list[ConsolidationBatchItem] | None = None,
    parent_ids: list | None = None,
    title: str | None = None,
    content: str | None = None,
    tags: list | None = None,
    context_id: str | None = None,
    scope: Literal["private", "shared"] = "shared",
    weight: int | float = 1,
    is_core: bool | None = None,
    override_justification: str | None = None,
    core_reason: str | None = None,
    core_exit_condition: str | None = None,
    core_review_after: str | None = None,
    detail_memory_ids: list | None = None,
) -> dict | list:
    """Merges two or more existing memories into one new canonical memory -- the parents are
    archived unchanged and linked via `consolidated_from`; the new memory's own identity is
    immutable afterward. For a batch of consolidations in one call, use `consolidations` instead
    of `parent_ids`/`title`/`content`.

    parent_ids (2+ existing memory IDs), title, content are required for a single consolidation.
    Semantic relations pointing at a parent are never auto-repointed onto the new memory; the
    response's `orphaned_relations`/`orphaned_edge_worklist` lists them as safe, optional
    follow-up `manage_relation` declarations you may choose to re-create.

    **Return shape depends on which form you use** (same pattern as `manage_relation`/
    `archive_memory`): a single consolidation returns `{"status": "ok", "data": {"entity_id":
    ..., "message": "...", "orphaned_relations": [...], "worklist_guidance": "..."}, "warnings":
    [...]}`. A `consolidations` BATCH instead returns a bare list of per-item result dicts,
    all-or-nothing (one item's failure aborts the whole batch).

    Rejections: `{"status": "rejected", "errors": [{"code": "REJECT_LOW_COHESION" | ..., ...}]}`
    -- `REJECT_LOW_COHESION` (the parent set's chunk-embedding centroids are too dissimilar to be
    one coherent memory) accepts `override_justification` (a non-throwaway explanation, baked
    into the committed content and audited) to force it through; for a batch, put
    `override_justification` on each item that needs it, never at the top level.

    Example: `consolidate_memories(parent_ids=["id1", "id2"], title="[Topic] Merged decision",
    content="...")`.

    `context_id` is the batch-wide default for the bulk shape; an item's own `context_id` wins.
    Core-memory governance: `is_core` is never inherited from parents -- if any resolved parent is
    an active core and `is_core` is omitted, the commit is rejected with an actionable error; pass
    `is_core=True` (with `core_reason`/`core_exit_condition`) to keep the result core, or
    `is_core=False` to let it become ordinary. Same capacity caps and `detail_memory_ids` rules as
    store_memory. For the bulk shape, put every `core_*`/`detail_memory_ids` field on each item.
    """
    owner_id_ = _effective_owner()
    consolidations_ = consolidations
    if consolidations_ and isinstance(consolidations_, str):
        consolidations_ = _normalize_list_or_str(consolidations_)
    if consolidations_:
        consolidations_ = _strip_item_owner_id(consolidations_)

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
    content: str | None,
    tags: list[str] | None,
    reason: str | None,
    owner_id: str | None,
    context_id: str | None,
    scope: Literal["private", "shared"] | None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None,
    repoint_relations: bool = False,
) -> dict:
    """Build the common replacement request without hidden aliases or front matter parsing."""
    return {
        "entity_id": entity_id,
        "title": title,
        "content": content,
        "tags": None if tags is None else _normalize_list_or_str(tags),
        "reason": reason,
        "owner_id": owner_id,
        "context_id": context_id,
        "scope": scope,
        "memory_type": memory_type,
        "repoint_relations": repoint_relations,
    }


@mcp.tool()
def revise_memory(
    entity_id: str,
    title: str,
    content: str | None = None,
    tags: list[str] = None,
    reason: str = None,
    context_id: str | None = None,
    scope: Literal["private", "shared"] | None = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
    repoint_relations: bool = False,
    content_file_path: str | None = None,
) -> dict:
    """Corrects a memory whose *representation* was flawed (wrong wording, an error you made
    recording it, a wrong unit) -- content changes, but the underlying fact/decision does not. Use
    supersede_memory instead when the real-world fact/decision itself has changed;
    update_memory_metadata for a metadata-only edit needing no content restatement.

    entity_id (the active memory to revise), title, and exactly one of content/content_file_path
    are required. tags, context_id, scope, and memory_type are all optional and INHERITED from
    the predecessor when omitted -- pass an explicit value only to change it, never to keep the
    old one. entity_id is never mutated in place: the predecessor is archived byte-for-byte and
    the new entity links back to it via `revises`.

    Returns `{"status": "ok", "data": {"old_id": ..., "new_id": ..., "inherited": {...},
    "changed": {...}, ...}, "warnings": [...]}` -- `inherited` lists every field you omitted
    (including `tags`, if omitted) with the value actually carried forward; `changed` lists every
    field you explicitly supplied. A validation problem -- a missing required field, or targeting
    an already-inactive `entity_id` -- returns a clean `{"status": "rejected", "errors": [{...}],
    ...}` value; it never raises an exception across the tool boundary. An inactive target names
    its known successor in the rejection -- inspect it (get_memory) before retrying, rather than
    reissuing the same call.

    Example: `revise_memory(entity_id="abc123", title="[Worker] Request timeout", content="The
    worker request timeout is 30 seconds.", reason="Corrected the unit from milliseconds.",
    repoint_relations=True)` -- `tags`/`context_id`/`scope`/`memory_type` all omitted here are
    inherited from `abc123` unchanged; `repoint_relations=True` carries every edge `abc123` already
    had onto the new entity automatically.

    Pass `repoint_relations=True` whenever the target has existing edges and this revision is
    identity-preserving continuity (the common case, since revise_memory is for fixing a flawed
    *representation*, not changing the underlying fact) -- otherwise the response's
    `orphaned_semantic_edges` list is left for you to walk and repoint by hand via manage_relation,
    one call per edge. Leave it False (the default) only when the revision might invalidate what
    an existing edge asserted about the old content, and stale edges should surface for review
    instead of silently carrying forward.
    """
    content, content_error = _resolve_content(content, content_file_path)
    if content_error is not None:
        return content_error
    owner_id_ = _effective_owner()
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
            repoint_relations=repoint_relations,
        ),
    )


@mcp.tool()
def supersede_memory(
    entity_id: str,
    title: str,
    content: str | None = None,
    tags: list[str] = None,
    reason: str = None,
    context_id: str | None = None,
    scope: Literal["private", "shared"] | None = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
    repoint_relations: bool = False,
    content_file_path: str | None = None,
) -> dict:
    """Replaces knowledge that was once valid with newer knowledge -- the real-world fact/
    decision itself has changed, not just how it was worded. Use revise_memory instead when
    you're only correcting a representational error (wrong wording/unit) in an otherwise-still-
    true memory; update_memory_metadata for a metadata-only edit.

    entity_id (the active memory to supersede), title, and exactly one of content/
    content_file_path are required. tags, context_id, scope, and memory_type are all optional and
    INHERITED from the predecessor when omitted. The predecessor remains byte-identical and is
    linked via `supersedes`; an inactive target is never silently redirected.

    Returns `{"status": "ok", "data": {"old_id": ..., "new_id": ..., "inherited": {...},
    "changed": {...}, "semantic_relations_repointed": bool, "repointed_relations": [...], ...},
    "warnings": [...]}` on success, `{"status": "rejected", "errors": [{...}]}` -- naming known
    active successors and lineage -- when the target is already inactive. Never raises an
    exception across the tool boundary for a validation problem.

    Example: `supersede_memory(entity_id="abc123", title="[Roadmap] Q3 priorities", content="...
    (updated priorities)", reason="Priorities changed after the Q2 retro.")`.

    `repoint_relations` (default False) opts in to server-side repointing: every active
    non-lifecycle edge touching the predecessor is invalidated and an equivalent edge recreated
    onto the new entity atomically -- no separate `orphaned_semantic_edges` walk plus manual
    manage_relation calls needed. Setting this flag is your judgment that the supersession is
    additive/index-like continuity (e.g. a growing tracker document), not a correction that
    should leave some edge intentionally stale against the old content; `repointed_relations`
    lists each old/new relation id pair actually rewritten. Pairs naturally with a large document
    already edited on disk via `content_file_path`, such as a wayfinder map's growing body.
    """
    content, content_error = _resolve_content(content, content_file_path)
    if content_error is not None:
        return content_error
    owner_id_ = _effective_owner()
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
            repoint_relations=repoint_relations,
        ),
    )


@mcp.tool()
def get_memory(entity_id: str) -> dict:
    """Retrieves ONE memory in full, by exact ID or an unambiguous ID prefix -- when you already
    know (or can uniquely identify) which memory you want. Use search_memory instead to find a
    memory by content/tags/context when you don't already have its ID.

    Explicit retrieval includes archived memories (an archived ID is never silently redirected to
    a successor) and always returns full content, status, and lineage -- unlike search_memory's
    result items, which carry only a snippet/preview.

    Returns `{"status": "ok", "data": {"id", "title", "content", "tags", "status", "lineage",
    ...}, "warnings": [...]}`. `{"status": "rejected", "errors": [{"code": "UNKNOWN_ENTITY_ID" |
    "AMBIGUOUS_ID_PREFIX", "message": "...", "field": "entity_id"}]}` when the ID doesn't resolve
    to exactly one memory -- `AMBIGUOUS_ID_PREFIX` names the matching candidates so you can
    disambiguate with a longer prefix.

    Example: `get_memory(entity_id="a1b2c3")`.
    """
    owner_id_ = _effective_owner()
    return _backend_or_raise().call("get_memory", {"entity_id": entity_id, "owner_id": owner_id_})


@mcp.tool()
def inspect_memory(entity_id: str) -> dict:
    """Lighter-weight sibling to get_memory: identical field set minus `content`, replaced by a
    short `snippet` (first ~3 non-heading lines, truncated). Use this when you want to confirm a
    memory's identity/metadata/lineage, or aren't yet sure this is the one you want, without
    pulling the full body -- get_memory is the only path to full content once you're sure.

    Returns `{"status": "ok", "data": {"id", "title", "snippet", "tags", "status", "lineage",
    ...}, "warnings": [...]}`; `{"status": "rejected", "errors": [{"code": "UNKNOWN_ENTITY_ID" |
    "AMBIGUOUS_ID_PREFIX", ...}]}` on the same conditions as get_memory.

    Example: `inspect_memory(entity_id="a1b2c3")` to confirm which memory a search hit actually
    is before deciding whether to pull it in full.
    """
    owner_id_ = _effective_owner()
    return _backend_or_raise().call(
        "inspect_memory", {"entity_id": entity_id, "owner_id": owner_id_}
    )


@mcp.tool()
def get_lineage(
    entity_id: str,
    direction: Literal["ancestors", "descendants"] = "ancestors",
    max_depth: int = 5,
) -> dict:
    """Traverses a memory's revision/supersession/consolidation lineage in either direction --
    `ancestors` shows where it came from, `descendants` shows what it became. Archived nodes
    remain fully visible, so historical provenance is never hidden by an archive.

    direction: "ancestors" (default) or "descendants". max_depth caps traversal hops (default 5).

    Returns `{"status": "ok", "data": {"entity_id": ..., "direction": ..., "root": {...},
    "nodes": [...], "edges": [...], "total": ..., "total_nodes": ..., "graph_exhausted": ...,
    "point_in_time": ..., "max_depth": ...}, "warnings": [...]}` -- `nodes` holds every node
    found in the requested `direction` (the key is always `nodes`, never renamed to
    `ancestors`/`descendants` -- `direction` itself is the only place those two words appear in
    the response); `edges` lists the connecting lineage relations, each with
    `source_id`/`target_id`/`predicate`/`depth`/`source_title`/`source_status`/`target_title`/
    `target_status`. `graph_exhausted` is `true` when the traversal reached every real edge
    within `max_depth` (nothing was cut off). `{"status": "rejected", "errors": [{"code":
    "VALIDATION_ERROR", "message": "...", "field": "entity_id" | "direction" | "max_depth"}]}`
    for a missing/malformed parameter.

    Example: `get_lineage(entity_id="a1b2c3", direction="descendants")` to see every memory that
    eventually replaced this one.
    """
    owner_id_ = _effective_owner()
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
def get_related_memories(
    entity_id: str,
    max_depth: int = 5,
    direction: Literal["outbound", "inbound", "both"] = "both",
    include_inspect: bool = False,
) -> dict:
    """Traverses semantic relations (manage_relation edges, not lineage) from one memory for up
    to `max_depth` hops. Use get_lineage instead for revision/supersession/consolidation history;
    this tool is for the separate graph of relations you create via manage_relation.

    direction="both" (default) walks outbound and inbound edges independently and merges the
    results (a union of two single-direction traversals, not a true mixed-direction walk) --
    pass "outbound" or "inbound" to walk only one. include_inspect=True inlines each returned
    node's inspect_memory fields (no full content/lineage).

    Returns `{"status": "ok", "data": {"root": {...}, "related_memories": [...],
    "total_related_found": ..., "max_depth": ...}, "warnings": [...]}` -- `related_memories` is
    the one and only node-list key in the response; no other key duplicates it under a different
    name. `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR", "message": "..."}]}`
    for an unresolvable `entity_id`.

    Example: `get_related_memories(entity_id="a1b2c3", direction="outbound", max_depth=2)`.
    """
    owner_id_ = _effective_owner()
    return _backend_or_raise().call(
        "get_related_memories",
        {
            "entity_id": entity_id,
            "max_depth": max_depth,
            "direction": direction,
            "include_inspect": include_inspect,
            "owner_id": owner_id_,
        },
    )


@mcp.tool()
def retrieve_context(
    query: str,
    limit: int | None = None,
    budget_tokens: int | None = None,
    strategy: Literal["local", "global"] | None = None,
) -> dict:
    """Assemble a budget-bounded memory context for a task in one call: primary search hits, one-
    hop graph expansion, contradiction surfacing, lifecycle history, and token-budget packing --
    everything search_memory + get_related_memories + get_lineage would otherwise require
    composing by hand. For precise filters or locating one specific record, use search_memory
    instead; avoid calling both with the same query, since this tool already searches internally.

    query is required. limit caps how many primary search hits seed expansion (default 5).
    budget_tokens caps the total token payload of `memories[]` (server default/ceiling apply if
    omitted). strategy: "local" (default) is one-hop-plus-expansion around direct search hits;
    "global" instead seeds from the nearest community-detection clusters for whole-topic breadth,
    trading graph-local depth for topic coverage.

    **Return shape note**: on success this tool does NOT use the `{"status": "ok", "data": ...}`
    envelope other SALTMDB tools use -- it returns its own bespoke top-level shape directly:
    `{"query": ..., "memories": [...], "edges": [...], "lineage": {...}, "conflict_sets": [...],
    "metadata": {...}}`. Only a caller-input problem (a missing/non-string `query`) returns the
    standard `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR", "field": "query"}]}`
    envelope instead -- check for a top-level `"status"` key to distinguish the two: its presence
    means rejection, its absence means the bespoke success shape above.

    Each `memories[]` item is tagged `inclusion`: `"primary"` (a direct search hit),
    `"expansion"` (reached via one-hop graph traversal), or `"conflict_only"` (force-included
    because it contradicts something else in the result) for `strategy="local"`; or
    `"community_representative"`/`"community_member"` for `strategy="global"` (which never
    populates `edges`/`lineage`/`conflict_sets` at all -- always `[]`/`{}` -- and uses
    `metadata.community` in place of `metadata.fan_out`). Every item's own `retrieval_provenance`
    explains how it entered the result; a primary-hit item may also carry
    `relevance_preview`/`relevance_preview_meta` (identical semantics to search_memory's own
    field of the same name).

    Example: `retrieve_context(query="DNS incident root cause", limit=5, budget_tokens=4000)`.
    """
    owner_id_ = _effective_owner()
    return _backend_or_raise().call(
        "retrieve_context",
        {
            "query": query,
            "limit": limit,
            "budget_tokens": budget_tokens,
            "strategy": strategy if strategy is not None else "local",
            "owner_id": owner_id_,
        },
    )


@mcp.tool()
def get_events(
    context_id: str | None = None,
    agent_id: str | None = None,
    event_type: str | None = None,
    agent_session_id: str | None = None,
    event_id: str | None = None,
    order: Literal["newest_first", "oldest_first"] = "newest_first",
    limit: int | None = None,
    offset: int | None = None,
    full_content: bool = False,
) -> list:
    """Reads back events from the append-only events ledger -- for multi-agent coordination
    (what did another agent just log/decide) and wrap-up/session-review. Use log_event to write
    an event; this tool never writes.

    context_id is the headline filter (every event logged under a shared thread handle,
    survivable across a power cut). agent_id filters to one agent's events -- this tool has no
    notion of "my own events" the way log_event has a bound owner; it reads across the whole
    ledger, narrowed by whichever filters you supply. agent_session_id filters to one adapter
    process session. order: "newest_first" (default) or "oldest_first" (for chronological
    synthesis).

    Returns a bare list of event dicts (`{"id", "timestamp", "agent_id", "type", "content",
    ...}`) -- NOT the `{"status": ..., "data": ...}` envelope other SALTMDB tools use; an empty
    result is just `[]`, not distinguishable from an error by shape alone (there is no current
    error-sentinel convention documented for this tool to rely on).

    Content over 1000 characters is truncated ("[TRUNCATED]" suffix) by default. Two ways to get
    full text back, mirroring get_memory's list-then-fetch-by-ID pattern: event_id (exact match
    on the primary key) always returns full content regardless of full_content; full_content=True
    disables truncation for every row a broader query matches -- use it once your other filters
    already bound the result set to something you know is safe to receive in full.

    Example: `get_events(context_id="my-session-thread", order="oldest_first")` to replay a
    thread chronologically.
    """
    return _backend_or_raise().call(
        "get_events",
        {
            "context_id": context_id,
            "agent_id": agent_id,
            "event_type": event_type,
            "agent_session_id": agent_session_id,
            "event_id": event_id,
            "order": order,
            "limit": limit if limit is not None else 20,
            "offset": offset if offset is not None else 0,
            "full_content": full_content,
        },
    )


@mcp.tool()
def review_core_memory(
    entity_id: str | None = None,
    outcome: Literal["retain", "demote", "archive"] | None = None,
    review_rationale: str | None = None,
    core_review_after: str | None = None,
) -> dict:
    """Reviews an active core memory: retain (extend its review deadline), demote (turn it back
    into an ordinary searchable memory), or archive (retire it) -- a direct, synchronous
    operation. Use store_memory instead for a meaningful CONTENT revision; this tool only ever
    changes lifecycle/review state.

    entity_id and outcome ("retain"/"demote"/"archive") are required. review_rationale (20-1,000
    chars) is stored for provenance, never injected into the bootstrap digest. `retain` requires
    an absolute core_review_after in the future, at most 30 days out (omit for the 14-day
    default); demote/archive must not supply core_review_after. The configured calling agent need
    not match the entity's own owner, and reviewing never transfers ownership.

    Returns `{"status": "ok", "data": {"id": ..., "message": "..."}, "warnings": [...]}` --
    demote/archive on an already-non-core/already-archived memory is an `ALREADY_DONE`-coded
    warning, not a rejection. `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR" |
    "NOT_FOUND" | "CONFLICT", "message": "..."}]}` -- `VALIDATION_ERROR` for a malformed
    outcome/timestamp, `NOT_FOUND` for an unresolvable entity_id, `CONFLICT` for a `retain`
    against a memory that isn't currently an active core (already demoted or archived).

    Example: `review_core_memory(entity_id="abc123", outcome="retain",
    review_rationale="Still an active hazard; extending review window.")`.
    """
    return _backend_or_raise().call(
        "review_core_memory",
        {
            "entity_id": entity_id,
            "outcome": outcome,
            "review_rationale": review_rationale,
            "owner_id": _effective_owner(),
            "core_review_after": core_review_after,
        },
    )


@mcp.tool()
def update_memory_metadata(entity_id: str, metadata: dict) -> dict:
    """Shallow-merges `metadata` into an existing memory -- the lightest-weight way to update
    metadata only, with no quality gate, duplicate check, or content-hash machinery involved (an
    alternative to store_memory(entity_id=..., metadata=...), which also supports a metadata-only
    update but carries store_memory's own full validation path).

    entity_id and metadata (a dict) are required. Submitted keys overwrite/add; keys not
    mentioned are preserved untouched. There is no key-deletion sentinel -- submit a key with
    value null to mark it cleared (the key stays present with a null value, it is not stripped).
    Works uniformly on core and non-core memories; core lifecycle fields (outcome,
    core_review_after, review_rationale) are governed exclusively by review_core_memory, not this
    tool.

    Returns `{"status": "ok", "data": {"id": ..., "message": "..."}, "warnings": [...]}` --
    including when the patch was empty (still success, not a warning: nothing was rejected).
    `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR" | "AMBIGUOUS_ID_PREFIX" |
    "NOT_FOUND", "message": "...", "field": "entity_id" | "metadata"}]}` for a malformed
    parameter or an unresolvable/ambiguous entity_id.

    Example: `update_memory_metadata(entity_id="abc123", metadata={"project_id": "wf-42"})`.
    """
    return _backend_or_raise().call(
        "update_memory_metadata",
        {
            "entity_id": entity_id,
            "metadata": metadata,
        },
    )
