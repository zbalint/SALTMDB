# SPEC-CONTEXT-RETRIEVAL-A5-RESULT-ENVELOPE-AND-MCP-TOOL

## 1. Why

This is slice **A5** of Milestone A ("local graph-aware context retrieval," the `retrieve_context`
MCP tool) from the `wayfinder:saltmdb:graph-aware-context-retrieval` roadmap — the final slice, per
the plan locked in memory `b9ec8a0e-04ca-4e9e-91af-03306a53f50d` (elaborating `bed2478c`). Slices
A1 (predicate-allowlist + fan-out-cap expansion engine), A2 (contradiction/conflict-set assembly),
A3 (lineage assembly with contradiction-flag propagation), and A4 (context-budget packing) are all
done, reviewed, and merged into `context-aware-search` as of commit `a2bf8fd` (the A4 merge landed
2026-09-12 specifically to unblock this slice — see memory `84da9f45` for A4's own review, and the
branch-strategy decision at memory `64658415`).

A5 does two things neither prior slice does:

1. **Wires A1-A4's four independently-shipped output dicts into the one locked result envelope** —
   standing constraint 13 (memory `64b8e514`, refined by the `ce08d466` prototype ticket) — by
   calling all four in sequence against a shared primary-hit set and a shared point-in-time, then
   assembling their outputs into `{query, memories, edges, lineage, conflict_sets, metadata}`.
2. **Registers `retrieve_context` as a new, standalone MCP tool** per ticket G2's 3-part acceptance
   bar (memory `c1ac7da1`), wiring it through all three places a new read-only MCP tool touches in
   this codebase: `mcp/tools.py` (the public Python wrapper), `daemon/dispatch.py` (the
   `DISPATCH_TABLE` entry), and `daemon/protocol.py` (the `READ_TOOLS` RPC classification).

**Request-parameter signature — resolved this session, was previously "Not yet specified" in the
wayfinder map** (`64b8e514`'s own "Not yet specified" section, closed via `AskUserQuestion` with
zbalint 2026-09-12, all three recommended options accepted):

- `query: str` — **required**, query-only. No `entity_ids` seed parameter: no committed acceptance
  criterion across G1-G8 or the schema-prototype ticket (`73d1459a`) ever exercises seeding context
  assembly from known entity ids instead of a fresh search — every one of the prototype's three
  validated scenarios derives `primary_hits` from a query. Adding one now would be exactly the
  "speculative knob" G2's acceptance-bar rule 1 forecloses. An entity-id-seed mode remains addable
  later as a pure superset if live usage ever shows a real need (`5d3f073c`'s live-usage-first
  posture), without breaking this signature.
- `limit: int | None = None` — **caller-exposed**, a thin pass-through to the internal
  `search_memory` call's own `limit` parameter (server default 5, matching `search_memory`'s own
  default exactly). This is not a new mechanism; it is an existing, already-public knob on the
  primitive this tool composes, and omitting it would fail G2's own rule 3 (a caller wanting more
  or fewer primary hits would otherwise have no way to get that from `retrieve_context` without
  falling back to raw `search_memory` + client-side composition — the exact thing this tool exists
  to make unnecessary).
- `budget_tokens: int | None = None` — **caller-exposed**, already locked by G8: server default
  (`CONTEXT_BUDGET_DEFAULT_TOKENS`) with a hard ceiling (`CONTEXT_BUDGET_MAX_TOKENS`), both existing
  constants from A4 (`src/saltmdb/config.py:288,295`). Passed straight through to
  `pack_context_budget`'s own `budget_tokens` parameter, which already implements the clamp.
- Everything else stays fixed/server-defaulted, no caller override, for Milestone A: the G3
  predicate allowlist, the G4 fan-out cap, traversal depth (always 1 hop) and direction (always
  `"both"`), and `point_in_time` (always "now" at call time — no temporal query support; that is
  Milestone E's job per the wayfinder map's own phase boundary, not this slice's).

**A cross-slice interaction gap this slice must resolve, not inherited from any single upstream
spec** — worked through below because no prior slice's spec had the full picture to catch it:

A2's G7 implementation locks "an unresolved conflict set surfaces with strictly equal visibility
across all members" (standing constraint 11). A4's G8 implementation locks "primary hits are not
exempt from the token budget" and "the conflict-set reserve is separate/additive, never a shared or
competing pool" (standing constraint 12) — but A4's own bypass-the-budget mechanism only ever
applies to `conflict_only`-inclusion members (entities with *no other route* into context). A2's own
`inclusion` precedence rule (`primary` > `expansion` > `conflict_only`) means a member of an
unresolved conflict set that *also* happens to be a primary hit or an A1 expansion candidate is
tagged `"primary"`/`"expansion"`, **not** `"conflict_only"` — which means A4 runs it through the
*ordinary*, competing budget pass with no special treatment at all. Concretely: take a primary hit
`P` and an out-of-network expansion candidate `X` where `P contradicts X` and the pair is
lifecycle-unresolved. `P` is `inclusion: "primary"`; if `P` also happens to be one of the last,
lowest-relevance items in a token-starved primary list, A4's greedy pass can legitimately drop it —
while `X`, ranked highly for mutual-neighbor-count, survives as `expansion`. The conflict set is
still reported once (via `conflict_sets`), but `memories[]` would then contain `X` without `P`,
which is exactly the "partial hiding of one member" G7's own text explicitly forbids ("truncation
is whole-set-or-nothing... never partially hiding one member" — the sentence is about A2's own
*set-admission* cap, but the underlying equal-visibility invariant it protects is violated just the
same by an *uneven survival* through A4's downstream budget pass, which A2's own spec never
anticipated since A2 has no visibility into A4's later truncation at all).

Per this project's standing rule (memory `74f6b4c0`: "we do not accept anything as a limitation as
long as we can implement or fix it and it does not require anything beyond our jurisdiction" — the
same rule that drove A2 Amendment 1's Q7 fix), this is fixed here, in A5, as a pure post-processing
reconciliation step over A1/A2/A4's *outputs* — **zero changes to A1/A2/A4's own already-shipped,
already-reviewed code**. §2.1 step 8 below is the fix: any entity dropped by A4's budget pass that
is also a member of an *admitted* (kept, not capped-out) unresolved conflict set is force-recovered
into the final `memories[]`, exactly as `conflict_only` members already are, and the `metadata`
truncation/usage signals are recomputed from the final, reconciled membership rather than passed
through from A4 raw. This keeps G7's equal-visibility invariant true at the one point in the
pipeline (A5) that can see both G7's and G8's outputs at once — neither A2 nor A4 individually can.

## 2. New file: `src/saltmdb/domain/services/retrieve_context_service.py`

### 2.1 `assemble_retrieve_context` — the slice's one public function

```python
def assemble_retrieve_context(
    query: str,
    owner_id: str | None,
    *,
    limit: int | None = None,
    budget_tokens: int | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
```

**Input contract**: `query` is the caller's search text, passed through to `search_memory` as
`query_keywords` unchanged — including when falsy (`""`). This slice adds no query validation or
special-casing of its own beyond what `search_memory` already does for a falsy `query_keywords`
(its existing tags/filters-only browse-mode behavior governs; since this slice passes no
`tags_filter`/`memory_type_filter`/etc., that path's own existing behavior is what a falsy `query`
gets — not re-derived or overridden here, per Coding Standards rule 3/4, reuse over reinvention).
`owner_id` is threaded straight through to the internal `search_memory` call exactly as the
`mcp/tools.py` wrapper resolves it (see §3) — this function itself does no owner resolution.

**Algorithm** (implement exactly this sequence):

1. Open one connection (`db_connection` if given, else `get_connection(db_path or get_db_path())`,
   closing it at the end only if this function opened it) — mirror A1-A4's own `should_close`
   pattern exactly. Thread this same connection through **every** call below, including the
   internal `search_memory` call (its own signature already accepts `db_connection`) — A1-A4
   already established the "one caller opens, everyone else reuses" precedent this slice must not
   break for its own four calls. **Corrected by Amendment 4** — also resolve `effective_db_path`
   here (see Amendment 4 for why this is a separate, additional value from the raw `db_path`
   parameter, and why `search_memory` itself, unlike A1-A4's own primitives, cannot fully collapse
   to this one connection regardless):
   ```python
   if db_path is not None:
       effective_db_path = db_path
   elif db_connection is not None:
       row = conn.execute("PRAGMA database_list").fetchone()
       effective_db_path = row[2] or None
   else:
       effective_db_path = db_path or get_db_path()
   ```
2. `pit = datetime.now(UTC).isoformat()` — one shared point-in-time for this entire call, passed
   to every A1/A2/A3 call below (A4 takes no `point_in_time` parameter — it does no traversal).
   Mirrors A1's own "every per-hit traversal sees the same bitemporal snapshot" rationale, extended
   here to mean every *slice* invoked by this orchestration sees the same snapshot, not just every
   hit within one slice.
3. Call `search_memory`:
   ```python
   search_result = memory_service.search_memory(
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
   ```
   **Corrected by Amendment 4**: this passes `effective_db_path` (step 1), not the raw `db_path`
   parameter — see Amendment 4 for the real bug this closes (a caller supplying `db_connection`
   without `db_path` would otherwise silently point `search_memory`'s own internal semantic/
   vector/chunk/topic-scoring sub-searches at the wrong database).
   `mode="strict"` (not `search_memory`'s own tool-level default `"broad"`) is a deliberate choice:
   `strict` applies `search_memory`'s existing relevance-abstention behavior (memory `c27792a1`),
   returning `[]` for a query with no sufficiently-relevant candidate rather than padding
   `retrieve_context`'s primary-hit set with noise — this is what makes a query-only call's default
   behavior "safe" per G2's acceptance-bar rule 2, without this slice inventing any new abstention
   logic of its own. `include_related=False` because this slice does its own graph traversal via A1
   immediately after — `search_memory`'s own `related_entities` field would be redundant work
   thrown away. Every other parameter (`context_id`, `agent_session_id`, `tags_filter`,
   `memory_type_filter`, `is_core`, `cursor`) is fixed to its "no filter" value — no G1-G8 ticket
   ever asked for `retrieve_context` to expose corpus-scoping knobs, and adding them now would be
   exactly the same class of speculative-knob violation the `entity_ids` seed parameter was
   rejected for in §1 above.

   **Corrected by Amendment 3** — see that section for the verified real shape: with
   `return_diagnostics=False` (as called above), `search_memory`'s own internal-exception path
   returns `[{"error": str(e)}]` — a **list** containing one sentinel dict with only an `"error"`
   key, never a bare dict (the dict-shaped `{"results": [...], "diagnostics": ..., "error": ...}`
   return is `orchestrator.py`'s `return_diagnostics=True` branch only, which this call never
   takes). Guard for this defensively, mirroring A1's own "log and treat as empty, don't fail the
   whole call" posture for a bad primary hit:
   ```python
   search_hits = search_result
   if search_hits and "id" not in search_hits[0]:
       logger.warning(
           "search_memory reported an internal error for query %r: %s",
           query, search_hits[0].get("error"),
       )
       search_hits = []
   ```
   The check is `"id" not in search_hits[0]` rather than a length/shape check on the whole list,
   because that is the one property that reliably distinguishes the error sentinel from a real
   result item: every real item `orchestrator.py`'s success path constructs always carries an
   `"id"` key (verified directly against its literal `item = {...}` dict-literal construction),
   while the error sentinel never does.
4. Build, from `search_hits` (before any later truncation/dropping):
   - `primary_hits: list[dict] = [{"id": h["id"], "score": h["score"]} for h in search_hits]` — the
     exact `{id, score}` shape A1/A2/A3/A4 all require.
   - `primary_meta: dict[str, dict] = {h["id"]: {"title": h["title"], "memory_type":
     h["memory_type"]} for h in search_hits}` — reused for primary rows in step 9 below; this slice
     never re-fetches title/memory_type for a primary hit from `entities`, since `search_memory`'s
     own result already carries both, fresher than a second query would be and free of an
     additional round trip (Coding Standards rule 1/2: don't build a new query when the data is
     already in hand).
   - `original_rank: dict[str, int] = {h["id"]: i + 1 for i, h in enumerate(search_hits)}` — 1-indexed
     rank in `search_memory`'s own returned order, captured **before** any budget truncation. This
     is the `rank` value every primary row's `retrieval_provenance` carries in step 9, regardless of
     whether that hit is later dropped-then-recovered by step 8 — rank describes *search* position,
     not *survival* position, and must not be recomputed after truncation (a post-truncation
     re-rank would silently renumber, e.g., hit #1 and #3 as #1 and #2 if #2 was dropped, losing the
     original search ordering information entirely).
5. `expansion_result = expand_context_candidates(primary_hits, point_in_time=pit, db_connection=conn)`
6. `conflict_sets_result = assemble_conflict_sets(expansion_result, primary_hits, point_in_time=pit, db_connection=conn)`
7. `budget_result = pack_context_budget(expansion_result, primary_hits, conflict_sets_result, budget_tokens=budget_tokens, db_connection=conn)`
8. `lineage_result = assemble_lineage(expansion_result, primary_hits, point_in_time=pit, db_connection=conn)`

   (A3 depends only on `expansion_result`+`primary_hits`, per the wayfinder map's own explicit
   correction — memory `b9ec8a0e`'s "CORRECTED 2026-09-12" note — that A3 has no data dependency on
   A2's output at all. Called here after A4 rather than before purely for call-order readability;
   there is no ordering dependency between step 7 and step 8, and swapping them would not change
   this function's output.)

9. **Equal-visibility reconciliation** (§1's cross-slice fix — implement exactly this, all pure
   Python over already-computed dicts, no new queries):
   ```python
   member_to_conflict_set: dict[str, str] = {}
   for i, cs in enumerate(conflict_sets_result["conflict_sets"]):
       cs_id = f"cs-{i + 1}"
       for member in cs["members"]:
           member_to_conflict_set[member["id"]] = cs_id

   recovered_primary = [
       eid for eid in budget_result["dropped_entity_ids"]["primary"]
       if eid in member_to_conflict_set
   ]
   recovered_expansion = [
       eid for eid in budget_result["dropped_entity_ids"]["expansion"]
       if eid in member_to_conflict_set
   ]
   true_primary_dropped = [
       eid for eid in budget_result["dropped_entity_ids"]["primary"]
       if eid not in member_to_conflict_set
   ]
   true_expansion_dropped = [
       eid for eid in budget_result["dropped_entity_ids"]["expansion"]
       if eid not in member_to_conflict_set
   ]
   final_primary_ids = budget_result["packed_entity_ids"]["primary"] + recovered_primary
   final_expansion_ids = budget_result["packed_entity_ids"]["expansion"] + recovered_expansion
   recovered_tokens_used = sum(
       budget_result["token_counts"][eid] for eid in recovered_primary + recovered_expansion
   )
   ```
   Every `member_to_conflict_set` lookup only ever needs `budget_result["dropped_entity_ids"]`, not
   `["packed_entity_ids"]`, because recovery only ever *adds back* a dropped id — a packed id is
   already present and needs no action. `member_to_conflict_set` is built from **every** member of
   **every** admitted conflict set (not filtered to `conflict_only` members) because A2's own
   `inclusion`-precedence rule already guarantees a `"primary"`/`"expansion"`-inclusion member is
   exactly one of `primary_hits`/`expansion_result["expansion_candidates"]` — the same universe
   `budget_result`'s `dropped_entity_ids`/`packed_entity_ids` partition — so no member can fall
   outside that lookup's applicability. Order: recovered ids are appended after the normally-packed
   ones (not interleaved back into rank/mutual-neighbor-count order) — this is a deliberate
   simplicity choice for a genuinely rare edge case (a real `contradicts` edge touching a
   budget-competing hit), not a claim that recovered items are less relevant; nothing in G7 dictates
   a required position for a recovered item, only that it must be *present*.

   `budget_result["packed_entity_ids"]`/`["dropped_entity_ids"]`/`["conflict_only_entity_ids"]`
   themselves are never mutated — recomputation happens entirely in the local variables above, so a
   caller inspecting `budget_result` directly (e.g. a future caller, or a test) still sees A4's own
   literal, unreconciled output.
10. Build `memory_type_by_id` for every id that needs a fetch: `search_memory` already gave
    `memory_type` for every primary id (step 4); A1's `expansion_candidates` and A2's `conflict_sets`
    members carry `title` (and, for `conflict_only` members, `status`) but **neither carries
    `memory_type`** — grounded against A1's `ExpansionNode`/`RankedNode` TypedDicts and A2's
    `entity_info` fetch (`SELECT id, title, status FROM entities ...`,
    `conflict_set_service.py:198`), neither of which selects `memory_type`. One batched fetch closes
    this gap for exactly the ids that need it:
    ```python
    needs_memory_type = set(final_expansion_ids) | set(budget_result["conflict_only_entity_ids"])
    memory_type_by_id: dict[str, str] = {}
    if needs_memory_type:
        placeholders = ",".join("?" for _ in needs_memory_type)
        rows = conn.execute(
            f"SELECT id, memory_type FROM entities WHERE id IN ({placeholders})",
            tuple(needs_memory_type),
        ).fetchall()
        memory_type_by_id = {row[0]: row[1] for row in rows}
    ```
    Mirrors A4's own single-purpose batched-fetch style (`context_budget_service.py` step 4)
    exactly, including the zero-input skip (no query issued when `needs_memory_type` is empty).
    Primary ids are deliberately excluded from this query — `primary_meta` (step 4) already has
    their `memory_type` from `search_memory`'s own result, at zero extra query cost.

    A missing row (a stale reference — same defensive posture A2 and A4 already give a missing
    entity row) falls back to the literal string `"unknown"` via `memory_type_by_id.get(eid,
    "unknown")` at the point of use in step 11 — reusing, not reinventing, this project's existing
    missing-entity-fallback convention (memory `299fbf4f`: A2's own conflict-set assembly reuses
    `_lineage_node`'s `"Unknown"` precedent for a missing title; this slice's `memory_type` fallback
    follows the identical spirit for a different field). This sentinel is a display-layer choice
    only — it is never written back to the `entities` table, so the column's `CHECK(memory_type IN
    (...))` constraint is not implicated.

11. Build `conflict_only_meta` from `conflict_sets_result["conflict_sets"]` members with
    `inclusion == "conflict_only"` — `{member["id"]: {"title": member["title"], "status":
    member["status"]} for cs in conflict_sets_result["conflict_sets"] for member in cs["members"]
    if member["inclusion"] == "conflict_only"}`. A2 already fetched exactly this `title`/`status`
    pair for these ids (`conflict_set_service.py` §3.2 step 9's `entity_info` lookup) — reused
    verbatim here rather than re-fetched, per Coding Standards rule 1/16.

12. Assemble `memories`, in this fixed order — primary, then expansion, then conflict-only (matches
    the schema-prototype's own validated assembly order):
    ```python
    memories: list[dict] = []
    for eid in final_primary_ids:
        memories.append({
            "entity_id": eid,
            "title": primary_meta[eid]["title"],
            "memory_type": primary_meta[eid]["memory_type"],
            "inclusion": "primary",
            "retrieval_provenance": [{"reason": "primary_search", "rank": original_rank[eid]}],
        })

    candidates_by_id = {c["entity_id"]: c for c in expansion_result["expansion_candidates"]}
    for eid in final_expansion_ids:
        candidate = candidates_by_id[eid]
        memories.append({
            "entity_id": eid,
            "title": candidate["title"],
            "memory_type": memory_type_by_id.get(eid, "unknown"),
            "inclusion": "expansion",
            "retrieval_provenance": candidate["retrieval_provenance"],
        })

    for eid in budget_result["conflict_only_entity_ids"]:
        meta = conflict_only_meta[eid]
        memories.append({
            "entity_id": eid,
            "title": meta["title"],
            "memory_type": memory_type_by_id.get(eid, "unknown"),
            "inclusion": "conflict_only",
            "retrieval_provenance": [{
                "reason": "contradicts_force_include",
                "conflict_set_id": member_to_conflict_set[eid],
            }],
        })
    ```
    A conflict-only row's `retrieval_provenance` uses the `contradicts_force_include` reason and
    `conflict_set_id` shape validated live during the `ce08d466` schema-prototype ticket
    (`prototype_retrieve_context.html:358`) — not previously locked into any G-ticket text verbatim,
    since G6 only ever defined the `primary_search` and graph-expansion provenance shapes; this is
    this slice's own explicit extension of G6's provenance concept to the one inclusion reason G6
    never enumerated, using the one shape this project already prototyped and validated for it.
13. `edges = expansion_result["in_network_edges"]` — passed through **verbatim**, no added
    `direction` field. A1's own `in_network_edges` shape (`{relation_id, source_id, target_id,
    predicate}`, `context_expansion_service.py`'s Output contract) has no direction field because
    both endpoints of an in-network edge are already primary hits — direction is not a property of
    the edge itself, only of "which primary hit reached which node," which is exactly what each
    *expansion* row's own `retrieval_provenance` already carries per-candidate. Adding a synthetic
    direction to `in_network_edges` here would invent a field no upstream contract defines and no
    G-ticket requires; this slice does not do that.
14. Build `conflict_sets` output:
    ```python
    conflict_sets_output = [
        {"id": f"cs-{i + 1}", "members": cs["members"], "edges": cs["edges"]}
        for i, cs in enumerate(conflict_sets_result["conflict_sets"])
    ]
    ```
    Adds only the `id` field (needed functionally — step 9's `member_to_conflict_set` and every
    `conflict_only` row's `conflict_set_id` reference it) on top of A2's own `members`/`edges`
    shape, otherwise unchanged. Deliberately does **not** add a `resolution` field: A2's own Output
    contract already omits one (every admitted set is unresolved by construction — lifecycle-
    resolved components never reach `conflict_sets` at all), and that omission is A2's own already-
    locked decision, not an oversight this slice should second-guess or pad back in.
15. Build `metadata`:
    ```python
    metadata = {
        "strategy": "local",
        "fan_out": {
            **expansion_result["fan_out"],
            "conflict_reserve": conflict_sets_result["contradicts_cap"],
        },
        "budget": {
            "unit": budget_result["budget"]["unit"],
            "limit": budget_result["budget"]["limit"],
            "used": budget_result["budget"]["used"] + recovered_tokens_used,
            "primary_truncated": len(true_primary_dropped) > 0,
            "primary_dropped_count": len(true_primary_dropped),
            "expansion_truncated": len(true_expansion_dropped) > 0,
            "expansion_dropped_count": len(true_expansion_dropped),
            "conflict_reserve_tokens_used": (
                budget_result["budget"]["conflict_reserve_tokens_used"] + recovered_tokens_used
            ),
        },
    }
    ```
    `strategy: "local"` is a constant for Milestone A (no other retrieval strategy exists yet;
    Milestone D is the future "global" counterpart per the wayfinder map's phase list) — matches the
    field name already used in the schema-prototype ticket's validated shape.
    `fan_out.conflict_reserve` nests A2's own `contradicts_cap` dict verbatim under `metadata.fan_out`
    — this key path is this slice's own judgment call (no G-ticket locks the exact nesting), placed
    here because G7's own text describes the conflict-set cap as extending G4's fan_out signal
    ("truncation is... always surfaced explicitly, extending G4's `expansion_truncated` signal to
    this granularity" — memory `64b8e514`'s G7 resolution). `metadata.budget.*`'s `used`,
    `*_truncated`, `*_dropped_count`, and `conflict_reserve_tokens_used` fields are the **reconciled**
    values from step 9 — not `budget_result["budget"]`'s own raw fields — since a naive pass-through
    would under-report `used`/`conflict_reserve_tokens_used` and over-report the two
    `*_dropped_count` fields for every entity step 9 force-recovered. `unit` and `limit` pass through
    unchanged (recovery never changes the effective budget ceiling, only what beyond it gets force-
    included).
16. Return:
    ```python
    {
        "query": query,
        "memories": memories,
        "edges": edges,
        "lineage": lineage_result,
        "conflict_sets": conflict_sets_output,
        "metadata": metadata,
    }
    ```

**Worked example** (concrete instance, per this workspace's spec-writing pre-lock gate step 8 —
traces steps 6-15 together for the exact scenario §1 identifies): primary hit `P` (rank 1, the
*only* primary hit, `score=0.9`), out-of-network expansion candidate `X` (reached from `P` via
`depends_on`, ranks first among expansion candidates). A `contradicts` edge exists between `P` and
`X`, lifecycle-unresolved (no supersession chain covers either). `budget_tokens` is set low enough
that `P` alone exhausts it, so `X` is never even reached by A4's greedy pass (it is still recorded
in `expansion_dropped`, since A4's loop never `break`s — it always evaluates every candidate).
Tracing: A2's `conflict_sets_result` has one set, members `[{id: P, inclusion: "primary", title:
None, status: None}, {id: X, inclusion: "expansion", title: None, status: None}]` (both `title`/
`status` are `None` per A2's own precedence rule — neither is `conflict_only`). A4's
`budget_result["dropped_entity_ids"]` = `{"primary": [], "expansion": ["X"]}` (P is packed — it's
first and fits; X is dropped — no budget left). Step 9: `member_to_conflict_set = {"P": "cs-1", "X":
"cs-1"}`; `recovered_expansion = ["X"]` (X is dropped *and* a conflict-set member); `true_
expansion_dropped = []` (X was the *only* expansion drop, and it's now recovered — so 0 true drops,
`expansion_truncated: False` even though A4's own raw `expansion_dropped_count` would have said 1).
`final_expansion_ids = [] + ["X"] = ["X"]`. Step 12: `X` gets an `expansion`-inclusion row with its
original A1 `retrieval_provenance` (not a `conflict_only` row — its structural path into context
really is graph expansion; the conflict is *why* the budget didn't get to exclude it, not *how* it
was found). Final `memories[]` contains both `P` and `X` — G7's equal-visibility invariant holds.
`metadata.budget.used` includes `X`'s token cost (via `recovered_tokens_used`), so the reported
`used` figure genuinely reflects everything actually returned, not a number that undercounts what
the caller received.

## 3. `src/saltmdb/mcp/tools.py`

**Corrected by Amendment 3 — this section now has two parts, not one.** The original text below
("pure insertion, no existing tool's line range is touched") was wrong: `retrieve_context` also
needs one line added to the existing `_OWNER_INJECTED_TOOLS` frozenset (lines 94-116). See
Amendment 3 for why this is a real, production-significant requirement, not an optional
enhancement.

**Part A — add `"retrieve_context"` to `_OWNER_INJECTED_TOOLS`** (line 94's frozenset literal),
in alphabetical-ish position matching the existing list's own ordering (grouped after
`review_core_memory`, since the existing list is not strictly alphabetical — e.g. `get_memory`
precedes `inspect_memory` which precedes `get_lineage` — so this addition is placed at the end of
the literal, after `review_core_memory`, rather than forcing a new ordering convention onto an
existing list this slice does not otherwise touch):

```python
_OWNER_INJECTED_TOOLS = frozenset(
    {
        # ... existing 12 entries, unchanged ...
        "review_core_memory",
        "retrieve_context",
    }
)
```

This is required because `RpcBackend.call()` (the only backend used in real production adapter
runtime) strips the `owner_id` key entirely from any tool's kwargs unless that tool's name is in
this set (`tools.py:150`), re-injecting a freshly-computed, trustworthy value only for members of
the set (`tools.py:147-148`). `retrieve_context` calls `search_memory` internally with a real
`owner_id` — without this addition, every production call to `retrieve_context` would silently run
with `owner_id=None`, changing which memories are visible to it, with no error and no test
(`DirectDispatchBackend`, used by every test that doesn't specifically target `RpcBackend`, never
strips or re-injects this key, so it would have masked the bug indefinitely). `retrieve_context`
is not a candidate for the `log_event`-style exception (the one documented reason a tool might
legitimately stay out of this set): it has no ownership-neutral contract, and its own dispatch
function (§4) has a real `owner_id` parameter with nothing else binding ownership before that
point, unlike `log_event`'s own tools.py wrapper which binds `agent_id` itself.

**Part B — add the new `@mcp.tool()` function**, placed after `get_related_memories` (after line
965, before the `get_events` tool at line 968) — this part remains a pure insertion, no existing
tool's line range is touched:

```python
@mcp.tool()
def retrieve_context(
    query: str,
    limit: int | None = None,
    budget_tokens: int | None = None,
) -> dict:
    """Assembles graph-aware, budget-bounded local context for a query in one call: primary search
    hits, one-hop predicate-allowlisted graph expansion, contradiction/conflict-set surfacing,
    lifecycle/supersession history, and deterministic token-budget packing -- everything
    search_memory + get_related_memories + get_lineage would otherwise require composing by hand.

    Returns one memories[] array (each item tagged inclusion: "primary"|"expansion"|
    "conflict_only", plus a retrieval_provenance explaining how it entered the result), the
    in-network edges directly connecting primary hits, a lineage map for every surfaced head with a
    known supersession chain (contradiction-flagged where relevant), conflict_sets for any
    unresolved contradiction touching this result (a lifecycle-resolved one surfaces via lineage
    instead, never here), and metadata.fan_out/metadata.budget truncation accounting.

    limit controls how many primary search hits seed expansion (default 5, matching search_memory's
    own default). budget_tokens caps the total token payload of memories[] (server default and hard
    ceiling apply if omitted). All other traversal behavior -- which relation predicates expand
    context, how far, and in which direction -- is fixed for this tool and not caller-configurable.
    """
    owner_id_ = _effective_owner()
    return _backend_or_raise().call(
        "retrieve_context",
        {
            "query": query,
            "limit": limit,
            "budget_tokens": budget_tokens,
            "owner_id": owner_id_,
        },
    )
```

Follows `get_related_memories`'s own plain-docstring-as-description style (lines 936-965) rather
than `search_memory`'s explicit `description=` kwarg style — the simpler, more recently-established
convention for a newly-added tool. `owner_id_` resolution mirrors both existing precedents exactly
(`_effective_owner()`, never a caller-supplied `owner_id` parameter — this tool must satisfy the
existing generic test `test_owner_id_is_absent_from_every_public_schema` automatically, by
construction, the same way every other tool in this file does).

## 4. `src/saltmdb/daemon/dispatch.py`

**Import** (line 20's `from saltmdb.domain.services import (...)` block): add `retrieve_context_service`
in alphabetical position, between `relation_service` and `telemetry_service`:

```python
from saltmdb.domain.services import (
    core_governance_service,
    event_service,
    librarian_service,
    memory_service,
    relation_service,
    retrieve_context_service,
    telemetry_service,
)
```

**New dispatch function**, placed after `_dispatch_get_events` (after line 420, before the
`DISPATCH_TABLE` literal at line 439) — mirrors `_dispatch_search_memory`'s style (explicit
per-field resolution, not a raw `**kw` forward) for `limit`/`budget_tokens` via the existing
`_optional_int_or_none` helper. **`query` is validated inline, NOT via `_required_str`** — see
Amendment 2 below for why `_required_str` is the wrong helper for this specific field:

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
    )
```

`limit`/`budget_tokens` use `_optional_int_or_none` (not `_optional_int` with a hardcoded default)
because the *default value itself* is `assemble_retrieve_context`'s own responsibility (`limit`
defaults inside step 3 of §2.1; `budget_tokens` defaults inside `pack_context_budget`, already
shipped) — mirrors A1-A4's own established pattern of dispatch.py resolving *which* kwargs were
supplied, never re-deciding a domain default dispatch.py doesn't own.

**`DISPATCH_TABLE`** (lines 439-454): add one entry, in the "Multi-branch" group (it takes a
required field it must validate, like `store_memory`/`get_memory`, not a bare one-liner forward),
placed after `"get_events"` and before `"review_core_memory"`:

```python
    "get_events": _dispatch_get_events,
    "retrieve_context": _dispatch_retrieve_context,
    "review_core_memory": _dispatch_review_core_memory,
```

**Not added to `MUTATING_TOOLS`** (lines 460-476): `retrieve_context` performs no writes — it calls
`search_memory`, `expand_context_candidates`, `assemble_conflict_sets`, `pack_context_budget`, and
`assemble_lineage`, all read-only — so it must run through `_dispatch_tool_inner`'s plain `return
fn(**kwargs)` branch, never `coordinator.submit(...)`, exactly like `search_memory`/
`get_related_memories`/`get_lineage` today.

## 5. `src/saltmdb/daemon/protocol.py`

**`READ_TOOLS`** (lines 36-49): add `"retrieve_context"`, placed after `"get_related_memories"` and
before `"get_events"` (matching `DISPATCH_TABLE`'s own ordering above, for a reader scanning both
files side by side):

```python
READ_TOOLS = frozenset(
    {
        "search_memory",
        "search_tags",
        "list_predicates",
        "get_memory",
        "inspect_memory",
        "get_lineage",
        "get_related_memories",
        "retrieve_context",
        "get_events",
        ...
    }
)
```

Not added to `WRITE_TOOLS` (lines 22-34) — same read-only rationale as §4.

## 6. `tests/test_retrieve_context_service.py` (new file)

Follow this suite's existing fixture conventions exactly, matching `tests/test_context_expansion_service.py`'s/
`tests/test_conflict_set_service.py`'s imports and setup pattern (`saltmdb.db.schema.init_db`,
`memory_service.store_memory` to create fixture entities, `relation_service.store_relation`/
`bulk_store_relations` to create edges, the shared `_axis_vector`-style orthogonal-embedding helper
where a test needs `store_memory` to succeed without tripping unrelated dedup/quality gates). Real
SQLite via `init_db`, no mocks for the graph/DB layer — mock only `memory_service.search_memory`
itself where a scenario needs to force a specific `search_hits` shape (e.g. the zero-hits scenario)
rather than relying on FTS/vector ranking to reliably reproduce it; every other scenario should call
the real `search_memory` against real fixture rows, matching A1-A4's own "real algebra over real
rows" precedent.

Required test scenarios (write these test-first, red before green, per this workspace's `tdd` skill
and Coding Standards rule 19 — this list is the acceptance bar's content, not exhaustive
implementation guidance OMP is free to skip beyond):

1. **Zero primary hits**: mock `search_memory` to return `[]`. Assert the full all-empty envelope —
   `memories == []`, `edges == []`, `lineage == {}`, `conflict_sets == []`,
   `metadata["fan_out"] == {"cap": 0, "eligible_count": 0, "truncated": False, "dropped_count": 0,
   "conflict_reserve": {"cap": CONTEXT_EXPANSION_CONTRADICTS_CAP, "eligible_count": 0, "truncated":
   False, "dropped_count": 0}}`, and `metadata["budget"]["used"] == 0` — produced by composing
   A1-A4's own already-tested empty-input shapes, not any special-cased branch in this slice's own
   code (confirm no such branch exists by reading the implementation, not just the test outcome).
2. **`search_memory`'s internal-error sentinel is treated as empty, not fatal** (corrected by
   Amendment 3 — the real shape, not the originally-specified one): mock `search_memory` to return
   `[{"error": "boom"}]` (a one-item list, matching `orchestrator.py`'s actual
   `return_diagnostics=False` exception path — NOT a bare dict); assert the call does not raise
   (no `KeyError` from the `h["id"]`/`h["score"]` comprehensions in step 4) and produces the same
   zero-hits envelope as scenario 1.
3. **Single dependency chain, no conflicts**: one primary hit with a `depends_on` edge to a
   non-primary node (mirrors A1 scenario 2). Assert `memories` has exactly 2 entries — the primary
   (`inclusion: "primary"`, `retrieval_provenance == [{"reason": "primary_search", "rank": 1}]`,
   `title`/`memory_type` matching the fixture) and the expansion candidate (`inclusion: "expansion"`,
   `retrieval_provenance` copied verbatim from A1's own shape) — and `edges == []` (no in-network
   edge exists between one primary hit and itself).
4. **In-network edge surfaces in top-level `edges`**: two primary hits connected by an
   included-predicate edge; assert that edge appears in the returned `edges` list with A1's exact
   4-key shape, unmodified.
5. **Lifecycle/supersession**: one primary hit with a real supersession chain of length 3. Assert
   `lineage` has exactly one head entry, `current`/`historical` shaped per A3's own contract, and
   that head does NOT also appear as a `conflict_sets` member (no conflict exists in this scenario).
6. **Unresolved contradiction, force-included conflict_only**: a primary hit `P` with a
   `contradicts` edge to a node `N` that is otherwise unreachable from any primary hit (not an A1
   expansion candidate). Assert `N` appears in `memories` with `inclusion: "conflict_only"`,
   `retrieval_provenance == [{"reason": "contradicts_force_include", "conflict_set_id": "cs-1"}]`,
   and `conflict_sets == [{"id": "cs-1", "members": [...], "edges": [...]}]` with both `P` and `N`
   as members.
7. **Lifecycle-resolved contradiction surfaces via lineage only, never `conflict_sets`**: a
   `contradicts` edge between two members of a supersession chain (mirrors A2's own resolved-
   component scenario). Assert `conflict_sets == []` and the relevant `lineage[head]` entry carries
   `was_flagged_contradiction`/`contradicted_with` on the correct specific entry.
8. **Equal-visibility reconciliation — the §1/§2.1-step-9 fix, exercised end to end**: reproduce the
   spec's own worked example verbatim — one primary hit `P` and one expansion candidate `X`
   connected by an unresolved `contradicts` edge, with `budget_tokens` set low enough that A4 would
   ordinarily drop `X`. Assert `X` still appears in `memories` (`inclusion: "expansion"`, its
   original A1 provenance intact, NOT relabeled `conflict_only`), `metadata["budget"]
   ["expansion_truncated"] is False`, `metadata["budget"]["expansion_dropped_count"] == 0`, and
   `metadata["budget"]["used"]` includes `X`'s own token count.
9. **Equal-visibility reconciliation does not mask an unrelated real drop**: same setup as scenario
   8, plus a second, ordinary (non-conflict) expansion candidate `Y` that also gets budget-dropped.
   Assert `Y` does NOT appear in `memories`, `metadata["budget"]["expansion_truncated"] is True`, and
   `expansion_dropped_count == 1` (counting only `Y`, not `X`) — proving the reconciliation is
   scoped to conflict members only, not a blanket suppression of all truncation signals.
10. **Fan-out cap truncation passes through**: enough out-of-network candidates to exceed
    `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS * len(primary_hits)`; assert
    `metadata["fan_out"]["truncated"] is True` with the correct `dropped_count`, and that the
    survivors in `memories` are exactly A1's own top-ranked ones.
11. **`limit` is honored**: two primary hits available in the fixture corpus for a query; call with
    `limit=1`; assert only 1 primary hit seeds expansion (verify via `search_memory`'s own returned
    count feeding into `original_rank`/`primary_hits`, not by asserting on `memories` length alone,
    since expansion candidates could coincidentally also number 1).
12. **`budget_tokens` clamps to the configured ceiling**: call with `budget_tokens` above
    `CONTEXT_BUDGET_MAX_TOKENS`; assert `metadata["budget"]["limit"] ==
    CONTEXT_BUDGET_MAX_TOKENS`, not the caller-supplied value (delegates to A4's own already-tested
    clamp — this test only confirms A5 passes `budget_tokens` through unmodified and reports A4's
    resulting `limit` verbatim).
13. **`memory_type` fallback for a stale conflict_only reference**: construct a `conflict_only`
    member whose id has no matching `entities` row (delete it after creating the relation, or
    construct the edge against a syntactically-valid but nonexistent id, matching A1 scenario 13's
    and A2's own precedent for this). Assert that row's `memory_type == "unknown"` rather than a
    `KeyError`.
14. **One connection for the whole call**: patch `get_connection` (or count calls via a connection-
    counting fixture) and assert it is called at most once across the entire `assemble_retrieve_context`
    invocation, including the internal `search_memory` call — proving §2.1 step 1's "thread one
    connection through everything" requirement actually holds for A1-A4's own four calls, not just
    reads as intended in prose. (Does not assert anything about connections opened *inside*
    `search_memory`'s own semantic/vector/chunk/topic-scoring sub-searches — see Amendment 4 for why
    those are out of this slice's jurisdiction to collapse.)
15. **Added by Amendment 4 — `search_memory` receives the connection's own database path, not a
    divergent default**: call `assemble_retrieve_context(..., db_connection=self.conn)` (the
    existing fixture pattern this suite already uses throughout, `db_path` omitted); patch
    `memory_service.search_memory` and assert the `db_path` kwarg it receives equals `self.db_path`
    (the fixture's own file, via `PRAGMA database_list` on `self.conn`) — **not** whatever
    `get_db_path()` would return. This is the regression test for the bug Amendment 4 fixes; without
    it, the fix could silently regress back to forwarding the raw, possibly-`None` `db_path`
    parameter a second time.

## 7. `tests/test_retrieve_context_wiring.py` (new file)

Mirrors `tests/test_phase4_mcp_surface.py`'s `test_lifecycle_registration_and_protocol_classification`
pattern (that file's lines 77-84) for this tool's own 3-file registration, plus one live dispatch
smoke test matching `tests/test_dispatch_types.py`'s style:

1. **Registration + classification**: `"retrieve_context"` is present in `dispatch.DISPATCH_TABLE`
   and `protocol.READ_TOOLS`; absent from `dispatch.MUTATING_TOOLS` and `protocol.WRITE_TOOLS`.
2. **Dispatch forwards defaults correctly**: patch
   `saltmdb.daemon.dispatch.retrieve_context_service.assemble_retrieve_context`, call
   `dispatch._dispatch_retrieve_context(query="q", owner_id="owner")` with `limit`/`budget_tokens`
   omitted, and assert the patched call received `limit=None, budget_tokens=None` (not `0` or a
   hardcoded default — confirming dispatch.py does not shadow `assemble_retrieve_context`'s own
   default resolution, per §4's stated rationale).
3. **Missing/non-string `query` raises, but empty-string `query` does NOT**: `dispatch.
   _dispatch_retrieve_context(owner_id="owner")` (no `query` kwarg) and `dispatch.
   _dispatch_retrieve_context(query=123, owner_id="owner")` (wrong type) both raise `ValueError`
   via the inline check in §4 (Amendment 2) — but `dispatch._dispatch_retrieve_context(query="",
   owner_id="owner")` does NOT raise; patch `assemble_retrieve_context` and assert it receives
   `query=""` unchanged. This is a deliberate divergence from `_required_str`'s own non-empty
   contract, not an oversight — see Amendment 2 for why.
4. **Tool schema has no `owner_id`**: `tools.retrieve_context` is covered automatically by the
   existing generic `test_owner_id_is_absent_from_every_public_schema`
   (`tests/test_mcp_tools.py:63`) once registered — this scenario just asserts that existing test
   still passes with the new tool present (no new test code needed here beyond running it; note
   this explicitly so the acceptance run in §9 is understood to already cover it).
5. **End-to-end through the real dispatch table**: using the in-process `DirectDispatchBackend` (or
   this suite's existing equivalent harness — check `tests/test_daemon_server.py`/
   `tests/test_corpus_snapshot_mcp.py` for the established in-process pattern before writing a new
   one), call `tools.retrieve_context(query="...")` against a small real fixture corpus and assert
   the call succeeds end-to-end and returns the `{query, memories, edges, lineage, conflict_sets,
   metadata}` top-level key set exactly.

**Added by Amendment 3** — `DirectDispatchBackend` (scenario 5 above) cannot exercise
`_OWNER_INJECTED_TOOLS` at all (it never strips or re-injects `owner_id`; see Amendment 3), so the
one behavior Part A of §3 exists to fix needs its own test against the actual backend that has the
bug surface:

6. **`RpcBackend` re-injects `owner_id` for `retrieve_context`, and `DirectDispatchBackend`'s own
   pass-through does not mask this**: added to `tests/test_session_identity.py`'s
   `TestRpcBackendIdentityWiring` (not the new `test_retrieve_context_wiring.py` file — this class
   already owns every other tool's version of this exact test, and `retrieve_context` belongs
   alongside them, not in a separate file), mirroring `test_backend_does_not_bind_owner_from_
   dispatch_kwargs`'s existing pattern exactly: configure `SESSION_IDENTITY` to a known owner,
   patch `saltmdb.daemon.client.call`, call `RpcBackend().call("retrieve_context", {"query": "q"})`,
   and assert the forwarded kwargs' `owner_id` equals the configured owner — proving §3 Part A's
   frozenset addition actually takes effect, not just that it reads correctly in the source.

## 8. Out of scope

- Recalibrating any Milestone-B-deferred numeric constant (`CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`,
  `CONTEXT_EXPANSION_CONTRADICTS_CAP`, `CONTEXT_BUDGET_DEFAULT_TOKENS`, `CONTEXT_BUDGET_MAX_TOKENS`)
  — Milestone B.
- Any Milestone B benchmark/eval machinery (golden query sets, contradiction-recall scoring,
  lifecycle-correctness scoring, multi-hop query generation) — a separate, not-yet-specced effort
  per the wayfinder map's own live-usage-first posture.
- An `entity_ids` seed parameter, or any caller override of the predicate allowlist, fan-out cap,
  traversal depth, traversal direction, or `point_in_time` — all explicitly rejected/foreclosed in
  §1 above for Milestone A.
- Any change to `expand_context_candidates`, `assemble_conflict_sets`,
  `classify_contradicts_components`, `assemble_lineage`, or `pack_context_budget`'s own signatures,
  algorithms, or output shapes — this slice calls all five exactly as they exist today on
  `context-aware-search` @ `a2bf8fd`. The equal-visibility reconciliation in §2.1 step 9 is
  implemented entirely as post-processing over their *outputs* in the new orchestrating function;
  it requires no change to any of them, and this spec makes none.
- A caller-supplied `mode` (strict/broad/history), `context_id`, `agent_session_id`, `tags_filter`,
  `memory_type_filter`, `is_core`, or `cursor` parameter on `retrieve_context` itself — all fixed
  per §2.1 step 3's reasoning.
- Pagination of `retrieve_context`'s own result (no `cursor` on the tool; `limit` bounds only how
  many primary hits seed expansion, not a paged walk through a larger result set).
- Adding a `resolution` field to the final `conflict_sets` output, or a synthetic `direction` field
  to the top-level `edges` output — both explicitly considered and rejected in §2.1 steps 13-14.
- Any change to `mcp/server.py`, `daemon/server.py`, `daemon/client.py`, or
  `db_write_coordinator.py` — none of this slice's wiring touches connection lifecycle, RPC framing,
  or the write-coordinator boundary beyond the two frozenset/dict entries named in §4-§5.

## 9. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_retrieve_context_service.py tests/test_retrieve_context_wiring.py -v
```
must exit 0, and every scenario in §6/§7 must correspond to at least one passing test (a reviewer
checks this by name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite —1607 tests passed on `context-aware-search`
@ `a2bf8fd` before this slice's changes, confirmed 2026-09-12 immediately after the A4 merge).

```bash
uv run ruff check src/saltmdb/domain/services/retrieve_context_service.py src/saltmdb/mcp/tools.py \
  src/saltmdb/daemon/dispatch.py src/saltmdb/daemon/protocol.py \
  tests/test_retrieve_context_service.py tests/test_retrieve_context_wiring.py && \
uv run ruff format --check src/saltmdb/domain/services/retrieve_context_service.py src/saltmdb/mcp/tools.py \
  src/saltmdb/daemon/dispatch.py src/saltmdb/daemon/protocol.py \
  tests/test_retrieve_context_service.py tests/test_retrieve_context_wiring.py && \
uv run mypy src/saltmdb/domain/services/retrieve_context_service.py src/saltmdb/mcp/tools.py \
  src/saltmdb/daemon/protocol.py
```
must exit 0.

```bash
uv run mypy src/saltmdb/daemon/dispatch.py
```
**Corrected by Amendment 4** — must produce *exactly* these three pre-existing errors and no others
(confirmed unrelated to A5, present on `context-aware-search` @ `a2bf8fd` before this slice's own
changes, at what were then lines 354/413 and are now 355/414 purely because A5's own import
addition shifts every later line down by one):
```
src/saltmdb/daemon/dispatch.py:355: error: Argument "metadata" to "update_memory_metadata" has incompatible type "Any | None"; expected "dict[Any, Any]"  [arg-type]
src/saltmdb/daemon/dispatch.py:414: error: Argument 4 to "analyze_dependencies" has incompatible type "**dict[str, object]"; expected "str | None"  [arg-type]
src/saltmdb/daemon/dispatch.py:414: error: Argument 4 to "analyze_dependencies" has incompatible type "**dict[str, object]"; expected "bool"  [arg-type]
Found 3 errors in 1 file (checked 1 source file)
```
A reviewer confirms this by running the same command against `context-aware-search` @ `a2bf8fd`
(pre-A5) and diffing: the post-A5 output must be identical modulo the one-line shift. Any error at
a different line, a different message, or a fourth error anywhere in the file fails this criterion
— this bar accepts zero new mypy debt, it only declines to require this slice to fix debt that
predates it and sits entirely outside §0's locked edit range for this file (the two pre-existing
errors are in `_dispatch_update_memory_metadata`/`_dispatch_get_related_memories`; A5 may only edit
this file's import block and the code from the new `_dispatch_retrieve_context` function onward).
These three commands together match this repo's documented lint/type gate (`CONTRIBUTING.md` §4),
applied per-file the way every other slice's own `mypy` acceptance criterion in this Milestone
already has been.

```bash
rg -n '"retrieve_context"' src/saltmdb/daemon/dispatch.py src/saltmdb/daemon/protocol.py
```
must show exactly one match in each file (the `DISPATCH_TABLE`/`READ_TOOLS` entries) — confirms §4/
§5's wiring landed and did not also get added to `MUTATING_TOOLS`/`WRITE_TOOLS` (a reviewer greps
those two frozensets by name to confirm absence, since a string match alone can't distinguish which
frozenset it landed in).

## 0. Status

**LOCKED** (2026-09-12).

**Scope**: may create `src/saltmdb/domain/services/retrieve_context_service.py`,
`tests/test_retrieve_context_service.py`, and `tests/test_retrieve_context_wiring.py`. May edit
`src/saltmdb/mcp/tools.py` (§3 Parts A and B — both the `_OWNER_INJECTED_TOOLS` frozenset addition
and the new tool function, per Amendment 3), `src/saltmdb/daemon/dispatch.py` (§4's import,
function, and `DISPATCH_TABLE` insertions only), and `src/saltmdb/daemon/protocol.py` (§5's
`READ_TOOLS` insertion only). Per Amendments 1 and 3, may also edit exactly these four existing
test files, at exactly the locations each amendment enumerates and nowhere else in them:
`tests/test_mcp_tools.py` (Amendment 1's count-guard fix), `tests/test_phase3_mcp_surface.py`
(Amendment 1), `tests/test_phase4_mcp_surface.py` (Amendment 1), and `tests/test_session_identity.py`
(Amendment 3's new `RpcBackend` owner-injection test method, §7 scenario 6). Does not touch:
`src/saltmdb/domain/services/context_expansion_service.py`, `conflict_set_service.py`,
`lineage_assembly_service.py`, `context_budget_service.py`, `src/saltmdb/config.py`,
`src/saltmdb/mcp/server.py`, `src/saltmdb/daemon/server.py`, `src/saltmdb/daemon/client.py`,
`src/saltmdb/daemon/db_write_coordinator.py`, any existing test file other than the four
enumerated above (and, within those four, any location other than what their amendment names), or
any file under `specs/` other than this one.

**Pre-lock gate run against the current tree (2026-09-12)**:
1. Every other section was drafted before this one (standard for this workspace's spec-writing
   skill) — Why, §2-§7's mechanical sections, §8 Out of scope, §9 Acceptance all precede this scope
   list in drafting order.
2. §9's `rg` acceptance command was run against the current tree before locking: zero matches today
   (confirms `"retrieve_context"` does not yet appear in either file, so the command's post-
   implementation "exactly one match" bar is meaningful, not trivially already-true).
3. §9's ruff/mypy file lists were cross-checked against §0's own scope list above — every file named
   in one appears in the other; no mismatch.
4. No lockfile, manifest, or codegen output is produced by this slice's changes — no `uv sync`
   re-run is required (no new dependency is added; `fastembed`/`sqlite3`/stdlib only, all already
   present via A1-A4).
5. No existing string or constant's *content* is changed by this slice (only new code is added) —
   rule 5's grep-the-old-text step is not applicable; confirmed by re-reading §2-§5 above for any
   edit to an existing line's value, finding none.
6. Every file on the "does not touch" list above was opened and read during this spec's drafting
   (§2.1's algorithm cites exact line numbers/shapes from `context_expansion_service.py`,
   `conflict_set_service.py`, `lineage_assembly_service.py`, and `context_budget_service.py`; §3-§5
   cite exact line numbers from `tools.py`, `dispatch.py`, and `protocol.py`) — none already
   contains a `retrieve_context` reference this slice would collide with (confirmed via the same
   `rg` sweep as step 2, run across all of `src/` and `tests/`, not just the two protocol/dispatch
   files: zero matches outside `prototype_retrieve_context.html` and the A1-A4 spec docs' own prose
   mentions of the future tool name).
7. Every shape stated in more than one place was diffed word-for-word before locking: A1's
   `expansion_candidates`/`fan_out`/`contradicts_edges` shapes (§2.1 steps 5, 9, 12, 13) match
   `context_expansion_service.py`'s actual `TypedDict`s and Output contract exactly, not the
   `prototype_retrieve_context.html` mockup's differently-named fields (`mutualNeighborCount` vs.
   `mutual_neighbor_count`, a synthetic `edges`+`direction` shape the real A1 output never has,
   etc.) — the prototype is cited only for the two provenance/nesting shapes (§2.1 step 12's
   `contradicts_force_include` reason, §2.1 step 15's `strategy` field name) that no G-ticket ever
   locked in text, and even there its exact key names were re-verified against the live file
   (`prototype_retrieve_context.html:358,384`), not recalled from memory. A2's `conflict_sets`/
   `contradicts_cap` shape (§2.1 steps 6, 9, 11, 14, 15) matches `conflict_set_service.py`'s actual
   Output contract, not the prototype's simplified `{members: [ids], edges}`/`resolution` shape.
   A4's `packed_entity_ids`/`dropped_entity_ids`/`token_counts`/`budget` shape (§2.1 steps 7, 9, 15)
   matches the (unmerged-until-this-session) `context_budget_service.py`'s actual Output contract,
   read directly from `SPEC-CONTEXT-RETRIEVAL-A4-CONTEXT-BUDGET-PACKING.md` §3.1 and cross-checked
   against the merged file at `context-aware-search` @ `a2bf8fd`.
8. §1's worked example was traced through §2.1 steps 6-15 concretely (the `P`/`X` unresolved-
   contradiction-vs-budget-drop instance) and checked against every other locked rule it touches:
   A2's inclusion-precedence rule (confirms `X` is tagged `"expansion"`, never `"conflict_only"`,
   so A4's ordinary budget competition — not its `conflict_only` bypass — is what would have
   dropped it without this slice's fix), A4's "no `break`, every candidate independently evaluated"
   rule (confirms `X` really does end up in `dropped_entity_ids["expansion"]` rather than never
   being evaluated at all), and G7's equal-visibility invariant (confirms the *reason* this needed
   fixing, not just that it was observed) — this is what produced §1's cross-slice-gap analysis and
   §2.1 step 9's fix, not something noticed only after drafting.
9. The one third-party-runtime dependency this spec's feasibility rests on — `memory_service.
   search_memory`'s actual `db_connection` parameter accepting a caller-supplied connection so §2.1
   step 1's "one shared connection" requirement is achievable for the search call too, not just
   the four already-`db_connection`-aware A1-A4 calls — was confirmed by reading
   `memory_service/orchestrator.py`'s actual `search_memory` signature (line 61:
   `db_connection=None`) directly, not assumed from the MCP tool wrapper's own narrower public
   signature (which has no such parameter at all — the wrapper never needed one, since it always
   went through the daemon's own coordinator-owned connection until this slice's direct domain-
   layer call).

## Amendment 1 — Three hardcoded MCP-registry-count assertions block §3's own registration requirement

**Adjudicated 2026-09-13, after OMP reported `BLOCKED — SPEC ADJUDICATION REQUIRED`.**

**The contradiction, verified against the actual worktree tree (not just OMP's report) before
adjudicating**: §3 requires registering `retrieve_context` as a genuinely new `@mcp.tool()`, which
necessarily takes the live MCP registry from 18 tools to 19. §0's original scope list said this
slice "does not touch: ... any existing test file." Grepping the actual worktree for every place
that count is hardcoded (not just the one OMP happened to name) found **three** exact-value
assertions, all currently `18`, not one:

1. `tests/test_mcp_tools.py::test_mcp_tool_count_regression_guard` (lines 1102-1112) — the
   project's own designated "authoritative count guard," per the cross-reference comment in (2)
   below.
2. `tests/test_phase3_mcp_surface.py::test_tool_count_and_registration` (line 35), with a
   preceding comment (lines 29-33) that tracks the tool count's entire history
   (`19 -> 18 -> 16 -> 17 -> 18`) phase by phase, and explicitly defers to (1) as authoritative.
3. `tests/test_phase4_mcp_surface.py::test_lifecycle_tools_are_typed_and_old_name_is_not_public`
   (line 37), with its own copy of the same history comment (lines 31-35).

OMP's own report named only (3); (1) and (2) were found during this adjudication by grepping for
every `len(tools.mcp._tool_manager._tools)`/hardcoded-count pattern in `tests/`, not assumed absent
because unreported — the fix below covers all three, not just the one OMP hit first. No test
hardcodes a count on `dispatch.DISPATCH_TABLE`, `protocol.READ_TOOLS`, or `protocol.WRITE_TOOLS`
directly (confirmed via the same grep sweep) — only the MCP-registry-size assertions above need
touching; every other existing `assertIn`/`assertNotIn` membership check in these same three files
is agnostic to the registry's total size and needs no change.

**Why this wasn't caught before locking**: the original pre-lock gate's step 6 ("for anything on
the 'does not touch' list, actually open it and confirm it doesn't already contain something
Acceptance's own criteria will demand gone") was run against the four files this slice *edits*
(`tools.py`, `dispatch.py`, `protocol.py`) and the services this slice *calls*, but not against the
broader "any existing test file" blanket clause in §0 itself — a blanket negative scope entry was
never itself grepped for a concrete, load-bearing conflict the way every *positive* scope entry was.
That is the gap this amendment closes, and the precedent worth carrying into future specs in this
project: a blanket "does not touch: X" clause needs the same concrete verification a positive scope
item gets, not just an assumption that the boundary is safe because nothing in this slice's own new
code lives inside X.

**Decision**: §0's scope is widened to explicitly permit editing exactly these three existing test
files, at exactly these locations — nothing else in any of the three files may change:

- `tests/test_mcp_tools.py`: in `test_mcp_tool_count_regression_guard`, change the expected value
  `18` to `19` and update the assertion's own `f"..."` message to name the new cause:
  ```python
  def test_mcp_tool_count_regression_guard(self):
      registered_count = len(tools.mcp._tool_manager._tools)
      self.assertEqual(
          registered_count,
          19,
          f"MCP server tool count must be exactly 19 after retrieve_context was added "
          f"(Milestone A slice A5) on top of the metadata update and inspect_memory tools "
          f"(ephemeral_memory and "
          f"export_corpus_snapshot removed, following Phase 6's dismiss_event removal), "
          f"got {registered_count}",
      )
  ```
- `tests/test_phase3_mcp_surface.py`: in `test_tool_count_and_registration`, extend the existing
  history comment with one more line and change `18` to `19`:
  ```python
  def test_tool_count_and_registration(self):
      # Phase 6 removed dismiss_event (19 -> 18); Phase 7 removed ephemeral_memory and
      # export_corpus_snapshot (18 -> 16, the plan's §2 target). update_memory_metadata was
      # added afterward (16 -> 17, API-ergonomics Gap 1), then inspect_memory (17 -> 18,
      # API-ergonomics Gap 2). Milestone A slice A5 added retrieve_context (18 -> 19). See
      # test_mcp_tools.py's test_mcp_tool_count_regression_guard for the authoritative count
      # guard.
      self.assertEqual(len(tools.mcp._tool_manager._tools), 19)
  ```
- `tests/test_phase4_mcp_surface.py`: in `test_lifecycle_tools_are_typed_and_old_name_is_not_public`,
  extend its own copy of the history comment and change `18` to `19`:
  ```python
  def test_lifecycle_tools_are_typed_and_old_name_is_not_public(self):
      # Phase 4 adds two lifecycle intents and renames consolidation one-for-one.
      # dismiss_event was removed in Phase 6; ephemeral_memory and export_corpus_snapshot were
      # removed in Phase 7 (18 -> 16, the plan's §2 target of exactly 16 MCP tools).
      # update_memory_metadata was added afterward (16 -> 17, API-ergonomics Gap 1), then
      # inspect_memory (17 -> 18, API-ergonomics Gap 2). Milestone A slice A5 added
      # retrieve_context (18 -> 19).
      self.assertEqual(len(tools.mcp._tool_manager._tools), 19)
  ```

**§9's acceptance bar is unchanged** — this amendment widens scope to make the existing
`PYTHONPATH=src uv run pytest tests/ -q` acceptance criterion achievable at all; it does not relax,
skip, or narrow that criterion, and the full suite (now including these three updated assertions)
must still exit 0. No other file, assertion, or behavior in any of the three test files may change
under this amendment — a broader cleanup of either file is out of scope, matching §0's existing
"does not touch... any existing test file" intent everywhere this amendment doesn't explicitly
carve out.

**Amendment gate re-run**: re-checked this amendment itself against the same three-file grep sweep
above (gate steps 2/3) to confirm no fourth hardcoded-count site was missed, and against gate
step 7 (the comment blocks and the assertion values are updated together, in the same edit, so
they cannot drift out of sync with each other). No other section of this spec is affected; §0's
"does not touch" list is corrected by this amendment to read as: does not touch any existing test
file **except the three enumerated locations above**.

**Note on OMP's own blocker log**: OMP's report named a SALTMDB memory id
(`a1a45f31-da88-462a-8830-a257858a7518`) as where it logged this blocker. That id does not resolve
via `get_memory` or `search_memory` in this database as of this adjudication — either a different
owner/visibility scope than this session's, or the id did not persist. Not investigated further
here since OMP's own report (file:line, exact contradiction) was independently verified directly
against the worktree and found accurate, and this amendment additionally covers two sites OMP's
own report did not name; flagging only so a future session doesn't waste time assuming that id is
retrievable.

## Amendment 2 — `query`'s empty-string contract: §2.1 vs. the originally-specified `_required_str` dispatch call

**Adjudicated 2026-09-13, after OMP's second `BLOCKED — SPEC ADJUDICATION REQUIRED` report.**

**The contradiction, verified against the actual worktree tree before adjudicating**: §2.1's input
contract states `query` is passed to `search_memory` "unchanged... including when falsy (`\"\"\"`)"
— a deliberate choice, reusing `search_memory`'s own existing browse-mode behavior for a falsy
query rather than this slice inventing new validation (§2.1's own stated rationale: "this slice
adds no query validation or special-casing of its own"). But §4's originally-specified
`_dispatch_retrieve_context` body called `query=_required_str(kw, "query")`, and
`dispatch.py`'s actual `_required_str` (lines 97-101) is:
```python
def _required_str(kw: dict[str, Any], key: str) -> str:
    value = kw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value
```
`not value` rejects `""` — so the originally-specified §4 code, if implemented exactly as written,
would raise on every empty-query call through the daemon/MCP route, directly contradicting §2.1's
own "including when falsy" guarantee for that same route. A direct call to
`assemble_retrieve_context(query="", ...)` bypassing dispatch entirely could still honor §2.1, but
the tool's actual public surface (`tools.retrieve_context` → `_dispatch_retrieve_context`) could
not — OMP correctly identified this as two incompatible public-API behaviors, not a rewording
question, and correctly refused to silently pick one.

**Why this wasn't caught before locking**: gate step 7 ("for any data shape, contract, or value
stated in more than one place... diff the two statements against each other, word for word") was
run for every *data shape* repeated across sections (A1-A4's output contracts, the envelope shape),
but `query`'s *validation behavior* was stated once in prose (§2.1) and once as literal, executable
code (§4's `_required_str` call) — two different *kinds* of statement about the same fact, which
step 7's own "diffed... word for word" framing was read too narrowly to catch (a prose guarantee
and a concrete function call are harder to visually diff against each other than two JSON blocks
are). **Standing lesson, alongside Amendment 1's**: when a spec states a behavioral guarantee in
prose in one section and then locks in the literal code that must (or must not) produce it in
another, that pairing needs the same explicit side-by-side check §7 already requires for two
prose/JSON restatements of one shape — reading the code block's actual runtime behavior, not just
its surface plausibility as "the existing helper for a required field."

**Decision: Option 1 — empty query is supported end-to-end**, matching §2.1's original intent
(unchanged) rather than narrowing it. Rationale: §2.1's reuse-`search_memory`'s-own-behavior
rationale was the deliberate design choice this slice was built around from the start (Coding
Standards rule 3/4 — reuse over reinvention; don't add validation `search_memory` itself doesn't
need); `_required_str`'s non-empty semantics fits fields like `entity_id`/`title`, where an empty
value is never meaningful, but `query` is different by construction — `search_memory` already
treats a falsy `query_keywords` as a legitimate, meaningful input (its existing tags/filters-only
browse mode), so rejecting `""` here would be inventing a restriction `search_memory` itself
doesn't have, solely because `_required_str` happened to be the nearest-looking existing helper.
Narrowing §2.1 instead (Option 2) would mean `retrieve_context`'s query-only tool cannot reach a
`search_memory` behavior its own direct-service layer explicitly promises support for — a real,
user-visible capability gap for zero benefit, since nothing about the daemon/MCP boundary requires
rejecting an empty string.

**Fix, already applied above in §4 and §7 (this amendment records why, not a second copy of the
diff)**: `_dispatch_retrieve_context` validates `query` inline —
```python
query = kw.get("query")
if not isinstance(query, str):
    raise ValueError("query is required")
```
— instead of calling `_required_str(kw, "query")`. This keeps "the argument must be present and
must be a string" (still a real, enforced requirement — `None`, a missing kwarg, or a non-string
value all still raise) while dropping only the "and must be non-empty" clause `_required_str` adds
on top, which is the one clause that contradicted §2.1. This is a **local, inline** check inside
`_dispatch_retrieve_context` only — not a new shared helper function — per Coding Standards rule 14
("don't create single-use helper abstractions for something used exactly once inline"): no other
field in this dispatch call, and no other tool's dispatch function anywhere in `dispatch.py`, needs
this exact "string-typed but empty-allowed" contract, so a new named helper (e.g.
`_required_str_allow_empty`) would be a single-caller abstraction this project's own standards
already rule out. **`_required_str` itself is untouched** — every other caller in `dispatch.py`
(all of which genuinely do need non-empty semantics, e.g. `entity_id`, `title`) keeps its existing
behavior exactly as shipped; this amendment changes zero lines outside `_dispatch_retrieve_context`
and its own two test scenarios.

**§9's acceptance bar is unchanged** by this amendment either — no new file, no relaxed test
command; §7 scenario 3 (already corrected above) is the concrete proof this contract holds, and
must pass under the same `pytest tests/ -q` run as everything else.

**Amendment gate re-run**: re-checked every other `kw.get(...)`/`_required_*`/`_optional_*` call
in §4's dispatch function against this same prose-vs-code mismatch pattern (`owner_id`, `limit`,
`budget_tokens`) — none of the other three has a prose guarantee anywhere in §2.1-§3 that its
corresponding dispatch-layer helper would contradict (`owner_id` is never validated as
required/non-empty by design; `limit`/`budget_tokens` are genuinely optional end-to-end with no
falsy-value special case asserted anywhere), so no fourth site needs the same fix. Also
re-diffed §2.1's own restated "query is passed to search_memory as query_keywords unchanged... even
when falsy" line against the corrected §4 code above, word for word, confirming they now agree.

**Note on OMP's own blocker log**: OMP's second report named a SALTMDB event id
(`321faf36-409d-4366-b7b8-b49392ba4fc9`). A `get_events` sweep of this project's own
`wayfinder:saltmdb:graph-aware-context-retrieval` context (filtered `event_type="issue"`,
newest-first) did not surface it — only an unrelated 2026-09-11 event predating this slice entirely
came back. Same loose-thread pattern as Amendment 1's unresolvable memory id; not chased further
for the same reason (the report's own file:line/code evidence was independently verified as
accurate against the real worktree regardless of whether its own log entry is retrievable from
here).

## Amendment 3 — Owner-propagation gap and a wrong search_memory error-shape claim (two contradictions, one round)

**Adjudicated 2026-09-13, after OMP's third `BLOCKED — SPEC ADJUDICATION REQUIRED` report.** Both
claims were verified directly against the real worktree source before adjudicating, in full, before
any fix was written — not patched from OMP's report text alone.

### Contradiction 1 — `retrieve_context` never reaches `_OWNER_INJECTED_TOOLS`, so production silently loses `owner_id`

§3 (as originally written) had `tools.retrieve_context` resolve `owner_id_ = _effective_owner()`
and add it to the kwargs dict passed to `_backend_or_raise().call(...)`, and called this "pure
insertion, no existing tool's line range touched." That framing was wrong. `tools.py`'s
`RpcBackend.call()` — confirmed by reading it directly, not summarized —
```python
if tool_name in _OWNER_INJECTED_TOOLS:
    kwargs = {**kwargs, "owner_id": _effective_owner()}
else:
    kwargs = {key: value for key, value in kwargs.items() if key != "owner_id"}
```
strips `owner_id` from any tool's kwargs entirely unless that tool's name is a member of
`_OWNER_INJECTED_TOOLS` (`tools.py:94-116`, confirmed to hold exactly 12 entries, `retrieve_context`
absent from all of them). `retrieve_context` was never added to that set anywhere in the original
spec — meaning the one backend actually used in production (`RpcBackend`; `DirectDispatchBackend`
is for tests and the in-process daemon RPC handler only) would silently dispatch every real
`retrieve_context` call with `owner_id=None`, changing which memories the tool can see, with no
error raised anywhere. `DirectDispatchBackend`'s own `call()` is a bare `dispatch.DISPATCH_TABLE[
tool_name](**kwargs)` forward — it never strips or re-injects this key at all — so every test this
spec originally required (§7 scenario 5, and the whole of §6) would have passed cleanly while the
real production path was broken, exactly OMP's own diagnosis.

**Fix**: §3 above is corrected to two parts — Part A adds `"retrieve_context"` to
`_OWNER_INJECTED_TOOLS`; Part B is the original tool-function insertion, otherwise unchanged. §7
gained a new scenario 6, in `tests/test_session_identity.py` (not the new wiring test file — that
existing file already owns this exact test shape for every other owner-injected tool, via its
`TestRpcBackendIdentityWiring` class, and `retrieve_context` belongs there, not in a parallel
one-off), directly exercising `RpcBackend.call()` — the one backend whose behavior actually
depends on this fix — so this specific regression cannot recur silently a second time.

**Why this wasn't caught before locking**: §3's original text asserted "pure insertion" as a
factual claim about the blast radius of adding a new tool, but that claim was never checked against
`RpcBackend.call()`'s actual body — it was inferred from "a new `@mcp.tool()` function doesn't
require editing any other function," which is true for the function itself but false for the
tool's *registration surface*, which (as this project's own established pattern shows for every
comparable owner-scoped tool) spans more than the two files+one frozenset already accounted for in
§4/§5. Grepped every string-literal occurrence of `"search_memory"` (a known-working, fully
owner-scoped precedent) across all of `src/` as the ground truth for what a comparable tool
actually needs, rather than reasoning from the `DISPATCH_TABLE`/`READ_TOOLS` pair alone:
`memory_service/__init__.py` (a package-level `__all__` re-export, irrelevant here —
`retrieve_context_service` is not part of the `memory_service` package), `dispatch.py`
(`DISPATCH_TABLE`), `protocol.py` (`READ_TOOLS`), and **two** separate hits in `tools.py`
(`_OWNER_INJECTED_TOOLS`, and the tool's own `.call(...)` site) — confirming `_OWNER_INJECTED_TOOLS`
is a real, load-bearing fourth registration point this project's own precedent already establishes
for every owner-scoped tool, not a one-off. Also grepped for every other tool-name-keyed
frozenset/dict across `tools.py`, `dispatch.py`, `protocol.py`, `client.py`, `server.py`, and
`db_write_coordinator.py` to confirm no *sixth* registry exists beyond the five now accounted for
(`DISPATCH_TABLE`, `MUTATING_TOOLS`, `READ_TOOLS`, `WRITE_TOOLS`, `_OWNER_INJECTED_TOOLS`) — none
found. **Standing lesson**: for a new MCP tool, enumerate every *string-literal* occurrence of an
existing, comparable tool's name across the whole `src/` tree first, and require the new tool to
appear at the same set of sites (explicable exceptions named individually), rather than assuming
the registration surface is fully described by the sections already drafted for it.

### Contradiction 2 — the specified `search_memory` internal-error shape was never the real one

§2.1 step 3 (as originally written) said `search_memory`'s internal-exception path (with
`return_diagnostics=False`, as this call uses) returns a bare dict,
`{"results": [], "diagnostics": ..., "error": ...}`, and specified guarding for it via `isinstance(
search_result, dict)`. That claim was wrong — re-reading `orchestrator.py`'s actual `except
Exception` branch line by line:
```python
except Exception as e:
    logger.error("Error searching memory: %s", e)
    diagnostics["error"] = str(e)
    validation._set_search_diagnostics(diagnostics)
    if return_diagnostics:
        return {"results": [], "diagnostics": diagnostics, "error": str(e)}
    return [{"error": str(e)}]
```
the dict-shaped return is the `return_diagnostics=True` branch — unreachable here, since this
slice's own §2.1 step 3 call always passes `return_diagnostics=False`. The branch this call
actually takes returns `[{"error": str(e)}]` — **a list**, containing one sentinel dict with only
an `"error"` key. The originally-specified `isinstance(search_result, dict)` guard could therefore
never fire for the one real failure mode this call can hit, and step 4's `h["id"]`/`h["score"]`
dict-comprehensions would raise `KeyError` on `{"error": str(e)}`'s missing `"id"` key — a crash on
a real upstream failure, the opposite of the "log and treat as empty" resilience §2.1 claimed to
provide.

**Fix**: §2.1 step 3 above is corrected to check `"id" not in search_hits[0]` (the property that
actually, verifiably distinguishes the error sentinel from every real result item — confirmed
against the success path's own literal `item = {"id": eid, "title": etitle, ...}` construction a
few lines above the exception handler) rather than a type check on the whole return value, since
`return_diagnostics=False` makes the return value always a list; the dict-shaped branch is dead
code for this call and the spec no longer claims otherwise. §6 scenario 2 is corrected to mock the
real `[{"error": "boom"}]` shape, not the fictitious dict one.

**Why this wasn't caught before locking**: this was a plain misreading of `orchestrator.py`'s
source during the original grounding pass — the two branches of the same `if return_diagnostics:
... / return ...` statement were read together and only the first (dict) branch's shape was
carried into the spec, without re-confirming which branch actually applies given this call's own
fixed `return_diagnostics=False` argument three lines above at the call site. **Standing lesson**:
when a cited function has an `if <flag>: return X \n return Y` shape and the calling code fixes
`<flag>` to a specific literal value, the spec must quote the *specific branch that literal value
selects*, not the general shape of the conditional — re-read the exact branch reached by the exact
arguments used, every time, rather than trusting an earlier read of "the error path" as a single
undifferentiated shape.

### Combined re-verification before re-locking (both contradictions, plus a broader sweep)

Beyond the two fixes above, re-ran the following before treating this amendment as complete, given
this is the third adjudication round on this spec and a fourth was not acceptable:

- Re-read `context_expansion_service.py`'s, `conflict_set_service.py`'s, `lineage_assembly_service.py`'s,
  and `context_budget_service.py`'s actual `except`/error-path handling (all four): none of them has
  an analogous "successful call returns list, exceptional call returns a differently-shaped
  sentinel" branch the way `search_memory` does — each either raises normally (uncaught, matching
  Coding Standards rule 15) or has the single documented defensive branch already described
  accurately elsewhere in this spec (A1's per-hit `analyze_dependencies` `"error"` key,
  independently confirmed already correct in the original §2.1 text and unaffected by this
  amendment).
- Re-confirmed (via the `search_memory`-string-literal sweep above) that no other cross-cutting
  registry beyond the five named ones exists anywhere in `mcp/`, `daemon/`, or the service layer
  this slice touches.
- Re-diffed §2.1's corrected step 3 against §6's corrected scenario 2, word for word, to confirm
  the code and the test now describe the identical shape (gate step 7).

No other section of this spec is affected by either fix. §9's acceptance bar is unchanged by
either fix in this amendment.

**Note on OMP's own blocker log**: OMP's third report did not name a SALTMDB entity id this time
(unlike the first two rounds) — nothing to check for resolvability here.

## Amendment 4 — Pre-existing dispatch.py mypy debt collides with §9, and the single-connection invariant is unachievable as literally stated (two contradictions, fourth OMP block)

**Adjudicated 2026-09-13, after OMP's fourth `BLOCKED — SPEC ADJUDICATION REQUIRED` report.** Both
claims were verified directly (a live `mypy` run against the pre-A5 baseline commit, and a direct
read of `memory_service/orchestrator.py`'s actual connection-handling code, plus one throwaway
Python probe of `PRAGMA database_list`'s return shape) before writing either fix — no service code,
wiring, or full-suite run was built or executed as part of this verification, per the standing
adjudication-verification boundary (`19155275`) established after Amendment 3's own over-build.

### Contradiction 1 — §9's `mypy dispatch.py` bar collides with pre-existing, unrelated debt

`uv run mypy src/saltmdb/daemon/dispatch.py`, run against `context-aware-search` @ `a2bf8fd` (this
worktree's own base commit, with **zero** A5 changes applied), already fails with 3 errors:
```
src/saltmdb/daemon/dispatch.py:354: error: Argument "metadata" to "update_memory_metadata" has incompatible type "Any | None"; expected "dict[Any, Any]"  [arg-type]
src/saltmdb/daemon/dispatch.py:413: error: Argument 4 to "analyze_dependencies" has incompatible type "**dict[str, object]"; expected "str | None"  [arg-type]
src/saltmdb/daemon/dispatch.py:413: error: Argument 4 to "analyze_dependencies" has incompatible type "**dict[str, object]"; expected "bool"  [arg-type]
Found 3 errors in 1 file (checked 1 source file)
```
— exactly OMP's reported errors (at lines 355/414: A5's own new import line shifts everything below
it down by one, but the errors themselves, their messages, and their originating functions
(`_dispatch_update_memory_metadata`, `_dispatch_get_related_memories`) are unchanged and unrelated
to any A5 edit). This is long-documented, deliberately-deferred technical debt: memory `50e45023`
(2026-08-10) traces this exact `dispatch.py` `kw.get()` → `Any | None` cluster back to a prior
production-readiness fix session that explicitly chose not to fix it (coercing away `Any | None`
risks breaking falsy-but-valid values like `weight=0`; the real fix needs a `TypedDict`-based
dispatch rewrite, out of scope for a lint-fix pass then and out of scope for A5 now). No prior slice
in this Milestone ever collided with it: A1's, A3's, and A4's own `mypy` acceptance commands each
scope to only their own single new service file (`context_expansion_service.py`,
`lineage_assembly_service.py`, `context_budget_service.py` respectively — confirmed by grepping
each spec's own §9) and never touched `dispatch.py` at all, because none of those slices registered
a new MCP tool. A5 is the first slice in this Milestone whose own `dispatch.py` edit brings this
file into an acceptance command's scope for the first time — so this is the first time this
pre-existing defect has ever collided with any of this Milestone's own gates, not something A1-A4
somehow already fixed or avoided.

Fixing these two functions is not an option under §0's own locked scope: A5 may only edit
`dispatch.py`'s import block and the code from `_dispatch_retrieve_context` onward (§0, corrected by
Amendment 1 to note test-file exceptions elsewhere, unchanged here) — both broken lines are in
unrelated, pre-existing, earlier functions. Requiring §9's literal "must exit 0" bar on this file
would make it impossible to satisfy without silently expanding scope past the locked boundary, the
same shape of contradiction Amendments 1-3 each found and fixed in a different part of this spec.

This also matches an already-established pattern elsewhere in this project's own history, not a
one-off exception invented for A5: memories `64579004` ("Full mypy and Bandit retain unrelated
existing failures"), `4d8065e4` ("Scoped bandit on touched implementation files: only the same
pre-existing... findings already present elsewhere... unrelated"), and `254c28f8` ("same pre-existing
mypy error only") all record prior sessions accepting a diff-against-baseline bar instead of an
absolute-zero bar specifically when a change's own gate check surfaces debt the change didn't
introduce and isn't in scope to fix. `CONTRIBUTING.md` §4 itself documents that the project's real,
full `uv run mypy src` gate already carries a nonzero baseline (memory `4d76dd05`: 9 known errors as
of alpha.102) that no single slice is expected to zero out on its own.

**Decision**: §9 is amended (see the corrected §9 above) from a single combined `mypy` invocation
covering all four files to three files (`retrieve_context_service.py`, `tools.py`, `protocol.py`)
that must still exit 0 with zero errors, plus a separate `dispatch.py` invocation whose acceptance
bar is "produces exactly these three pre-existing errors, verified unchanged from the pre-A5
baseline, and no others" — a diff-against-baseline bar, not a relaxation to "any number of errors is
fine." Any new error, anywhere in `dispatch.py`, including inside A5's own newly-added code, still
fails this criterion.

### Contradiction 2 — the single-connection invariant is unachievable as literally stated, and one real correctness bug sits underneath it

§2.1 step 1 (original text) claimed threading `db_connection` through the internal `search_memory`
call collapses "a single request" to one connection "in the whole `retrieve_context` pipeline." Two
independent problems, both confirmed directly against `memory_service/orchestrator.py`'s actual
code (not summarized from OMP's report):

1. **The literal invariant cannot hold, and this is `search_memory`'s own pre-existing
   architecture, not a regression A5 introduces.** `orchestrator.py`'s `search_memory` does use
   `conn` directly for its own top-level query construction (tag lookups, FTS row fetch, the
   `where_clauses`/`params` path) — so passing `db_connection=conn` genuinely avoids one connection
   open at that level, exactly as originally claimed for that part. But deeper in the same function
   (lines 373-374 and every call site below them), its semantic/vector/chunk/topic-scoring helpers —
   `search_primitives.semantic_search`, `chunk_candidate_search`, `retrieval_vector_search`,
   `_score_topics_with_fallback` — each accept only a `db_path: str` parameter, never
   `db_connection`, and each opens its own connection internally regardless of what
   `assemble_retrieve_context` does. This is confirmed by direct inspection of
   `orchestrator.py:373-618`: `if not db_path: db_path = get_db_path()` followed by five separate
   calls passing `db_path` (never `conn`) into these helpers. `search_memory` is architecturally
   different from A1-A4's own primitives here — it predates the "one caller opens, everyone else
   reuses" precedent A1-A4 established, and its own deeper sub-searches were never built against
   that contract. Making `search_memory`'s full call graph connection-transparent would mean adding
   a `db_connection` parameter to `search_primitives.py`'s own semantic/vector/chunk/topic
   functions — a change to files §0 already lists as untouched by this slice (`memory_service`
   isn't even named in §0's edit-scope list at all), a cross-cutting change affecting every other
   `search_memory` caller in the codebase (well beyond `retrieve_context`), and something no G1-G8
   ticket has ever asked for. Per the standing "don't accept a limitation within our own
   jurisdiction" rule (`74f6b4c0`) — this genuinely sits **outside** A5's jurisdiction, unlike A2's
   own Q7 gap that rule was written for, which A2 could fix by editing its own already-in-scope
   code. Accepted as a real, filed limitation, not silently absorbed: a follow-up wayfinder ticket
   is warranted for a future milestone to decide whether `search_primitives.py` should ever gain a
   `db_connection` parameter; it is not this slice's job to decide that now.

2. **A real, independent correctness bug sits underneath the invariant, and this one *is* in A5's
   own jurisdiction to fix.** The advisor's note is correct and confirmed: `assemble_retrieve_context`
   (as originally specified, and as OMP's own current implementation shows) calls `search_memory`
   with `db_connection=conn` and `db_path=db_path` — the **raw**, possibly-`None` parameter, not a
   value resolved from wherever `conn` actually points. When a caller supplies `db_connection`
   without `db_path` — which is exactly this suite's own established test-fixture pattern
   throughout `tests/test_retrieve_context_service.py` (`db_connection=self.conn`, `db_path` always
   omitted, confirmed by reading the file directly) — `orchestrator.py`'s own `if not db_path:
   db_path = get_db_path()` fallback resolves to the **global default** database path, not the
   fixture's own temp database, for every one of those five sub-search helper calls. This means
   `search_memory`'s semantic/vector/chunk/topic-scoring paths could silently read a different
   database than the one `conn` (and therefore the rest of `assemble_retrieve_context`'s own A1-A4
   calls) is actually using — confirmed as a real, live discrepancy, not a hypothetical one, since
   it reproduces on the test suite's own existing calling convention today. (It has not yet
   surfaced as an observable test failure only because `mode="strict"`'s relevance-abstention and
   this suite's own fixture queries apparently don't force these particular scenarios through the
   affected sub-search paths in a way that changes their asserted outcome — that is luck, not a
   reason to leave the bug in place.)

   Fixed entirely within A5's own new file (no upstream signature change): §2.1 step 1 now also
   resolves `effective_db_path` — trusting an explicit `db_path` if the caller gave one, deriving
   the real file path from `conn` itself via `PRAGMA database_list` when only `db_connection` was
   supplied (verified live: a throwaway probe against a real `init_db()`-opened connection returned
   `[(0, 'main', '<the exact file path passed to init_db>')]`), and otherwise falling back to
   exactly the same `db_path or get_db_path()` value already used to open `conn` in the no-connection-
   supplied case. Step 3's `search_memory` call now passes `db_path=effective_db_path`, never the
   raw parameter. §6 gains scenario 15, asserting `search_memory` receives the fixture's own
   database path under the suite's existing `db_connection`-only calling convention — the regression
   test for this specific bug, mirroring Amendment 3's own "this cannot recur silently a second
   time" rationale.

**Why this wasn't caught before locking (both contradictions)**: for contradiction 1, the
"single connection" framing was carried over from A1-A4's own precedent without separately
verifying that `search_memory` itself — a much older, larger, and more structurally complex
function than any of A1-A4's own primitives — actually honors a caller-supplied `db_connection`
all the way down its own call graph, rather than only at its own top level; the pre-lock gate's
step 9 confirmed `search_memory` *accepts* a `db_connection` parameter at all (true), but never
traced what that parameter actually reaches inside the function body. For contradiction 2, the
`db_path=db_path` pass-through looked like the obviously-correct choice (forward the caller's own
parameter unchanged) and was never checked against what happens when that parameter is `None` but
`db_connection` is not — the same class of gap as Amendment 2's prose-vs-code mismatch, but here
between two *sibling parameters* of the same function rather than between prose and one function's
body. **Standing lesson, now the fourth logged on this one spec**: when this slice's own contract
claims a downstream call is fully connection-transparent, verify that by reading every line the
downstream function's own connection variable touches, not just confirming the parameter exists in
its signature — and when two parameters of the same function can independently vary (`db_connection`
present, `db_path` absent, or vice versa), check what happens in the cross case explicitly, not just
the two "obviously consistent" cases (both given, or both absent).

**§9's acceptance bar is otherwise unchanged** by contradiction 2's fix — same test commands, one
additional required scenario (§6 item 15) folded into the existing "every scenario in §6/§7 must
correspond to at least one passing test" bar. Contradiction 1's own §9 change is recorded above in
the corrected §9 section directly, not duplicated here.

**Amendment gate re-run**: re-checked §0's scope list against both fixes — contradiction 2's fix
touches only `retrieve_context_service.py` and its own test file, both already in scope; no new
file, no upstream signature change, nothing added to or removed from §0's edit-scope list.
Re-confirmed (via the same `mypy`-on-baseline check used to verify contradiction 1) that no other
file in this slice's four-file wiring surface (`tools.py`, `protocol.py`, the new service file)
carries any pre-existing mypy debt of its own — both are clean at baseline, so §9's "exit 0" bar for
those three files is unaffected and unchanged.

**Note on OMP's own blocker log**: OMP's report named event id `186e9c2e-973b-416e-83a4-cf59064eb793`
for the independent second blocker. Not separately re-verified for resolvability here since this
round's evidentiary bar was met directly against source and a live probe regardless of whether that
event id resolves in this session's own SALTMDB access scope.
