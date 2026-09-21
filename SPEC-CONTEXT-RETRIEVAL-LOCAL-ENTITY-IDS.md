# SPEC-CONTEXT-RETRIEVAL-LOCAL-ENTITY-IDS

## 0. Status

**LOCKED** (Amendment 1 applied to §4.1, Amendment 2 applied to §2.2, Amendment 3 applied to §5.6/§9, all pre-implementation, see those sections — OMP correctly reported `BLOCKED — SPEC ADJUDICATION REQUIRED` three times before editing any tracked file; all three adjudicated and fixed without touching production or test code)

**Scope — may edit:**
- `src/saltmdb/domain/services/retrieve_context_service.py`
- `src/saltmdb/mcp/tools.py` (the `retrieve_context` tool function only — its docstring and signature)
- `src/saltmdb/daemon/dispatch.py` (the `_dispatch_retrieve_context` function only, plus — per Amendment 1 — the one new top-of-file `from saltmdb.utils.envelope import error, rejected` import line §4.1 now requires; no other line in this file changes)
- `tests/test_retrieve_context_service.py`
- `tests/test_retrieve_context_wiring.py`
- `tests/test_orphan_community_service.py` (one test method only — `test_scenario_14_assemble_retrieve_context_composes_orphan_assignment_end_to_end`)

**Does not touch (see §8, Out of scope, for why each of these is deliberately excluded):**
- `_assemble_global_context` (the function body) and everything it calls — `community_retrieval_service.py`, `orphan_community_service.py`'s `global`-path usage
- `context_expansion_service.py`, `conflict_set_service.py`, `context_budget_service.py`, `lineage_assembly_service.py` — G3/G4/G7/G8's own mechanics
- `saltmdb.utils.text.resolve_entity_ref`, `saltmdb.utils.envelope`, `saltmdb.daemon.dispatch._required_str_list` — reused as-is, not modified
- Any file under `src/saltmdb/domain/services/memory_service/` — `search_memory(mode="strict")` itself is untouched; this spec removes retrieve_context's one call site into it, not the shared gate

## 1. Why

`retrieve_context`'s `local` strategy (the tool's silent default) reproducibly returns near-empty results on ordinary natural-language questions, because its internal primary-hit search hardcodes `search_memory(mode="strict")` — the same shared, heavily-calibrated relevance-abstention policy any caller gets by invoking `search_memory(mode="strict")` directly, not a bespoke retrieve_context mechanism (confirmed by direct source read, `retrieve_context_service.py:57-72` on the pre-change tree). Live evidence: a DNS-incident query returning zero real hits under `local` while `strategy="global"` surfaced the correct memories; a follow-up 3-query natural-language probe run *after* `global`'s own D3/D4 similarity-floor fixes shipped, showing `global` still buries or outright misses the correct answer in some cases — so switching the silent default to `global` was rejected as unsafe. External validation: 3 of 3 blind agents in a beta experiment rated `retrieve_context` the hardest/most-confusing tool of 19 and independently hit this exact failure live.

Resolved via a live grilling session with zbalint (SALTMDB memory `3e7aa75a`, ticket `de327538`, standing constraint 17 on the roadmap map): `local` strategy drops its internal primary search entirely. Instead of a `query` string, callers now supply `entity_ids` — one or more memory IDs they already have (typically from their own prior `search_memory` call, `get_lineage`, or `get_related_memories`) — as the anchor set that seeds graph expansion (G3), fan-out (G4), conflict-set assembly (G7), and budget packing (G8), all of which continue to run completely unchanged. This eliminates the internal search step that was the actual source of the gate problem, rather than recalibrating or mode-swapping it, and directly operationalizes zbalint's own "search and retrieve are complementary, not alternatives" usage insight as a forced API shape: an agent can no longer call `retrieve_context` as a search substitute, because it structurally requires an anchor the caller already chose.

`strategy="global"` is explicitly out of scope (see §8) — it has its own follow-up ticket (`3c612f02`) for an analogous entity-anchored re-seeding redesign, not yet grilled. Because `retrieve_context` is one MCP tool serving both strategies through one dispatch path, and `global` must keep working exactly as it does today (still driven by `query`), the public tool signature necessarily grows *both* `entity_ids` (new, `local`-only) and keeps `query` (unchanged, `global`-only) side by side for this transitional period — this is not a design choice made fresh in this spec, it is the direct, unavoidable mechanical consequence of the already-made decision to split `global`'s fix into a separate ticket rather than block on it. Every requirement below that touches the public signature treats this coexistence as fixed, not optional.

This spec also fixes three points the grilling round didn't reach, resolved here against the actual pre-change source (not asserted from memory) — read together, they are the reason `assemble_retrieve_context`'s local-path body needs real rewriting, not a parameter rename:

- **`score`**: on the pre-change tree, each primary hit's `score` (from the internal search's ranking) is a real, load-bearing tiebreak input to two *other* files this spec does not touch — `context_expansion_service.py:136`'s fan-out ranking and `conflict_set_service.py:242`'s conflict-set tiebreak, both computed as `max(primary_hit_score[...])` over the anchor set that produced a given candidate. There is no ranking score for a caller-supplied anchor. Assigning every anchor the same placeholder value (`1.0`) makes that `max(...)` term constant across every candidate in both sorts, so each one gracefully falls through to its next already-deterministic key (`node["entity_id"]` alphabetical in `context_expansion_service.py:140-146`) with **zero code changes required in either file** — confirmed by reading both sort sites directly. `score` is already internal-only today (never present in a `memories[]` output item); this spec keeps it exactly that way.
- **`relevance_preview`/`relevance_preview_meta`**: on the pre-change tree, these are computed by the internal `search_memory` call as an extractive preview *relative to the query text* (confirmed via the existing test `test_primary_hit_surfaces_relevance_preview_expansion_hit_does_not` and the orchestrator code it exercises). With no query text at all in an `entity_ids`-anchored call, there is nothing to extract a preview from — these fields become **permanently absent** from every `inclusion: "primary"` item under `local`, not "sometimes absent." This is a structural consequence of dropping `query`, not a new design choice.
- **`owner_id`**: on the pre-change tree, `assemble_retrieve_context` receives `owner_id` but uses it for exactly one thing — the internal `search_memory(owner_id=owner_id, ...)` call being removed (confirmed by grep: no other reference to `owner_id` anywhere in `retrieve_context_service.py`; `_assemble_global_context` never took it either). `get_memory` — this codebase's own precedent for "I already have a specific ID, give me it" — takes no `owner_id` and performs no owner-based access filtering at all. Removed entirely rather than kept as dead weight flowing through three unused layers.
- **`query`'s own validation for `strategy="global"`**: the pre-change signature had `query: str` as a required positional argument with no default, so a caller omitting it got an immediate Python `TypeError`. Making `query` optional (`str | None = None`) at the outer function level — necessary so the same signature can also serve `local`'s `entity_ids`-only calls — would otherwise let a direct call silently pass `query=None` through to the untouched `_assemble_global_context`, crashing inside `seed_and_rank_communities` instead of failing cleanly. §2.2 adds an explicit `query` validation branch for `strategy="global"` to close this gap, without touching `_assemble_global_context` itself. Found by tracing a concrete `global`-path call through the new signature before locking, not assumed safe by resemblance to the old code.

## 2. `src/saltmdb/domain/services/retrieve_context_service.py`

### 2.1 Imports

Add, alongside the existing imports at the top of the file:

```python
from saltmdb.utils.envelope import error, rejected
from saltmdb.utils.text import resolve_entity_ref
```

**Do not remove** the existing `from saltmdb.domain.services import memory_service` import, even though §2.2 removes the only line in this file that calls `memory_service.search_memory(...)`. Found via a whole-tree grep for `retrieve_context_service.memory_service` (gate step 10 discipline, not assumed): `tests/test_retrieve_context_service.py`'s `test_global_populated_leaf_community_uses_community_pipeline_only` (line ~701 of the pre-change file, untouched per §0/§8) uses `patch("saltmdb.domain.services.retrieve_context_service.memory_service.search_memory")` — a string-resolved mock target that requires `retrieve_context_service`'s own module namespace to still bind the name `memory_service`, independent of whether any code in this file still calls it. Removing the import would raise `AttributeError` at that untouched test's `setUp`, not a clean failure. Since `ruff check` cannot see a string-based `mock.patch` target and would otherwise flag this import as unused (F401), add `# noqa: F401` directly on the import line with a one-line comment stating this exact reason (mirrors the existing `# noqa: C901, PLR0912, PLR0915` already on this file's function definition, same file, same convention).

**Do remove** `Callable` from the existing `from typing import Any, Callable, cast` line, changing it to `from typing import Any, cast`. Unlike `memory_service`, `Callable` has no mock-target dependency (grep-confirmed: its only use in this file, `Callable[..., list[dict[str, Any]] | dict[str, Any]]`, is inside the exact `search_fn = cast(...)` block §2.2 removes) — nothing else in the file or in any test references it, so leaving it in would be a genuine, unjustified F401.

### 2.2 `assemble_retrieve_context` signature and local-path body (replaces lines 24-96 of the pre-change file)

**Amendment 2 (post-lock, pre-implementation adjudication)**: the replacement below originally still computed `effective_db_path` (the `A5 Amendment 4` fix -- resolving the real path behind a caller-supplied `db_connection` via `PRAGMA database_list`, so a downstream service call always receives the caller's actual database rather than silently falling back to `get_db_path()`'s global default) while also deleting that variable's only consumer: the `search_fn(..., db_path=effective_db_path)` call inside the exact `search_memory` block this same section removes. OMP correctly caught this live (`BLOCKED — SPEC ADJUDICATION REQUIRED`) before editing any tracked file: grep-confirmed `effective_db_path` has exactly one consumer in the whole pre-change file (the now-removed `search_fn` call), and reproduced this repo's real `ruff check` configuration (`pyproject.toml`'s `[tool.ruff.lint]` selects `F` and does not ignore `F841`) failing with `F841 Local variable 'effective_db_path' is assigned to but never used` against the originally-locked replacement. Verified independently before fixing (not trusted from OMP's report alone): confirmed via direct grep of the pre-change file that `effective_db_path` appears nowhere outside the block being replaced, and confirmed §5.4 of this very spec already documents, for the *test* side of this same fact, that "there is no sub-call receiving a `db_path` kwarg to assert against" post-change -- the service-layer replacement simply hadn't caught up to that same already-stated fact. The reason `effective_db_path` existed no longer holds under this rewrite: the entity-resolution loop that replaces `search_fn` reads directly off `conn` (`conn.execute(...)`), never needing a separately-resolved `db_path` string, and `_assemble_global_context(query, budget_tokens, conn)` was never passed one either (unchanged from the pre-change file, `cast`-confirmed by grep). The `db_path` *parameter* itself is untouched and still genuinely used, directly (not via `effective_db_path`), at `conn = get_connection(db_path or get_db_path())` when no connection is supplied. Fix: the `effective_db_path: str | None` declaration and its three-way `if`/`elif`/`else` assignment block are deleted outright from the replacement below -- dead code with a mechanically confirmed zero-consumer count, not a legitimate allowed consumer to prescribe. This does not reopen or contradict §4.1's Amendment 1 -- a separate, independently-discovered gap in the same locked section.

Current signature and local-path opening (pre-change, lines 24-96 — see `Read` output already on record for this session; reproduced here for the exact anchor):

```python
def assemble_retrieve_context(  # noqa: C901, PLR0912, PLR0915
    query: str,
    owner_id: str | None,
    *,
    limit: int | None = None,
    budget_tokens: int | None = None,
    strategy: str = "local",
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Assemble local graph-aware context from one point-in-time and one database connection."""
    should_close = False
    conn = db_connection
    if conn is None:
        conn = get_connection(db_path or get_db_path())
        should_close = True
    effective_db_path: str | None
    if db_path is not None:
        effective_db_path = db_path
    elif db_connection is not None:
        row = cast(tuple[int, str, str], conn.execute("PRAGMA database_list").fetchone())
        effective_db_path = row[2] or None
    else:
        effective_db_path = db_path or get_db_path()

    try:
        if strategy == "global":
            return _assemble_global_context(query, budget_tokens, conn)
        pit = datetime.now(UTC).isoformat()
        search_fn = cast(
            Callable[..., list[dict[str, Any]] | dict[str, Any]],
            memory_service.search_memory,
        )
        search_result = search_fn(
            owner_id=owner_id,
            query_keywords=query,
            limit=limit if limit is not None else 5,
            context_id=None,
            agent_session_id=None,
            tags_filter=None,
            memory_type_filter=None,
            is_core=None,
            cursor=None,
            mode="strict",
            include_related=False,
            return_diagnostics=False,
            db_connection=conn,
            db_path=effective_db_path,
        )
        search_hits = cast(list[dict[str, Any]], search_result)
        if search_hits and "id" not in search_hits[0]:
            logger.warning(
                "search_memory reported an internal error for query %r: %s",
                query,
                search_hits[0].get("error"),
            )
            search_hits = []

        primary_hits = [{"id": hit["id"], "score": hit["score"]} for hit in search_hits]
        primary_meta = {
            hit["id"]: {
                "title": hit["title"],
                "memory_type": hit["memory_type"],
                # relevance_preview/relevance_preview_meta are already computed by the internal
                # search_memory call above (query-focused extractive preview, see orchestrator.py)
                # -- surfaced here rather than discarded, per the same optional/budget-degraded
                # contract search_memory itself uses (absent, not null, when preview was skipped).
                "relevance_preview": hit.get("relevance_preview"),
                "relevance_preview_meta": hit.get("relevance_preview_meta"),
            }
            for hit in search_hits
        }
        original_rank = {hit["id"]: index + 1 for index, hit in enumerate(search_hits)}
        primary_hit_ids_set = {hit["id"] for hit in primary_hits}
```

Replace with:

```python
def assemble_retrieve_context(  # noqa: C901, PLR0912, PLR0915
    entity_ids: list[str] | None = None,
    query: str | None = None,
    *,
    budget_tokens: int | None = None,
    strategy: str = "local",
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Assemble local graph-aware context from one point-in-time and one database connection.

    strategy="local" (default): entity_ids is the required anchor set -- one or more memory IDs
    the caller already has. strategy="global": query is the required anchor (unchanged; see
    _assemble_global_context). Both parameters default to None at the signature level only so
    one function can serve both strategies -- exactly one is semantically required depending on
    strategy, enforced by the explicit validation block below, not by the type signature itself.
    """
    if strategy == "global":
        if not query or not isinstance(query, str):
            return rejected(
                [error("VALIDATION_ERROR", "query is required for strategy=\"global\"", "query")]
            )
    else:
        if not entity_ids or not isinstance(entity_ids, list):
            return rejected(
                [error("VALIDATION_ERROR", "entity_ids is required and must be a non-empty list", "entity_ids")]
            )
        if not all(isinstance(item, str) and item for item in entity_ids):
            return rejected(
                [error("VALIDATION_ERROR", "entity_ids must be a list of non-empty strings", "entity_ids")]
            )

    should_close = False
    conn = db_connection
    if conn is None:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        if strategy == "global":
            return _assemble_global_context(query, budget_tokens, conn)
        pit = datetime.now(UTC).isoformat()

        resolved_hits: list[dict[str, Any]] = []
        unresolved_errors: list[dict[str, Any]] = []
        for raw_id in cast(list[str], entity_ids):
            resolved_id, candidates, truncated = resolve_entity_ref(conn, raw_id)
            if candidates:
                item = error(
                    "AMBIGUOUS_ID_PREFIX",
                    f"ID prefix '{raw_id}' matches multiple memories; provide a longer prefix or full UUID.",
                    "entity_ids",
                )
                item["candidates"] = candidates
                if truncated:
                    item["candidates_truncated"] = True
                unresolved_errors.append(item)
                continue
            if not resolved_id:
                unresolved_errors.append(
                    error("UNKNOWN_ENTITY_ID", f"No memory matches entity_id '{raw_id}'.", "entity_ids")
                )
                continue
            row = conn.execute(
                "SELECT title, memory_type FROM entities WHERE id = ?", (resolved_id,)
            ).fetchone()
            if row is None:
                unresolved_errors.append(
                    error("UNKNOWN_ENTITY_ID", f"No memory matches entity_id '{raw_id}'.", "entity_ids")
                )
                continue
            resolved_hits.append({"id": resolved_id, "title": row[0], "memory_type": row[1]})

        if unresolved_errors:
            return rejected(unresolved_errors)

        # No ranking exists for a caller-supplied anchor. A uniform placeholder score makes the
        # tiebreak term in context_expansion_service.py and conflict_set_service.py's own
        # max(...) comparisons constant across every candidate, so both sorts fall through
        # unchanged to their next already-deterministic key -- see spec Why section.
        primary_hits = [{"id": hit["id"], "score": 1.0} for hit in resolved_hits]
        primary_meta = {
            hit["id"]: {"title": hit["title"], "memory_type": hit["memory_type"]}
            for hit in resolved_hits
        }
        original_rank = {hit["id"]: index + 1 for index, hit in enumerate(resolved_hits)}
        primary_hit_ids_set = {hit["id"] for hit in primary_hits}
```

### 2.3 Primary-item assembly (replaces lines 199-215 of the pre-change file)

Pre-change:

```python
        memories: list[dict[str, Any]] = []
        for entity_id in final_primary_ids:
            memory_item: dict[str, Any] = {
                "entity_id": entity_id,
                "title": primary_meta[entity_id]["title"],
                "memory_type": primary_meta[entity_id]["memory_type"],
                "inclusion": "primary",
                "retrieval_provenance": [
                    {"reason": "primary_search", "rank": original_rank[entity_id]}
                ],
            }
            if primary_meta[entity_id]["relevance_preview"] is not None:
                memory_item["relevance_preview"] = primary_meta[entity_id]["relevance_preview"]
                memory_item["relevance_preview_meta"] = primary_meta[entity_id][
                    "relevance_preview_meta"
                ]
            memories.append(memory_item)
```

Replace with:

```python
        memories: list[dict[str, Any]] = []
        for entity_id in final_primary_ids:
            memories.append(
                {
                    "entity_id": entity_id,
                    "title": primary_meta[entity_id]["title"],
                    "memory_type": primary_meta[entity_id]["memory_type"],
                    "inclusion": "primary",
                    "retrieval_provenance": [
                        {"reason": "caller_supplied_anchor", "rank": original_rank[entity_id]}
                    ],
                }
            )
```

(`relevance_preview`/`relevance_preview_meta` are dropped entirely from this block, not conditionally included — there is no query text to derive them from; see §1. The `retrieval_provenance` reason string changes from `"primary_search"` to `"caller_supplied_anchor"` since there is no search step to attribute it to — `rank` is retained as the caller's own list-order position, still a meaningful, deterministic signal.)

### 2.4 Return envelope's top-level key (replaces line 311 of the pre-change file)

Pre-change:

```python
        return {
            "query": query,
```

Replace with:

```python
        return {
            "entity_ids": entity_ids,
```

(Only within the `local`-path `return` block — `_assemble_global_context`'s own `return {"query": query, ...}` at its line 402 is untouched; see §8.)

### 2.5 `limit` parameter removal

Every reference to `limit` inside `assemble_retrieve_context`'s local path is removed as a direct consequence of §2.2's rewrite (the parameter no longer exists in the signature, and the removed internal search call was its only consumer). No separate edit beyond what §2.2 already shows.

## 3. `src/saltmdb/mcp/tools.py`

### 3.1 `retrieve_context` signature and docstring (replaces lines 1247-1295 of the pre-change file)

Pre-change:

```python
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
```

Replace with:

```python
def retrieve_context(
    entity_ids: list[str] | None = None,
    query: str | None = None,
    budget_tokens: int | None = None,
    strategy: Literal["local", "global"] | None = None,
) -> dict:
    """Assemble a budget-bounded memory context around a known anchor in one call: one-hop graph
    expansion, contradiction surfacing, lifecycle history, and token-budget packing -- everything
    get_related_memories + get_lineage would otherwise require composing by hand. This tool does
    not search; find the memory you want with search_memory first, then hand its id here to pull
    in what's connected to it. Using only search_memory or only retrieve_context is a usage
    anti-pattern -- they are complementary, not alternatives.

    Pass exactly one of entity_ids or query, matching your strategy -- never both, never neither.
    strategy="local" (default): entity_ids is required -- one or more memory IDs you already have
    (typically from your own
    prior search_memory call, get_lineage, or get_related_memories); each is resolved the same
    flexible way get_memory resolves entity_id (full ID, short prefix, or exact title). Every
    unresolvable or ambiguous id in the list rejects the whole call -- no silent partial-list
    degradation. strategy="global": query is required instead (unchanged) -- it seeds from the
    nearest community-detection clusters by query-embedding similarity for whole-topic breadth,
    trading graph-local depth for topic coverage; this strategy has its own separate, not-yet-
    redesigned entity-anchored seeding path tracked in a follow-up effort, not available yet.
    budget_tokens caps the total token payload of `memories[]` (server default/ceiling apply if
    omitted).

    **Return shape note**: on success this tool does NOT use the `{"status": "ok", "data": ...}`
    envelope other SALTMDB tools use -- it returns its own bespoke top-level shape directly. For
    `strategy="local"` that top-level shape echoes the caller's own anchor back as `entity_ids`:
    `{"entity_ids": [...], "memories": [...], "edges": [...], "lineage": {...},
    "conflict_sets": [...], "metadata": {...}}`. For `strategy="global"` it instead echoes `query`
    in that same top-level slot (unchanged) -- the two strategies' envelopes differ in this one
    field while that asymmetry persists. Only a caller-input problem (a missing/empty/wrong-type
    `entity_ids` for `local`, an unresolvable or ambiguous id anywhere in the list, or a missing
    `query` for `global`) returns the standard `{"status": "rejected", "errors": [{"code": ...,
    "field": ...}]}` envelope instead -- check for a top-level `"status"` key to distinguish the
    two: its presence means rejection, its absence means the bespoke success shape above.

    Each `memories[]` item is tagged `inclusion`: `"primary"` (a caller-supplied anchor),
    `"expansion"` (reached via one-hop graph traversal), or `"conflict_only"` (force-included
    because it contradicts something else in the result) for `strategy="local"`; or
    `"community_representative"`/`"community_member"` for `strategy="global"` (which never
    populates `edges`/`lineage`/`conflict_sets` at all -- always `[]`/`{}` -- and uses
    `metadata.community` in place of `metadata.fan_out`). Every item's own `retrieval_provenance`
    explains how it entered the result. Unlike before, a `strategy="local"` primary item never
    carries `relevance_preview`/`relevance_preview_meta` -- those were derived from query text,
    which this strategy no longer has; you already know why you picked that anchor.

    Example: `retrieve_context(entity_ids=["a1b2c3d4"], budget_tokens=4000)`.
    """
    owner_id_ = _effective_owner()
    if strategy == "global":
        return _backend_or_raise().call(
            "retrieve_context",
            {
                "entity_ids": None,
                "query": query,
                "budget_tokens": budget_tokens,
                "strategy": "global",
                "owner_id": owner_id_,
            },
        )
    return _backend_or_raise().call(
        "retrieve_context",
        {
            "entity_ids": entity_ids,
            "query": None,
            "budget_tokens": budget_tokens,
            "strategy": "local",
        },
    )
```

(`owner_id_` is still computed and forwarded for `strategy="global"` only, matching `_assemble_global_context`'s own untouched signature and dispatch's own untouched handling of it for that branch -- see §4. It is dropped from the `local` payload entirely, per §1's `owner_id` finding.)

## 4. `src/saltmdb/daemon/dispatch.py`

### 4.1 `_dispatch_retrieve_context` (replaces lines 447-457 of the pre-change file)

**Amendment 1 (post-lock, pre-implementation adjudication)**: the version originally locked here assumed `dispatch.py` already had a `_DispatchValidationError` exception class and an `error_codes` module, and that its shared per-field helpers (`_optional_int_or_none`, `_optional_strategy`, `_required_str_list`) already raised that exception and were caught/converted to `rejected()` envelopes at each call site. That assumption was wrong for this worktree: it was carried over from `develop`'s current `dispatch.py`, which picked up exactly that machinery from a separate, unrelated effort (`SPEC-TOOL-CONTRACT-CONSISTENCY`, merged to `develop` via `e8ce355`) that this branch's lineage (`context-aware-search`) never merged in -- confirmed `e8ce355` is not an ancestor of `feature/context-retrieval-local-entity-ids`. OMP correctly caught this live (`BLOCKED -- SPEC ADJUDICATION REQUIRED`) before editing any tracked file: grep confirmed zero occurrences of `_DispatchValidationError`/`error_codes`/`rejected`/`error` anywhere in this worktree's `dispatch.py`, whose actual established contract is that every one of its per-field helpers raises bare `ValueError`, uncaught, propagating straight through `dispatch_tool`'s `except BaseException: raise` (dispatch.py:527-529) -- there is no dispatch-layer envelope construction anywhere in this file today. Pulling `feature/tool-contract-consistency`'s merge into this branch to make the original text apply verbatim was rejected as the fix: it would drag a large, unrelated cross-cutting refactor into a branch scoped to one function, violating this spec's own locked "`_dispatch_retrieve_context` function only" edit boundary. The replacement below instead targets what actually exists in this worktree: `saltmdb.utils.envelope`'s `error`/`rejected` (real, already imported and used elsewhere in this exact codebase, e.g. `memory_service/lifecycle.py`'s `UNKNOWN_ENTITY_ID` sites) with bare string-literal codes -- the same convention §2.2 of this very spec already independently uses correctly (`"VALIDATION_ERROR"`, `"UNKNOWN_ENTITY_ID"`, `"AMBIGUOUS_ID_PREFIX"` as literals, no `error_codes` module reference anywhere in §2). No `_DispatchValidationError` class and no `error_codes` module are introduced. `strategy`/`budget_tokens` keep using the existing shared helpers exactly as today (bare `ValueError`, uncaught) -- consistent with every other still-unconverted dispatch function in this file, and untested by §6.2's assertions, which only require envelope-shaped rejections for the `entity_ids`/`query` checks below.

Pre-change (verified against this worktree's actual current `dispatch.py:447-457`):

```python
def _dispatch_retrieve_context(**kw):
    query = kw.get("query")
    if not isinstance(query, str):
        raise ValueError("query is required")
    return retrieve_context_service.assemble_retrieve_context(
        query=query,
        owner_id=kw.get("owner_id"),
        limit=_optional_int_or_none(kw, "limit"),
        budget_tokens=_optional_int_or_none(kw, "budget_tokens"),
        strategy=_optional_strategy(kw),
    )
```

Replace with:

```python
def _dispatch_retrieve_context(**kw):
    strategy = _optional_strategy(kw)
    budget_tokens = _optional_int_or_none(kw, "budget_tokens")
    if strategy == "global":
        if kw.get("entity_ids") is not None:
            return rejected(
                [error("VALIDATION_ERROR", "entity_ids is not valid for strategy=\"global\"", "entity_ids")]
            )
        query = kw.get("query")
        if not isinstance(query, str):
            return rejected([error("VALIDATION_ERROR", "query is required", "query")])
        return retrieve_context_service.assemble_retrieve_context(
            query=query,
            budget_tokens=budget_tokens,
            strategy="global",
        )
    if kw.get("query") is not None:
        return rejected(
            [error("VALIDATION_ERROR", "query is not valid for strategy=\"local\"", "query")]
        )
    entity_ids = kw.get("entity_ids")
    if not isinstance(entity_ids, list) or not entity_ids:
        return rejected(
            [error("VALIDATION_ERROR", "entity_ids is required and must be a non-empty list", "entity_ids")]
        )
    if not all(isinstance(item, str) for item in entity_ids):
        return rejected(
            [error("VALIDATION_ERROR", "entity_ids must be a list of strings", "entity_ids")]
        )
    return retrieve_context_service.assemble_retrieve_context(
        entity_ids=entity_ids,
        budget_tokens=budget_tokens,
        strategy="local",
    )
```

Also add, alongside `dispatch.py`'s existing imports (there is no pre-existing `saltmdb.utils.envelope` import in this file to extend -- every current dispatch function that returns a `rejected()` envelope gets it by passing a service call's return value straight through, never by constructing one itself; this is the first `_dispatch_*` function in the file to construct one directly, which is why the import is new rather than an existing line being reused):

```python
from saltmdb.utils.envelope import error, rejected
```

(Rejects a caller supplying the wrong-strategy parameter explicitly, rather than the MCP tool wrapper's own silent discard in §3.1 being the only guard — §3.1's wrapper already forces the non-matching field to `None` before this point for calls that go through `tools.retrieve_context`, so this check is primarily a defense-in-depth guard for direct dispatch-layer callers/tests that bypass the wrapper, mirroring the same belt-and-suspenders posture already used for `entity_ids`'s own validation being checked at both dispatch and service layers.)

(The `entity_ids` non-string-member check above is written inline rather than via the existing `_required_str_list` helper (dispatch.py:114-118) because that helper's contract is to *raise* bare `ValueError` on a non-string member -- correct for every one of its other two callers (`tags`, `parent_ids`), which are untouched by this spec and out of its locked edit scope, but wrong here: §6.2 requires `entity_ids=["ok", 123]` to come back as a returned `rejected()` envelope from `_dispatch_retrieve_context`, not a raised exception. Reusing the helper unmodified would fail that assertion; modifying the shared helper to return instead of raise would change behavior for its other two callers, outside this spec's "`_dispatch_retrieve_context` function only" scope. The inline check reproduces the same "list of strings" condition without touching the shared helper. `owner_id`/`limit` are dropped from the call entirely per §1/§3 -- `owner_id` is no longer read via `kw.get("owner_id")` anywhere in the replacement above, and `limit` no longer exists as a dispatch-layer concept at all per §2.5.)

## 5. `tests/test_retrieve_context_service.py`

This file's 20 existing tests fall into four dispositions (§5.2-§5.6 below), plus two new tests to add (§5.5) and one dead test helper to remove (§5.7). Apply each by category; do not treat this as a uniform find-and-replace.

**5.1 — `_assemble` helper (lines 89-103) and `_memory_id`/`_memory` fixtures**: change the helper's signature from `(query: str, *, limit=None, budget_tokens=None, owner_id=...)` to `(entity_ids: list[str], *, budget_tokens=None)`, dropping `limit` and `owner_id` (the fixture helpers `_memory`/`_relation`/`_raw_relation` that create real rows are untouched -- they already return real entity ids, which is exactly what the converted calls below need):

```python
    def _assemble(
        self,
        entity_ids: list[str],
        *,
        budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        return assemble_retrieve_context(
            entity_ids,
            budget_tokens=budget_tokens,
            db_connection=self.conn,
        )
```

**5.2 — Convert to `entity_ids` (11 tests, use the fixture(s) each test's own body already creates as the anchor; keep every existing assertion about expansion/conflict/lineage/budget behavior unchanged since G3/G4/G7/G8 are untouched)**:
`test_single_dependency_chain_surfaces_primary_and_expansion_without_conflicts`,
`test_in_network_edge_surfaces_verbatim_between_two_primary_hits`,
`test_three_generation_supersession_chain_appears_in_lineage_only`,
`test_budget_dropped_expansion_head_pruned_from_lineage`,
`test_unresolved_contradiction_force_includes_conflict_only_member`,
`test_lifecycle_resolved_contradiction_surfaces_via_lineage_not_conflict_sets`,
`test_equal_visibility_reconciles_budget_dropped_conflict_expansion`,
`test_equal_visibility_reconciliation_preserves_unrelated_budget_drop`,
`test_fan_out_cap_truncation_passes_through_from_expansion`,
`test_budget_tokens_above_ceiling_reports_configured_maximum`,
`test_one_connection_is_shared_across_search_and_graph_pipeline`.

For every call site among these 11 that used `self._assemble(some_query_string, ...)` where `some_query_string` was written to match a specific fixture by keyword, replace it with `self._assemble([that_fixture_entity_id], ...)` using the actual variable already holding that fixture's id in the same test body (each of these tests already assigns the fixture's return value to a local variable via `self._memory(...)`; use that variable directly, not a new lookup). For any of these 11 whose assertions reference `relevance_preview`/`relevance_preview_meta` on a primary item, or a `score`-derived value in a primary item, remove that assertion (those fields no longer exist on primary items; see §1/§2.3). Specifically, `test_single_dependency_chain_surfaces_primary_and_expansion_without_conflicts` (line 232 of the pre-change file) asserts `[{"reason": "primary_search", "rank": 1}]` against a primary item's `retrieval_provenance` — update the string to `"caller_supplied_anchor"` per §2.3 (found via a whole-tree grep for the old string at lock time, per this skill's own gate step 5 — not a guess).

`test_one_connection_is_shared_across_search_and_graph_pipeline` (line 621 of the pre-change file) never mocked `search_memory` in the first place — it wraps the real `get_connection` and asserts `call_count <= 1`. It needs only its query args (`"single-connection-query"`, `"retrieve-context-test"`, `limit=1`) replaced with `entity_ids=[primary]` (dropping `limit`); the `get_connection` wrap and its call-count assertion are unaffected, and the property it guards (no redundant connection open) still holds — the `entity_ids` resolution loop in §2.2 uses the already-established `conn` directly via `conn.execute(...)`, exactly like the code it replaces did.

**5.3 — Rewrite for the new premise (3 tests)**:
- `test_primary_hit_surfaces_relevance_preview_expansion_hit_does_not` — rename to `test_primary_hit_never_carries_relevance_preview_under_entity_ids_anchor` and rewrite its body to assert `"relevance_preview" not in memory_item` and `"relevance_preview_meta" not in memory_item` for the primary item, using an `entity_ids`-anchored call. Keep whatever fixture setup this test already uses for its expansion-hit assertion (that half is unaffected).
- `test_stale_conflict_only_reference_uses_unknown_memory_type` (line 569 of the pre-change file): this test already builds `primary_hits = [{"id": primary, "title": ..., "memory_type": "fact", "score": 1.0}]` by hand and mocks `search_memory` to return it verbatim (rather than actually searching), specifically so it can pair a real `primary` memory with a fabricated stale `contradicts` target (`missing`, a UUID never actually stored) via a raw FK-disabled insert. Under `entity_ids` anchoring this fabrication is unnecessary: remove the `patch("saltmdb.domain.services.retrieve_context_service.memory_service.search_memory", return_value=primary_hits)` context manager and its corresponding `search_memory_mock.assert_called_once()` assertion (line 616) entirely — call `self._assemble([primary], budget_tokens=...)` instead, which resolves `primary` for real through §2.2's own resolution loop and produces the equivalent `primary_hits` shape organically. Keep the `expand_context_candidates` mock and its `assert_called_once()` exactly as-is (still legitimately needed to inject the fabricated stale `contradicts_edges`) — only the `search_memory` mock and its assertion are removed.
- `test_explicit_local_strategy_matches_omitted_strategy` (line 884 of the pre-change file): currently mocks `search_memory` to return `[]` and asserts both the omitted-strategy and explicit-`strategy="local"` calls produce an identical *empty* envelope. Remove the `search_memory` patch entirely (irrelevant now), create one real fixture memory, and assert both calls (`self._assemble([fixture])` and `assemble_retrieve_context(entity_ids=[fixture], strategy="local", db_connection=self.conn)`) produce an identical, *populated* result (the fixture present as the sole primary item) — the property under test (explicit `"local"` matches the default) is unchanged; only the fixture-vs-mock mechanism and the empty-vs-populated expectation change.

**5.4 — Delete (obsolete premise, 4 tests)**:
- `test_zero_primary_hits_compose_the_all_empty_envelope` — "zero primary hits from a query that matched nothing" cannot happen under `entity_ids` (an empty/missing list is now a `VALIDATION_ERROR` rejection, not an empty success envelope). Delete; §5.5 adds its replacement.
- `test_search_internal_error_sentinel_composes_as_empty_not_fatal` — tested the removed `search_hits[0]` missing-`"id"`-key fallback path (pre-change lines 74-80), which no longer exists. Delete.
- `test_limit_one_honors_primary_search_count_before_expansion` — tested the removed `limit` parameter's old meaning. Delete.
- `test_connection_only_passes_connection_database_path_to_search` (line 642 of the pre-change file) — its entire premise is asserting `search_memory.call_args.kwargs["db_path"]`, i.e. that *something downstream receives `db_path` as an argument from a sub-call*. Under §2.2's rewrite, entity resolution happens via `conn.execute(...)` directly inline — there is no sub-call receiving a `db_path` kwarg to assert against, and the connection-reuse property this test was adjacent to is already fully covered by the rewritten `test_one_connection_is_shared_across_search_and_graph_pipeline` in §5.2. Delete; no replacement needed.

**5.5 — New tests (add, to cover the validation behavior §5.4's deletions leave uncovered)**:
- A test asserting `assemble_retrieve_context(entity_ids=None, db_connection=self.conn)` (and separately, `entity_ids=[]`) returns `{"status": "rejected", "errors": [{"code": "VALIDATION_ERROR", "field": "entity_ids"}]}` (mirror the exact assertion style `test_dispatch_requires_string_query_but_allows_empty_string` in `test_retrieve_context_wiring.py` used for its now-removed case).
- A test asserting a single unresolvable id (a well-formed but nonexistent UUID) in an otherwise-valid `entity_ids` list returns `{"status": "rejected", "errors": [{"code": "UNKNOWN_ENTITY_ID", "field": "entity_ids"}]}`, and that the whole call is rejected even when the list also contains a real, resolvable id (no partial success).

**5.6 — `test_global_populated_leaf_community_uses_community_pipeline_only`, `test_global_without_communities_returns_empty_hierarchy_envelope`**

**Amendment 3 (post-lock, pre-implementation adjudication)**: originally specified as fully "untouched." That was wrong for one exact line in each, caught live by OMP (`BLOCKED — SPEC ADJUDICATION REQUIRED`, third round) after applying only the §2.2 service replacement and running these two tests fresh: both failed on the wrong string reaching `embed_text`/the result envelope (`embed_text('retrieve-context-test')` instead of `embed_text('global-community-query')`, and the mirror on the empty-envelope test), confirming a real parameter-order collision, not a false positive. Root cause, verified directly against this worktree's actual test source (`tests/test_retrieve_context_service.py:718-724` and `:824-829`): both calls are positional --
```python
assemble_retrieve_context(query, "retrieve-context-test", strategy="global", budget_tokens=1000, db_path=self.db_path)
```
and
```python
assemble_retrieve_context(query, "retrieve-context-test", strategy="global", db_connection=self.conn)
```
Under the *pre-change* signature (`query: str, owner_id: str | None, *, ...`), the first positional bound to `query` and the second to `owner_id` -- correctly, since that was the exact old order. Under §2.2's new signature (`entity_ids: list[str] | None = None, query: str | None = None, *, ...`), the same two positionals now bind `entity_ids = query_string` and `query = "retrieve-context-test"` -- silently wrong, not a crash, which is exactly why it only surfaced by running the tests rather than by static inspection. The original §5.6 instruction ("confirm neither test passes a now-removed `limit`/`owner_id` kwarg... if either does, drop just that kwarg") does not cover this: `"retrieve-context-test"` is not a *keyword* argument that can simply be dropped, it is the second *positional* argument, and dropping it without also converting the first positional to a keyword would leave `query` bound by position to whatever argument (if any) now follows -- not a safe mechanical no-op.

Two ways to resolve this were weighed: (a) reorder §2.2's production signature to put `query` first (restoring positional compatibility for these two tests), or (b) rewrite these two test calls from positional to keyword form, since `owner_id` was dead weight for the `global` path even on the *pre-change* tree (confirmed via §1's own already-locked finding: `_assemble_global_context` never took `owner_id` to begin with, so `"retrieve-context-test"` was already being silently discarded before this spec, not merely repurposed by it). (a) was rejected: §5.1's `_assemble` test helper (already in-scope, already being rewritten by this spec, not "untouched") relies on `entity_ids` being the *first* positional parameter (`assemble_retrieve_context(entity_ids, budget_tokens=budget_tokens, db_connection=self.conn)`) -- reordering the signature would just move the identical class of silent-rebinding bug onto that call site instead of fixing it, at the cost of also touching production code to preserve two already-legacy test calls' incidental syntax. (b) is smaller, touches only the two lines actually responsible, and needs no production-code change beyond what §2.2 already specifies.

**Resolution**: these two tests are no longer described as fully untouched. Every line in each stays exactly as-is (setup, patches, assertions, disposition) **except** the one call line, which changes from positional to keyword form, dropping the dead `"retrieve-context-test"` argument entirely (it corresponded to `owner_id`, fully removed from the signature per §1/§2.2, and was never consumed by the `global` path in the first place):
```python
result = assemble_retrieve_context(
    query=query,
    strategy="global",
    budget_tokens=1000,
    db_path=self.db_path,
)
```
for `test_global_populated_leaf_community_uses_community_pipeline_only` (was lines 718-724 of the pre-amendment file), and
```python
result = assemble_retrieve_context(
    query=query,
    strategy="global",
    db_connection=self.conn,
)
```
for `test_global_without_communities_returns_empty_hierarchy_envelope` (was lines 824-829 of the pre-amendment file). Their internal `patch("saltmdb.domain.services.retrieve_context_service.memory_service.search_memory")` context managers, and every other line of both tests, are still genuinely untouched -- do not "clean these up," their continued presence is exactly why §2.1 keeps the `memory_service` import alive. This amendment does not reopen §5.1-§5.5/§5.7 or interact with Amendments 1/2 -- a separate, independently-discovered gap in the same locked section.

**5.7 — `_assert_empty_envelope` helper (line 151)**: after §5.3/§5.4's edits, every one of its four callers (lines 203, 213, 654, 899 of the pre-change file) is gone — three deleted outright (§5.4), one (`test_explicit_local_strategy_matches_omitted_strategy`) rewritten to assert a populated result instead (§5.3). Remove this helper method entirely rather than leave it dead.

## 6. `tests/test_retrieve_context_wiring.py`

**6.1 — `test_dispatch_forwards_omitted_optional_values_as_none` (lines 50-64)**: replace the call and assertion:

```python
    def test_dispatch_forwards_omitted_optional_values_as_none(self):
        with patch.object(
            dispatch.retrieve_context_service,
            "assemble_retrieve_context",
            return_value={},
        ) as assemble:
            dispatch._dispatch_retrieve_context(entity_ids=["e1"])

        assemble.assert_called_once_with(
            entity_ids=["e1"],
            budget_tokens=None,
            strategy="local",
        )
```

**6.2 — `test_dispatch_requires_string_query_but_allows_empty_string` (lines 66-88)**: replace entirely -- rename to `test_dispatch_requires_nonempty_entity_ids_list`, covering: missing `entity_ids` (`VALIDATION_ERROR`), `entity_ids=[]` (`VALIDATION_ERROR`), `entity_ids=["ok", 123]` (`VALIDATION_ERROR`, non-string member), and a valid `entity_ids=["e1", "e2"]` call reaching the (mocked) service with both ids forwarded unchanged. Add two more assertions to this same test method (not a new method -- §9's expected test count assumes no new method here): `dispatch._dispatch_retrieve_context(entity_ids=["e1"], query="q")` (both parameters supplied, no explicit `strategy`, defaulting to `"local"`) returns `VALIDATION_ERROR` with `field: "query"`, per §4.1's new cross-parameter check -- and its mirror, `dispatch._dispatch_retrieve_context(query="q", entity_ids=["e1"], strategy="global")` returns `VALIDATION_ERROR` with `field: "entity_ids"`.

**6.3 — `test_public_schema_exposes_query_controls_without_owner_id` (lines 90-97)**: update the expected parameter list:

```python
    def test_public_schema_exposes_query_controls_without_owner_id(self):
        self.assertEqual(
            list(inspect.signature(tools.retrieve_context).parameters),
            ["entity_ids", "query", "budget_tokens", "strategy"],
        )
        self.assertNotIn("owner_id", inspect.signature(tools.retrieve_context).parameters)
        registered = tools.mcp._tool_manager._tools["retrieve_context"]
        self.assertNotIn("owner_id", registered.parameters.get("properties", {}))
```

**6.4 — `test_public_tool_reaches_real_dispatch_and_returns_envelope` (lines 99-118)**: replace the query-based fixture/call with an entity_ids-based one:

```python
    def test_public_tool_reaches_real_dispatch_and_returns_envelope(self):
        result = store_memory(
            content="Wiring end-to-end memory fixture body",
            title="Wiring end-to-end memory",
            owner_id="wiring-owner",
            db_connection=self.conn,
        )
        if not isinstance(result, dict):
            self.fail(f"fixture store failed: {result}")
        self.assertEqual(result["status"], "ok")
        entity_id = result["data"]["id"]

        envelope = tools.retrieve_context(entity_ids=[entity_id])

        self.assertEqual(
            set(envelope),
            {"entity_ids", "memories", "edges", "lineage", "conflict_sets", "metadata"},
        )
        self.assertEqual(envelope["entity_ids"], [entity_id])
```

**6.5 — `test_registration_and_protocol_classification_are_read_only` (lines 44-48)**: untouched — asserts dispatch-table/protocol registration only, no parameter shape involved.

## 7. `tests/test_orphan_community_service.py`

**7.1 — `test_scenario_14_assemble_retrieve_context_composes_orphan_assignment_end_to_end` (lines 387-441)**: this test already creates and holds an `orphan` fixture id (line 390) and was relying on its query string `"orphan composition query"` to match that fixture via search. Replace the call at lines 408-413:

```python
        result = assemble_retrieve_context(
            "orphan composition query",
            "orphan-community-test",
            limit=1,
            db_connection=self.conn,
        )
```

with:

```python
        result = assemble_retrieve_context(
            entity_ids=[orphan],
            db_connection=self.conn,
        )
```

Every assertion below this call (lines 415-441) is about the orphan-community mechanism (C.5, untouched) and needs no further change.

## 8. Out of scope

- **`strategy="global"` behavior and seeding mechanism** — `_assemble_global_context`, `community_retrieval_service.py`, and `global`'s own use of `query`/`owner_id` are unchanged. Its own entity-anchored re-seeding redesign is tracked separately in wayfinder ticket `3c612f02`, not yet grilled. The public `retrieve_context` tool's transitional dual-parameter signature (§3.1) is the mechanical consequence of this split, not new scope creep into `global`'s own behavior.
- **`search_memory(mode="strict")` itself** — this spec removes retrieve_context's one call site into it; the shared gate's own calibration is untouched, per the resolved ticket's explicit decoupling.
- **G3/G4/G7/G8's internal mechanics** — `context_expansion_service.py`, `conflict_set_service.py`, `context_budget_service.py`, `lineage_assembly_service.py` are not edited. §1 explains in detail why the `score`-placeholder approach makes this true rather than merely asserting it.
- **C.5's orphan-to-community assignment mechanism** (`orphan_community_service.py`) — reused as-is; it already keys off a primary/anchor entity id regardless of how that id was obtained, so no change is needed for it to keep working under `entity_ids` anchoring (confirmed by §7's minimal test-only edit).
- **`get_memory`, `resolve_entity_ref`, `saltmdb.utils.envelope`, `_required_str_list`** — all reused verbatim, not modified.
- **Renaming or restructuring anything beyond what §2-§7 name explicitly** — no drive-by cleanup, no touching adjacent functions in any edited file.
- **`src/saltmdb/mcp/prototype_retrieve_context.html`** — a static schema-illustration prototype artifact (predates this spec, from the original result-JSON-schema prototyping round) that also hardcodes the string `"primary_search"` (line 348), found via the same whole-tree grep that caught the two real test assertions in §5.2. It is not imported or exercised by any test and does not affect the Acceptance bar in §9. Left stale deliberately — updating every schema-illustration artifact whenever the live schema changes is not this spec's job; flagged here so it is a documented, deliberate exclusion rather than a silent miss.

## 9. Acceptance

Run from the repo root, with `src` on `PYTHONPATH`:

```bash
PYTHONPATH=src uv run pytest tests/test_retrieve_context_service.py tests/test_retrieve_context_wiring.py tests/test_orphan_community_service.py -q
```

Must exit 0. Pre-change baseline (run fresh this session, on `context-aware-search` tip `c7e977f`, before any edit in this spec): **40 passed** (`test_retrieve_context_service.py`: 20, `test_retrieve_context_wiring.py`: 5, `test_orphan_community_service.py`: 15). Post-change expected count, worked from §5-§7's exact dispositions, not re-derived: `test_retrieve_context_service.py` goes from 20 to 20 − 4 (§5.4 deletions) + 2 (§5.5 new) = 18 (renames in §5.3 change no count); `test_retrieve_context_wiring.py` stays at 5 (§6.2 replaces one method's body with a wider one covering more sub-cases, still one method — see §6.2's own wording); `test_orphan_community_service.py` stays at 15 (one test's body edited, §7.1, none added or removed). Total: **38 passed, 0 failed**. This is an exact expected count, not a range — a different total means either an §5-§7 disposition was missed or an extra test was added/removed that this spec did not call for; verify by diffing the actual test-method list against §5-§7's own enumeration, not just the aggregate number.

Additionally, confirm no remaining reference to the removed call shape anywhere in the tree. This exact command was run against the pre-change tree at lock time and found exactly 8 matching call sites, individually traced to their owning test/function: the `_assemble` helper definition (§5.1), `test_one_connection_is_shared_across_search_and_graph_pipeline` (§5.2, converts), `test_connection_only_passes_connection_database_path_to_search` (§5.4, deletes), `test_global_populated_leaf_community_uses_community_pipeline_only` and `test_global_without_communities_returns_empty_hierarchy_envelope` (§5.6 — **per Amendment 3, each gets exactly one line rewritten from positional to keyword form**; on the pre-change tree read at lock time both called `assemble_retrieve_context(query, "retrieve-context-test", strategy="global", ...)` positionally), `test_explicit_local_strategy_matches_omitted_strategy` (§5.3, rewrites), `dispatch.py`'s one call (§4.1, restructured — its `global` branch keeps matching), and `test_orphan_community_service.py`'s scenario 14 (§7.1, converts). `-U` (multiline mode) is required, since every real call site in this codebase wraps its arguments onto the following line rather than passing them inline:

```bash
rg -U -n 'assemble_retrieve_context\(\s*\n?\s*(query\b|"[^"]*")' tests/ src/
```

After the change, this must return **exactly three two-line matches, no more, no fewer**: `src/saltmdb/daemon/dispatch.py`'s `global` branch (§4.1), and the two global-strategy tests named above in `tests/test_retrieve_context_service.py` (§5.6, now in their Amendment-3 keyword form — the regex matches `query=query` on the line right after the opening paren identically to how it matched bare `query`, verified live: `query\b` is word-boundary-terminated by the following `=`, so this pattern's match count and the exact three sites it identifies are unaffected by Amendment 3) — all three are the in-scope-preserved `global` path, not a missed `local` conversion. Any match in `tests/test_orphan_community_service.py`, any match on `test_one_connection_is_shared_across_search_and_graph_pipeline`'s or `test_explicit_local_strategy_matches_omitted_strategy`'s own lines, or a fourth/missing match anywhere, means a call site named in §5/§7 was missed or a `global`-path test was incorrectly touched.

Finally:

```bash
PYTHONPATH=src uv run ruff check src/saltmdb/domain/services/retrieve_context_service.py src/saltmdb/mcp/tools.py src/saltmdb/daemon/dispatch.py
PYTHONPATH=src uv run ruff format --check src/saltmdb/domain/services/retrieve_context_service.py src/saltmdb/mcp/tools.py src/saltmdb/daemon/dispatch.py
```

Both must exit 0.
