from typing import Literal
import json
import logging
from saltmdb.mcp.server import mcp
from saltmdb.daemon import client as daemon_client
from saltmdb.daemon import protocol

# ephemeral_service is the sole domain.services import remaining in this module (see
# ephemeral_memory below): EPHEMERAL_CONN never touches the persistent DB, so ephemeral_memory
# never goes over RPC and stays adapter-local, exactly as before Track B.
from saltmdb.domain.services import ephemeral_service

logger = logging.getLogger(__name__)


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
    agent_id: str = None,
    type: str = None,
    content: str = None,
    error_code: str = None,
    session_id: str = None,
    context_id: str = None,
    **kwargs,
) -> str:
    """Appends an event to the append-only events ledger."""
    kw = _unwrap_kwargs(kwargs)
    agent_id_ = _resolve(agent_id, kw, kwargs, "agent_id", "agent") or "system"
    type_ = _resolve(type, kw, kwargs, "type", "event_type") or "event"
    content_ = _resolve(content, kw, kwargs, "content", "message", "description") or ""
    error_code_ = _resolve(error_code, kw, kwargs, "error_code")
    session_id_ = _resolve(session_id, kw, kwargs, "session_id")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id", "project")
    return _backend_or_raise().call(
        "log_event",
        {
            "agent_id": agent_id_,
            "type": type_,
            "content": content_,
            "error_code": error_code_,
            "session_id": session_id_,
            "context_id": context_id_,
        },
    )


@mcp.tool()
def get_canonical_tags(query: str = None, domain: str = None, limit: int = None, **kwargs) -> list:
    """Queries the database to suggest existing canonical tags matching a search query/substring, to prevent tag fragmentation. Use query='auth' to filter by tag name substring.

    limit caps the number of tags returned (default 50), including when query is omitted --
    the full canonical tag table is never dumped unbounded."""
    kw = _unwrap_kwargs(kwargs)
    query_ = (
        query or domain or _resolve(None, kw, kwargs, "query", "domain", "substring", "tag_filter")
    )
    limit_ = _resolve(limit, kw, kwargs, "limit")
    limit_ = limit_ if limit_ is not None else 50
    return _backend_or_raise().call("get_canonical_tags", {"domain": query_, "limit": limit_})


@mcp.tool()
def get_canonical_predicates(query: str = None, limit: int = None, **kwargs) -> list:
    """Queries existing canonical relation predicates matching a search substring, to reduce
    predicate drift (e.g. elaborates_on vs relates_to vs references).

    limit caps the number of predicates returned (default 50)."""
    kw = _unwrap_kwargs(kwargs)
    query_ = _resolve(query, kw, kwargs, "query", "predicate_filter", "substring")
    limit_ = _resolve(limit, kw, kwargs, "limit")
    limit_ = limit_ if limit_ is not None else 50
    return _backend_or_raise().call("get_canonical_predicates", {"query": query_, "limit": limit_})


@mcp.tool()
def merge_tags(keep_tag: str = None, tags_to_merge: list = None, **kwargs) -> str:
    """Merges one or more fragmented/synonym tags into an explicitly chosen canonical tag, repointing all
    affected entities' tag associations. Use to fix folksonomy fragmentation (e.g. keep_tag='#docs',
    tags_to_merge=['#doc', '#documentation'])."""
    kw = _unwrap_kwargs(kwargs)
    keep_tag_ = _resolve(keep_tag, kw, kwargs, "keep_tag", "canonical_tag", "keep")
    raw_merge = (
        tags_to_merge
        if tags_to_merge is not None
        else _resolve(None, kw, kwargs, "tags_to_merge", "merge_tags", "aliases")
    )
    tags_to_merge_ = _normalize_list_or_str(raw_merge)
    return _backend_or_raise().call(
        "merge_tags", {"keep_tag": keep_tag_, "tags_to_merge": tags_to_merge_}
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

    If check_duplicates_only is True, returns duplicate detection results without writing to the database.

    Store-time disposition (Track A): every call is preflighted before persistence. If
    evidence-gathering finds no flagged candidates, this behaves exactly as always -- a single
    call, plain string result. If it finds one or more (a possible duplicate, supersession, or
    stale-consolidated-node signal), nothing is written; instead this returns a dict with
    `status: "REVIEW_REQUIRED"`, an opaque `review_token`, and the flagged `candidates`, each with
    an advisory `suggested_label` (never authoritative -- use your own judgment, including calling
    it a false alarm) and the `available_dispositions` for it. Resend the identical call with
    `review_token` and `dispositions` (one `{"candidate_id": ..., "disposition": "distinct" |
    "supersede" | "consolidate" | "elaborate"}` per flagged candidate) to commit. A stale/expired
    token, or a resend that no longer matches what was previewed, returns `status: "REVIEW_STALE"`
    instead -- call again without `review_token` for a fresh preflight.
    """
)
def store_memory(
    content: str = None,
    title: str = None,
    tags: list = None,
    is_core: bool = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] = None,
    owner_id: str = None,
    context_id: str = None,
    scope: Literal["private", "shared"] = None,
    check_duplicates_only: bool = False,
    review_token: str = None,
    dispositions: list = None,
    **kwargs,
) -> str | dict:
    kw = _unwrap_kwargs(kwargs)
    content_ = _resolve(content, kw, kwargs, "content", "text") or ""
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id", "context", "project")
    title_ = _resolve(title, kw, kwargs, "title")
    scope_ = _resolve(scope, kw, kwargs, "scope") or "shared"

    raw_tag = tags if tags is not None else _resolve(None, kw, kwargs, "tags", "tag")
    tags_ = _normalize_list_or_str(raw_tag) if raw_tag is not None else None

    raw_is_core = is_core if is_core is not None else _resolve(None, kw, kwargs, "is_core")
    if raw_is_core is not None:
        is_core_ = raw_is_core in (True, 1, "true", "1", "True")
    else:
        is_core_ = None

    memory_type_ = _resolve(memory_type, kw, kwargs, "memory_type", "type", "kind")

    check_duplicates_only_ = check_duplicates_only or kw.get("check_duplicates_only") or False

    entity_id = _resolve(None, kw, kwargs, "entity_id", "id")
    weight = _resolve(None, kw, kwargs, "weight") or 1
    relevance = _resolve(None, kw, kwargs, "relevance")
    impact = _resolve(None, kw, kwargs, "impact")
    novelty = _resolve(None, kw, kwargs, "novelty")
    actionability = _resolve(None, kw, kwargs, "actionability")
    metadata = _resolve(None, kw, kwargs, "metadata")
    skip_duplicate_check = _resolve(None, kw, kwargs, "skip_duplicate_check") or False
    review_token_ = _resolve(review_token, kw, kwargs, "review_token")
    dispositions_ = _resolve(dispositions, kw, kwargs, "dispositions")

    return _backend_or_raise().call(
        "store_memory",
        {
            "content": content_,
            "tags": tags_,
            "owner_id": owner_id_,
            "scope": scope_,
            "weight": weight,
            "is_core": is_core_,
            "memory_type": memory_type_,
            "title": title_,
            "entity_id": entity_id,
            "relevance": relevance,
            "impact": impact,
            "novelty": novelty,
            "actionability": actionability,
            "metadata": metadata,
            "skip_duplicate_check": skip_duplicate_check,
            "context_id": context_id_,
            "review_token": review_token_,
            "dispositions": dispositions_,
            "check_duplicates_only": check_duplicates_only_,
        },
    )


@mcp.tool(
    description="""Performs full-text keyword & dense vector hybrid search in long-term memory.

    If entity_id or fetch_full is specified, retrieves full Markdown text chunk directly.

    memory_type_filter optionally restricts results to one of the five fixed memory_type
    values ('fact', 'event', 'procedure', 'decision', 'preference'); every result item also
    echoes its 'memory_type'.

    rerank_by_topic (opt-in, default False): applies a Stage-2 cross-chunk semantic rerank on
    top of the normal hybrid search, using precomputed per-chunk embeddings instead of whole-
    document vectors. Use it when a short, specific query keeps losing to longer documents that
    merely share vocabulary but aren't actually about the query's topic (the "length dilution"
    problem) -- e.g. a query about one narrow fact loses to a long, generic document that happens
    to mention the same words in passing. When enabled, each result item gains a `topic_score`
    (0-1, higher = more topically specific to the query) and a `semantic_verdict`
    ("SAME_SPECIFIC_TOPIC" / "BROADLY_RELATED_THEMES" / "DIFFERENT_TOPICS"), and result ordering
    is fully reranked by topic_score instead of the normal hybrid-search order. A built-in
    confidence gate skips this Stage-2 rerank automatically whenever the hybrid search already has
    a decisive, dual-channel-confirmed top result -- rerank_by_topic=True still requests reranking,
    but the gate may decide it isn't needed for a given query.

    prefer_durable_types (off by default; pass True to opt in): stable-reorders results so
    `event`-typed memories (session notes/handovers, prone to staleness) sink behind the four
    durable types (fact/decision/procedure/preference), within the widened hybrid candidate pool.

    demote_superseded (off by default; pass True to opt in): stable-reorders results so a memory
    that is the target of a `supersedes` relation whose `valid_to` is unset or still in the future
    sinks to the back of the widened hybrid candidate pool. This is a narrower, single-column check
    than the full four-column bitemporal validity (`valid_from`/`valid_to`/`valid_at`/`invalid_at`)
    `mode="strict"`'s resolver and `mode="history"`'s own `is_superseded` tagging use elsewhere --
    a `supersedes` edge with a future `valid_from`, or one already invalidated via `invalid_at`, is
    still demoted by this flag (pre-existing behavior, unchanged by this default flip; Codex
    diff-review finding, roadmap `ba2cf66f`).

    Both `prefer_durable_types` and `demote_superseded` only affect the hybrid FTS+dense-vector
    pipeline; they have no effect when semantic search is disabled (which now makes query-based
    search_memory calls return an error rather than falling back to FTS-only results -- see
    SALTMDB_ENABLE_SEMANTIC) or on empty-query filter/tag-only browsing.

    disable_semantic (opt-in, default False): forces the FTS-only path for this one call,
    regardless of the server's SALTMDB_ENABLE_SEMANTIC setting -- a per-call override, not a
    server-wide toggle (Track B: a persistent daemon reads its environment once at its own
    startup, so a caller-side env mutation has no effect on an already-running daemon).

    use_cross_encoder (opt-in, default False; experimental, requires SALTMDB_RERANKER_MODEL to be
    set to a supported model name server-side -- a no-op with no error otherwise): an independent
    Stage-2 reordering alternative to `rerank_by_topic`, not a dependency of it -- either flag
    alone widens the candidate pool and shares the same decisive-hybrid-winner confidence gate.
    Scores the widened pool with a local ONNX cross-encoder (no PyTorch runtime) and fully
    reorders by score, adding a `cross_encoder_score` field to each result item. If both
    `rerank_by_topic` and `use_cross_encoder` are requested and neither is gated off,
    cross-encoder's ordering wins (it runs second, as the more precise stage) -- `topic_score`
    stays attached to the item regardless. Any failure (disabled, unsupported model, runner error)
    falls back deterministically to whatever ordering would exist without it -- never an error,
    never a widened result count.

    mode (opt-in, default "broad"): "broad" itself adds no filtering, resolution, or gating beyond
    what `rerank_by_topic`/`prefer_durable_types`/`demote_superseded`/`use_cross_encoder` already
    do -- it was byte-identical to this tool's behavior before `mode` existed, back when those four
    flags all defaulted off; that again matches today's defaults after the frozen blind evaluation
    selected `broad_rt0_pdt0_ds0_ce0` as the replacement broad-mode default.
    "strict"
    resolves a matched-but-superseded candidate to its live, multi-hop `supersedes` successor and
    requires every surviving candidate to independently clear a calibrated relevance-abstention
    gate -- an empty list is then a normal, successful "nothing sufficiently relevant" result, not
    an error. "strict" also always applies durable-type preference and demotes (never excludes) a
    surviving candidate that's still the target of a currently-valid `supersedes` edge the
    resolver couldn't cleanly resolve, or of a currently-valid `corrects` edge -- unconditionally,
    regardless of `prefer_durable_types`/`demote_superseded` above. "history" leaves every
    candidate visible (like "broad") and tags a candidate that is the target of a currently-valid
    `supersedes` edge with `"is_superseded": true` -- the tagging step itself never hides or
    reorders anything, but opt-in `prefer_durable_types`/`demote_superseded` still apply
    under "history" exactly as they do under "broad" and can reorder its results independently of
    the tagging. Neither
    "strict" nor "history" ever exposes archived material -- both still require
    `status != 'archived'` like "broad" already does. Only affects the hybrid query-keyword
    pipeline, same scope as `rerank_by_topic`/`prefer_durable_types`/`demote_superseded` above.
    """
)
def search_memory(
    owner_id: str = None,
    query_keywords: str = None,
    tags_filter: list = None,
    entity_id: str = None,
    fetch_full: bool = False,
    limit: int = None,
    context_id: str = None,
    is_core: bool = None,
    memory_type_filter: Literal["fact", "event", "procedure", "decision", "preference"] = None,
    cursor: str = None,
    include_related: bool = None,
    rerank_by_topic: bool | None = None,
    prefer_durable_types: bool | None = None,
    demote_superseded: bool | None = None,
    use_cross_encoder: bool | None = None,
    mode: Literal["strict", "broad", "history"] | None = None,
    disable_semantic: bool | None = None,
    **kwargs,
) -> list | dict | str:
    kw = _unwrap_kwargs(kwargs)
    entity_id_ = _resolve(entity_id, kw, kwargs, "entity_id", "id")
    fetch_full_ = fetch_full or kw.get("fetch_full") or False

    query_keywords_ = _resolve(
        query_keywords, kw, kwargs, "query_keywords", "query", "q", "keywords"
    )
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id", "context", "project")
    raw_tags = tags_filter or _resolve(None, kw, kwargs, "tags_filter", "tags")
    tags_filter_ = _normalize_list_or_str(raw_tags) if raw_tags else None
    metadata_filter_ = _resolve(None, kw, kwargs, "metadata_filter")
    explain_mode = _resolve(None, kw, kwargs, "explain_mode") or False
    tag_operator = _resolve(None, kw, kwargs, "tag_operator") or "AND"
    memory_type_filter_ = _resolve(
        memory_type_filter, kw, kwargs, "memory_type_filter", "memory_type", "type_filter"
    )
    limit_ = _resolve(limit, kw, kwargs, "limit", "max_results", "top_k") or 5
    is_core_ = _resolve(is_core, kw, kwargs, "is_core")
    cursor_ = _resolve(cursor, kw, kwargs, "cursor")
    include_related_ = _resolve(include_related, kw, kwargs, "include_related")
    include_related_ = include_related_ if include_related_ is not None else True
    rerank_by_topic_ = _resolve(rerank_by_topic, kw, kwargs, "rerank_by_topic", "rerank")
    rerank_by_topic_ = rerank_by_topic_ if rerank_by_topic_ is not None else False
    prefer_durable_types_ = _resolve(
        prefer_durable_types, kw, kwargs, "prefer_durable_types", "prefer_durable"
    )
    prefer_durable_types_ = prefer_durable_types_ if prefer_durable_types_ is not None else False
    demote_superseded_ = _resolve(demote_superseded, kw, kwargs, "demote_superseded")
    demote_superseded_ = demote_superseded_ if demote_superseded_ is not None else False
    use_cross_encoder_ = _resolve(
        use_cross_encoder, kw, kwargs, "use_cross_encoder", "cross_encoder"
    )
    use_cross_encoder_ = use_cross_encoder_ if use_cross_encoder_ is not None else False
    mode_ = _resolve(mode, kw, kwargs, "mode")
    mode_ = mode_ if mode_ is not None else "broad"
    disable_semantic_ = _resolve(disable_semantic, kw, kwargs, "disable_semantic", "no_semantic")
    disable_semantic_ = disable_semantic_ if disable_semantic_ is not None else False

    return _backend_or_raise().call(
        "search_memory",
        {
            "entity_id": entity_id_,
            "fetch_full": fetch_full_,
            "owner_id": owner_id_,
            "query_keywords": query_keywords_,
            "tags_filter": tags_filter_,
            "metadata_filter": metadata_filter_,
            "explain_mode": explain_mode,
            "limit": limit_,
            "context_id": context_id_,
            "is_core": is_core_,
            "memory_type_filter": memory_type_filter_,
            "tag_operator": tag_operator,
            "cursor": cursor_,
            "mode": mode_,
            "include_related": include_related_,
            "rerank_by_topic": rerank_by_topic_,
            "prefer_durable_types": prefer_durable_types_,
            "demote_superseded": demote_superseded_,
            "use_cross_encoder": use_cross_encoder_,
            "disable_semantic": disable_semantic_,
        },
    )


@mcp.tool()
def ephemeral_memory(
    action: Literal["get", "store"] = None, key: str = None, value: str = None, **kwargs
) -> str:
    """Manages volatile in-memory secret storage (get or store)."""
    kw = _unwrap_kwargs(kwargs)
    action_ = _resolve(action, kw, kwargs, "action") or "get"
    key_ = _resolve(key, kw, kwargs, "key")
    value_ = _resolve(value, kw, kwargs, "value")

    # Deliberately NOT routed through _backend_or_raise() -- EPHEMERAL_CONN is a separate
    # in-memory-only sqlite3 connection that never touches the persistent DB, so this tool was
    # never in scope for the DB-access-boundary invariant to begin with. Routing it through the
    # daemon would silently turn per-agent-process-isolated volatile secrets into a cross-agent-
    # shared store (Codex Track-B plan-review round-2 finding) -- calling ephemeral_service
    # directly, in-process, exactly as before Track B, preserves today's isolation exactly.
    if action_ == "store" or value_ is not None:
        return ephemeral_service.store_ephemeral_memory(key=key_, value=value_)
    return ephemeral_service.get_ephemeral_memory(key=key_)


@mcp.tool()
def archive_memory(
    entity_id: str | list[str] | None = None, owner_id: str = None, **kwargs
) -> str | list:
    """Explicitly archives (retires) one or multiple long-term memories.

    Accepts entity_id as a single string ID OR a list of string IDs.
    """
    kw = _unwrap_kwargs(kwargs)
    raw_target = _resolve(entity_id, kw, kwargs, "entity_id", "archive_requests", "id")
    target = _normalize_list_or_str(raw_target)
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")

    # The bulk/single/none decision depends on the ORIGINAL request shape (did the caller pass a
    # list, even a 1-item one?) -- pre-normalization information that daemon/dispatch.py can't
    # reconstruct from the already-normalized `target` list alone, so the mode is resolved here
    # and sent as an explicit tag (self-caught during implementation, see dispatch.py's matching
    # comment).
    if len(target) > 1 or (isinstance(raw_target, list) and len(target) > 0):
        return _backend_or_raise().call(
            "archive_memory", {"mode": "bulk", "archive_requests": target}
        )
    elif len(target) == 1:
        return _backend_or_raise().call(
            "archive_memory", {"mode": "single", "entity_id": target[0], "owner_id": owner_id_}
        )
    return _backend_or_raise().call("archive_memory", {"mode": "none", "owner_id": owner_id_})


@mcp.tool()
def manage_relation(
    relations: list = None,
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    invalidate: bool = None,
    override_justification: str = None,
    owner_id: str = None,
    **kwargs,
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
    """
    kw = _unwrap_kwargs(kwargs)
    relations_ = _resolve(relations, kw, kwargs, "relations")
    if relations_ and isinstance(relations_, str):
        relations_ = _normalize_list_or_str(relations_)

    source_id_ = _resolve(source_id, kw, kwargs, "source_id", "source")
    target_id_ = _resolve(target_id, kw, kwargs, "target_id", "target")
    predicate_ = _resolve(predicate, kw, kwargs, "predicate", "relation")
    invalidate_ = _resolve(invalidate, kw, kwargs, "invalidate") or False
    invalid_at_ = _resolve(None, kw, kwargs, "invalid_at")
    valid_at_ = _resolve(None, kw, kwargs, "valid_at")
    override_justification_ = _resolve(
        override_justification, kw, kwargs, "override_justification", "override_reason"
    )
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")

    return _backend_or_raise().call(
        "manage_relation",
        {
            "relations": relations_,
            "source_id": source_id_,
            "target_id": target_id_,
            "predicate": predicate_,
            "invalidate": invalidate_,
            "invalid_at": invalid_at_,
            "valid_at": valid_at_,
            "override_justification": override_justification_,
            "owner_id": owner_id_,
        },
    )


@mcp.tool()
def commit_consolidation(
    consolidations: list = None,
    parent_ids: list = None,
    title: str = None,
    content: str = None,
    tags: list = None,
    owner_id: str = None,
    context_id: str = None,
    override_justification: str = None,
    **kwargs,
) -> str | list:
    """Commits single or multiple consolidated memories, archiving raw parents and creating lineage edges.

    A pairwise-cohesion gate rejects parent sets whose chunk-embedding centroids fail a minimum
    similarity threshold (REJECT_LOW_COHESION), unless override_justification (a non-throwaway
    string explaining why this merge should proceed anyway) is supplied to force it through --
    the override is baked into the committed content and atomically audited. For the bulk
    (`consolidations`) shape, put `override_justification` on each individual item that needs
    it, not at the top level -- it is never shared across items in the same batch.
    """
    kw = _unwrap_kwargs(kwargs)
    consolidations_ = _resolve(consolidations, kw, kwargs, "consolidations")
    if consolidations_ and isinstance(consolidations_, str):
        consolidations_ = _normalize_list_or_str(consolidations_)

    raw_parents = _resolve(parent_ids, kw, kwargs, "parent_ids")
    parent_ids_ = _normalize_list_or_str(raw_parents)
    title_ = _resolve(title, kw, kwargs, "title")
    content_ = _resolve(content, kw, kwargs, "content", "text")
    raw_tags = _resolve(tags, kw, kwargs, "tags")
    tags_ = _normalize_list_or_str(raw_tags)
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
    context_id_ = _resolve(context_id, kw, kwargs, "context_id", "project_id")
    scope = _resolve(None, kw, kwargs, "scope") or "shared"
    weight = _resolve(None, kw, kwargs, "weight") or 1
    is_core_ = _resolve(None, kw, kwargs, "is_core")
    override_justification_ = _resolve(
        override_justification, kw, kwargs, "override_justification", "override_reason"
    )

    return _backend_or_raise().call(
        "commit_consolidation",
        {
            "consolidations": consolidations_,
            "parent_ids": parent_ids_,
            "title": title_,
            "content": content_,
            "is_core": is_core_,
            "tags": tags_,
            "scope": scope,
            "weight": weight,
            "owner_id": owner_id_,
            "context_id": context_id_,
            "override_justification": override_justification_,
        },
    )


@mcp.tool()
def inspect_graph(
    entity_id: str | None = None,
    mode: Literal["dependencies", "lineage", "orphans"] = None,
    max_depth: int = None,
    owner_id: str = None,
    point_in_time: str = None,
    **kwargs,
) -> dict:
    """Inspects memory graph structure (dependencies, consolidation lineage, or orphaned nodes).

    entity_id is optional when mode='orphans'.
    point_in_time (aliases: as_of, at) restricts 'dependencies'/'lineage' traversal to relation
    edges valid as of that ISO timestamp (defaults to now). Ignored for mode='orphans'.
    """
    kw = _unwrap_kwargs(kwargs)
    entity_id_ = _resolve(entity_id, kw, kwargs, "entity_id", "root_entity_id", "root_id", "id")
    mode_ = _resolve(mode, kw, kwargs, "mode") or "dependencies"
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")
    point_in_time_ = _resolve(point_in_time, kw, kwargs, "point_in_time", "as_of", "at")
    max_depth_ = _resolve(max_depth, kw, kwargs, "max_depth")

    return _backend_or_raise().call(
        "inspect_graph",
        {
            "entity_id": entity_id_,
            "mode": mode_,
            "owner_id": owner_id_,
            "point_in_time": point_in_time_,
            "max_depth": max_depth_,
        },
    )


@mcp.tool()
def get_events(
    agent_id: str = None,
    type_filter: str = None,
    session_id: str = None,
    limit: int = None,
    offset: int = None,
    status_filter: str = None,
    owner_id: str = None,
    mode: Literal["events", "session", "memories"] = None,
    **kwargs,
) -> list:
    """Retrieves operational events, session summary events, or scans memory logs."""
    kw = _unwrap_kwargs(kwargs)
    mode_ = _resolve(mode, kw, kwargs, "mode") or "events"
    limit_ = _resolve(limit, kw, kwargs, "limit") or 20
    offset_ = _resolve(offset, kw, kwargs, "offset") or 0
    session_id_ = _resolve(session_id, kw, kwargs, "session_id")
    agent_id_ = _resolve(agent_id, kw, kwargs, "agent_id", "agent")
    type_filter_ = _resolve(type_filter, kw, kwargs, "type_filter", "type")
    status_filter_ = _resolve(status_filter, kw, kwargs, "status_filter")
    owner_id_ = _resolve(owner_id, kw, kwargs, "owner_id", "owner")

    return _backend_or_raise().call(
        "get_events",
        {
            "mode": mode_,
            "limit": limit_,
            "offset": offset_,
            "session_id": session_id_,
            "agent_id": agent_id_,
            "type_filter": type_filter_,
            "status_filter": status_filter_,
            "owner_id": owner_id_,
        },
    )


@mcp.tool()
def dismiss_event(
    event_id: str | list[str] | None = None,
    reason: str | None = None,
    agent_id: str | None = None,
    **kwargs,
) -> str:
    """Dismisses review events to prevent them from remaining pending."""
    kw = _unwrap_kwargs(kwargs)
    event_ids_ = _resolve(event_id, kw, kwargs, "event_id", "event_ids", "id", "ids")
    reason_ = _resolve(reason, kw, kwargs, "reason")
    agent_id_ = _resolve(agent_id, kw, kwargs, "agent_id", "agent", "owner_id", "owner") or "system"
    if not event_ids_:
        raise ValueError("Missing 'event_id' parameter.")
    return _backend_or_raise().call(
        "dismiss_event", {"event_ids": event_ids_, "reason": reason_, "agent_id": agent_id_}
    )
