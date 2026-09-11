# SPEC-CONTEXT-RETRIEVAL-A2-CONFLICT-SET-ASSEMBLY

## 1. Why

This is slice **A2** of Milestone A ("local graph-aware context retrieval," the `retrieve_context`
MCP tool) from the `wayfinder:saltmdb:graph-aware-context-retrieval` roadmap. Milestone A ships as
5 independently-specced, sequenced slices (A1–A5) per the standing user preference recorded in
SALTMDB memory `887cbf7a` and the plan locked in memory `bed2478c-9c3a-45be-a446-ee090f5a28dc`.
Slice A1 (predicate-allowlist + fan-out-cap expansion engine) is done, reviewed, and merged to
master at `fa33dd6`.

A2 implements the **contradiction/conflict-set assembly** step — standing constraint 11 from the
wayfinder map, ticket **G7** (memory `c2f86098`, resolved as event `84800410`): given the raw
`contradicts` edges A1 already surfaced touching the primary hits, group them into conflict sets
via connected components, classify each set as lifecycle-resolved (drop it — it will surface via
the ordinary lineage path, built downstream by slice A3) or unresolved (keep it, with strictly
equal visibility across all members, no "winner"), and bound the unresolved sets under their own
additive cap, separate from A1's general fan-out cap.

**Scope boundary confirmed against slice A3, not assumed** (per memories `bed2478c`, `8cd3413f`,
`2a5c1357`): the `was_flagged_contradiction`/`contradicted_with` lineage-entry signal, and whether
a `contradicts` edge is ever retroactively invalidated by lifecycle resolution, are **slice A3's
concern only** — both are scoped entirely to `lineage[head]` assembly (constraint 14), which this
slice never touches. A2's only obligation toward "is this component lifecycle-resolved" is a
boolean-shaped classification that decides `conflict_sets` inclusion; it emits no lineage entries
and has no `lineage`-shaped output at all. Slice A3 depends on A2's output for its own, separate
purpose (knowing which entries to flag) — that dependency runs A2 → A3, not the other way around,
and does not require A2 to anticipate A3's own field shapes.

**G7's locked policy this slice implements** (full text in `bed2478c`/`64b8e514`, restated here for
the file that actually has to match it): graph-connected `contradicts` edges are grouped via
connected components into one conflict set per component, preserving the underlying edges
alongside membership (never implying a direct edge between members that lack one). A conflict set
is lifecycle-resolved — surfaced as current+historical via the ordinary lineage path, NOT the
conflict reserve — only when the supersession chain already deterministically resolves it;
otherwise it surfaces as an unresolved conflict with strictly equal visibility across all members.
`contradicts`' own cap is a reserve for unresolved sets only, additive to (never drawn from) A1's
general fan-out cap; when unresolved sets exceed it, truncation is whole-set-or-nothing (never
partially hiding one member) and always surfaced explicitly.

**Codebase reuse, grounded against actual source, not roadmap prose**:

- `src/saltmdb/domain/services/context_expansion_service.py`'s `expand_context_candidates`
  (A1, master `fa33dd6`) is this slice's literal input. Its `contradicts_edges` field is a flat,
  unfiltered, uncapped, ungrouped list of raw `{relation_id, source_id, target_id, predicate}`
  dicts (`predicate` always the literal string `"contradicts"`), deduped only by `relation_id`
  across all primary hits' per-hit traversals. **Grounding fact this spec's algorithm depends on**:
  because A1 calls `analyze_dependencies(root_entity_id=<primary hit>, max_depth=1, ...)` once per
  primary hit and scans that traversal's own `edges` for `predicate == "contradicts"`, **every
  edge in `contradicts_edges` has at least one endpoint that is itself a primary hit** — a
  contradicts edge between two entities neither of which is a primary hit cannot appear in A1's
  output. This is what makes §3 step 6's per-set tiebreak always defined, with no fallback case.
- `src/saltmdb/domain/services/relation_service.py`'s `get_lineage(entity_id, direction, max_depth,
  point_in_time, db_connection)` (lines 755-906) is reused **exactly as-is, zero modification** for
  the lifecycle-connectivity check below. It already walks the closed `_LINEAGE_PREDICATES` triple
  (`revises`, `supersedes`, `consolidated_from`) bidirectionally, is point-in-time- and
  bitemporal-aware, cycle-guarded via its own path column, and returns `{"nodes": [...], "edges":
  [...], ...}` or `{"error": ...}` if `entity_id` doesn't resolve.
- `src/saltmdb/config.py`'s existing `SUPERSESSION_CHAIN_MAX_DEPTH = 10` (added for
  `_resolve_supersession_chains`, `memory_service/ranking.py`) is reused as this slice's
  `get_lineage(max_depth=...)` bound — no new depth constant.
- `_resolve_supersession_chains`'s (`ranking.py:413-580`) own batched `entity_info` fetch idiom
  (one `SELECT id, status, updated_at, created_at FROM entities WHERE id IN (...)` covering the
  whole candidate pool, not per-item) is the precedent this slice's own entity-materialization
  query follows — see §3 step 7.
- `relation_service.py`'s `_lineage_node` helper (lines 726-752) establishes this codebase's
  existing convention for a relation endpoint whose `entities` row no longer exists: return a
  synthetic `{"id": entity_id, "title": "Unknown", "status": "unknown"}` placeholder rather than
  raising or silently dropping the id. This slice's own entity-materialization query (§3 step 7)
  reuses that exact convention for any id it cannot resolve.

**Locked design decisions this spec makes that were left open upstream** (resolved via grilling
with zbalint, 2026-09-11, memory `0cb1d191`, 7/7 accepted as recommended — full rationale there,
restated here only to the depth needed to implement):

1. **Lifecycle-chain predicate scope**: the connectivity check below uses `get_lineage`'s existing
   `_LINEAGE_PREDICATES` triple as-is (via calling `get_lineage`, never a new query) — not
   `ranking.py`'s narrower, `supersedes`-only `_resolve_supersession_chains` walk, which is
   internal to `search_memory` mode="strict" and inappropriate to reuse here (it re-applies
   `search_memory`'s own caller-scoped `where_clauses`/`params`, which have no meaning for this
   slice).
2. **Resolution granularity is whole-component, all-or-nothing.** A contradicts-connected
   component (2+ member entities, computed via union-find over `contradicts_edges`) is either
   entirely lifecycle-resolved (excluded from `conflict_sets` in full) or entirely unresolved
   (included in full, with equal-visibility members) — never split partway.
3. **Connectivity-walk depth bound reuses `SUPERSESSION_CHAIN_MAX_DEPTH`** (10) as `get_lineage`'s
   `max_depth` parameter for every call this slice makes. Hitting the cap without confirming
   connectivity is treated as abstain, per decision 4.
4. **"Deterministic" (non-ambiguous) resolution means exactly one non-archived
   (`status != 'archived'`) entity among the component's own member ids** — counted strictly among
   the component's own members, never among other entities incidentally touched by the
   connectivity walk. Zero or two-plus non-archived members both abstain (treated as unresolved),
   mirroring `_resolve_supersession_chains`'s own archived-intermediate-abstains philosophy.
5. **The additive cap counts net-new (`conflict_only`) entities, not edges or sets, and is a flat
   constant — deliberately not scaled by `num_primary_hits`** the way A1's fan-out cap is: G3's
   "small-but-nonzero" framing describes an absolute visibility floor for conflicts, not a
   proportional budget; the general fan-out cap already handles "more primary hits → more
   capacity" for ordinary neighbors. Whole-set selection tiebreak, when unresolved sets
   collectively exceed the cap, is each set's own `max(primary_hit_score)` across its primary-hit
   endpoints — always defined per the grounding fact above.
6. **A2 materializes `id`/`title`/`status` inline for every `conflict_only` entity** (any
   `contradicts_edges` endpoint not already a known primary hit or A1 `expansion_candidates`
   entry), via one batched query covering the whole call — not deferred to slice A5, since A2 is
   the only slice that knows which extra ids were pulled in solely via `contradicts`.
7. **Known, accepted limitation, not silently assumed**: the connectivity check (one anchor
   member's `get_lineage` ancestors ∪ descendants) correctly detects a *linear* supersession/
   revision chain but has a real blind spot for a `consolidated_from` **merge** topology — if two
   contradicts-linked entities A and D are each independently consolidated into a new entity B,
   neither A's nor D's own ancestor/descendant traversal discovers the other. The fully-correct fix
   (BFS-to-closure seeded from all component members at once, with its own new termination bound)
   is exactly the kind of speculative machinery this effort's live-usage-first posture (G1) says
   not to build before real usage shows it's needed — there are zero live `contradicts` edges in
   the corpus today, and this is a compound rare case. **Accepted as a documented limitation**,
   mirroring the already-adjudicated precedent at memory `4cbf26ac`. A merge-diamond `contradicts`
   pair is misclassified as unresolved (the conservative, never-silently-hide-a-conflict direction)
   rather than correctly resolved — not a data-loss risk, just a missed lifecycle-resolution
   opportunity. Do not attempt to fix this as part of this slice.
8. **Numeric placeholder, explicitly marked for Milestone B recalibration**:
   `CONTEXT_EXPANSION_CONTRADICTS_CAP = 5` — a flat, small-but-nonzero reserve, same
   placeholder-marking treatment A1 gave `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`.

## 2. `src/saltmdb/config.py`

Insert immediately after the `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS = 10` line (added by A1,
currently the line directly before the blank line and `# Rework Phase 6` comment), before that
blank line:

```python

# Milestone A slice A2 (wayfinder ticket G7, memory 0cb1d191) -- retrieve_context's contradicts-
# conflict-set reserve: bounds how many net-new (conflict_only) entities an unresolved contradicts
# conflict set may pull in, additive to (never drawn from) CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's
# general fan-out cap above. Deliberately a FLAT constant, not scaled by num_primary_hits like the
# fan-out cap -- G3's "small-but-nonzero" framing is an absolute visibility floor for conflicts,
# not a proportional budget. PLACEHOLDER: not yet benchmarked against SALTMDB's own corpus (zero
# live contradicts edges exist today) -- recalibrate in Milestone B per the project's
# live-usage-first evaluation posture (memory 5d3f073c). Do not remove the placeholder framing when
# tuning this; replace this comment with the benchmark citation once a real value is locked.
CONTEXT_EXPANSION_CONTRADICTS_CAP = 5
```

No other constant is added by this slice. Do not add a new depth-bound constant for the
lineage-connectivity check — it reuses the existing `SUPERSESSION_CHAIN_MAX_DEPTH` (defined further
down in this same file, in the "Rework Phase 6" block) by importing it, not by redefining it.

## 3. New file: `src/saltmdb/domain/services/conflict_set_service.py`

A new domain-service module, sibling to `context_expansion_service.py` and `relation_service.py`,
following the same conventions: module-level `logger = logging.getLogger(__name__)`, the same
`db_connection=None, db_path: str | None = None` open-or-reuse-connection pattern.

### 3.1 `assemble_conflict_sets` — the slice's one public function

```python
def assemble_conflict_sets(
    expansion_result: dict,
    primary_hits: list[dict],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict:
```

**Input contract**: `expansion_result` is A1's `expand_context_candidates` return dict, used
literally — `expansion_result["contradicts_edges"]` (the raw edges to group) and
`expansion_result["expansion_candidates"]` (to know which `contradicts_edges` endpoints are
already-known expansion candidates, so they aren't double-counted as `conflict_only`).
`primary_hits` is the **same** `list[{"id": str, "score": float}]` A5 passed to
`expand_context_candidates` for this call — required so `primary_hit_ids`/`primary_hit_score` can
be reconstructed identically. **Caller contract**: `point_in_time` MUST be the identical value A5
passed to `expand_context_candidates` for this call (or, if neither passed one, both must resolve
their own `datetime.now(UTC).isoformat()` close enough in time that this is a non-issue in
practice) — both slices must see the same bitemporal snapshot within one `retrieve_context` call,
mirroring A1's own single-shared-`pit`-per-call discipline. An empty `contradicts_edges` list is
valid input (not an error) and must short-circuit to the all-empty shape below without issuing any
`get_lineage` or entity query.

**Algorithm** (implement exactly this sequence):

1. If `expansion_result["contradicts_edges"]` is empty, return immediately:
   `{"conflict_sets": [], "contradicts_cap": {"cap": CONTEXT_EXPANSION_CONTRADICTS_CAP,
   "eligible_count": 0, "truncated": False, "dropped_count": 0}}`. Do not open a connection for
   this case if one wasn't already supplied via `db_connection`.
2. Open one connection (`db_connection` if given, else `get_connection(db_path or get_db_path())`,
   closing it at the end only if this function opened it) — mirror A1's `should_close` pattern
   exactly. Resolve `pit = point_in_time or datetime.now(UTC).isoformat()`.
3. Build `primary_hit_ids: set[str]` and `primary_hit_score: dict[str, float]` from `primary_hits`,
   and `expansion_candidate_ids: set[str]` from `expansion_result["expansion_candidates"]`
   (each entry's `"entity_id"`).
4. **Union-find over `contradicts_edges`**: for each edge, union its `source_id` and `target_id`.
   Implement a standard union-find with path compression and a proper base case (a root node is
   its own parent; `find()` must terminate on a self-parented root — do not port the prototype's
   JS `find()` helper, which had a documented infinite-recursion bug on exactly this case, fixed in
   commit `893d8e3`; write this fresh in Python instead). Group the resulting components: for each
   distinct root, its component's `member_ids` (all entity ids that appear as a `source_id` or
   `target_id` on any edge that unioned into this root) and `component_edges` (the subset of
   `contradicts_edges` entries whose `source_id`/`target_id` both belong to this component — every
   edge belongs to exactly one component).
5. **Batched entity fetch, once for the whole call**: collect every entity id appearing across all
   components' `member_ids`, run one
   `SELECT id, title, status FROM entities WHERE id IN (...)` (empty-`IN`-safe: skip the query
   entirely if the id set is empty, though step 1 already guarantees non-empty here). Build
   `entity_info: dict[str, dict]` from the rows. For any id in the collected set that the query
   does not return a row for, synthesize `{"title": "Unknown", "status": "unknown"}` for it in
   `entity_info` — reusing `_lineage_node`'s existing missing-row convention (§1 reuse list),
   applied here as a plain dict lookup rather than a second helper call (no new abstraction; see
   Coding Standards rule 14 — this is a 2-line fallback, not a function).
6. **Classify each component**:
   - Pick the deterministic anchor = `min(member_ids)` (lexicographically smallest id — pure
     determinism choice, mirrors A1's own `entity_id asc` tertiary tiebreak precedent, not a design
     decision open to recalibration).
   - Call `get_lineage(anchor, direction="ancestors", max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
     point_in_time=pit, db_connection=conn)` and the same with `direction="descendants"`. If either
     call returns an `"error"` key (defensive only — the anchor resolved into a stored relation, so
     this should not happen in practice), log `logger.warning` naming the component and the error,
     and treat this component as unresolved (do not raise, do not skip the component from the
     output — a connectivity check that can't run must never silently drop a real conflict).
   - `anchor_lineage_ids = {node["id"] for node in ancestors_result["nodes"]} | {node["id"] for
     node in descendants_result["nodes"]}` (both already include the anchor itself, per
     `get_lineage`'s own `root_info` seeding).
   - **Connectivity confirmed** iff `member_ids <= anchor_lineage_ids` (every component member is
     in the anchor's combined ancestor/descendant set). Per §1 decision 7, a `consolidated_from`
     merge that this anchor-based check cannot see is a known, accepted limitation — do not attempt
     a broader BFS.
   - If NOT confirmed: unresolved (proceed to step 7).
   - If confirmed: count `non_archived = [m for m in member_ids if entity_info[m]["status"] !=
     "archived"]`. If `len(non_archived) == 1`: **lifecycle-resolved** — exclude this component from
     `conflict_sets` entirely (no output for it at all, not even a placeholder). If `len
     (non_archived) != 1` (0 or 2+): unresolved (proceed to step 7) — connectivity alone does not
     make a resolution deterministic per §1 decision 4.
7. **For each unresolved component**, build its candidate conflict-set entry:
   - `members`: for each `member_id` in `member_ids` (sorted ascending, for deterministic output
     ordering), tag `inclusion` with precedence `primary` > `expansion` > `conflict_only` (a member
     that is literally a primary hit is tagged `"primary"` even if it would also qualify as an
     expansion candidate). For `inclusion in {"primary", "expansion"}`, set `title: None, status:
     None` (deliberately not duplicated here — A5 already has this data from `primary_hits`/
     `expansion_candidates` and will merge it; see §1 reuse list). For `inclusion ==
     "conflict_only"`, set `title`/`status` from `entity_info[member_id]`.
   - `edges`: `component_edges` as-is (already the exact `{relation_id, source_id, target_id,
     predicate}` shape from A1's `contradicts_edges`).
   - `conflict_only_count` (int): `len([m for m in member_ids if m not in primary_hit_ids and m not
     in expansion_candidate_ids])` — this is what the cap in step 8 counts against.
   - `tiebreak_score` (float): `max(primary_hit_score[m] for m in member_ids if m in
     primary_hit_ids)` — always non-empty per the grounding fact in §1 (every component contains at
     least one primary hit, since every `contradicts_edges` entry has one).
8. **Rank and cap**: sort unresolved components by `(-tiebreak_score, tuple(sorted(member_ids)))`
   ascending on the tuple (i.e. highest `tiebreak_score` first, ties broken by the sorted
   member-id tuple ascending — pure determinism, mirrors A1's `entity_id asc` precedent). Walk the
   ranked list in order, admitting each whole component only while the running total of
   `conflict_only_count` (summed only across already-admitted components) plus this component's own
   `conflict_only_count` does not exceed `CONTEXT_EXPANSION_CONTRADICTS_CAP`. The first component
   that would exceed the cap, and every component ranked after it, are dropped — **do not skip
   ahead** to try a smaller lower-ranked component that might still fit (that would reorder
   inclusion by size rather than relevance, which is not what "ranked by relevance, capped" means
   here). `eligible_count` = total number of unresolved components (before admission).
   `dropped_count` = number of unresolved components NOT admitted. `truncated = dropped_count > 0`.
9. Strip the now-unneeded `conflict_only_count`/`tiebreak_score` working fields from each admitted
   component's final `members`/`edges`-only shape (they were computation aids, not part of the
   output contract below).

**Output contract** (exact keys, exact types):

```python
{
    "conflict_sets": [
        {
            "members": [
                {
                    "id": str,
                    "inclusion": "primary" | "expansion" | "conflict_only",
                    "title": str | None,   # populated only when inclusion == "conflict_only"
                    "status": str | None,  # populated only when inclusion == "conflict_only"
                },
                ...  # sorted by id ascending
            ],
            "edges": [
                {"relation_id": str, "source_id": str, "target_id": str, "predicate": "contradicts"},
                ...
            ],
        },
        ...  # admitted unresolved sets only, ranked highest-tiebreak-score first; lifecycle-
             # resolved components never appear here at all
    ],
    "contradicts_cap": {
        "cap": int,
        "eligible_count": int,
        "truncated": bool,
        "dropped_count": int,
    },
}
```

This function raises nothing itself for resolvable/valid input; the only defensive path (an
anchor's `get_lineage` call returning an `"error"` key) is handled per step 6 (logged, component
treated as unresolved, never raised).

## 4. `tests/test_conflict_set_service.py` (new file)

Follow `tests/test_context_expansion_service.py`'s exact fixture conventions: `init_db` + real
SQLite (no mocks — this module's value is the graph algebra over real rows), the same `_memory_id`/
`_memory`/`_relation`/`_raw_relation` helper set (copy them, do not import across test files), plus
one new helper `_archive(entity_id: str) -> None` running
`UPDATE entities SET status='archived' WHERE id=?` — the exact pattern `test_relation_service.py`'s
own lineage/supersession fixtures already use (e.g. its
`test_lineage_walks_all_lifecycle_predicates_and_preserves_status`). Build `expansion_result`-shaped
dicts by hand in each test (either call `expand_context_candidates` for real against the same
fixtures, or construct the minimal dict literal directly) rather than mocking it.

Required test scenarios (write test-first, red before green, per this workspace's `tdd` skill and
Coding Standards rule 19):

1. **Empty input**: `assemble_conflict_sets({"contradicts_edges": [], ...}, [])` returns
   `{"conflict_sets": [], "contradicts_cap": {"cap": CONTEXT_EXPANSION_CONTRADICTS_CAP,
   "eligible_count": 0, "truncated": False, "dropped_count": 0}}` without opening a connection
   (pass no `db_connection`/`db_path` and assert no exception — confirms the short-circuit).
2. **Single unconnected contradicts edge, no lineage at all**: primary hit P contradicts non-primary
   N, neither has any `revises`/`supersedes`/`consolidated_from` edge. Assert one conflict set with
   members `[{id: N, inclusion: "conflict_only", title: "N's title", status: "raw"}, {id: P,
   inclusion: "primary", title: None, status: None}]` (sorted by id) and the one edge.
3. **Lifecycle-resolved via `supersedes`, correctly excluded**: P contradicts N; separately, P
   `supersedes` N directly (P is source, N is target); archive N. Assert `conflict_sets == []` (the
   component is fully excluded, not present with any marker) and `contradicts_cap.eligible_count ==
   0`.
4. **Lifecycle-resolved via `revises` and via `consolidated_from`**: two more variants of scenario 3,
   substituting each of the other two `_LINEAGE_PREDICATES` for `supersedes`, confirming Q1's "use
   the whole triple" decision is actually exercised, not just `supersedes`.
5. **Connected but NOT lifecycle-resolved — zero non-archived members**: P contradicts N; P
   `supersedes` N; archive BOTH P and N (simulating a stale chain nobody cleaned up further).
   Assert the component IS present in `conflict_sets` (connectivity alone is not sufficient per §1
   decision 4).
6. **Connected but NOT lifecycle-resolved — two non-archived members**: P contradicts N; P
   `supersedes` N; leave both live (neither archived). Assert the component IS present in
   `conflict_sets`.
7. **Not connected at all**: P contradicts N; P and N each have unrelated lineage edges to other,
   uninvolved entities (not to each other). Assert the component IS present (connectivity check
   correctly finds member N absent from anchor's lineage set).
8. **3-member component via shared contradicts endpoint**: P1 contradicts N; P2 also contradicts N
   (two separate primary hits sharing one contradicted neighbor). Assert ONE conflict set with all
   three members and both edges — not two separate sets.
9. **`conflict_only` vs `expansion` inclusion precedence**: N is both a `contradicts`-edge endpoint
   AND already present in `expansion_result["expansion_candidates"]` (reached from the same primary
   hit via an allowlisted predicate too). Assert N's `inclusion == "expansion"`, not
   `"conflict_only"`, and its `title`/`status` are `None` (not independently fetched).
10. **`primary` beats `expansion` precedence**: construct a case where a member id is both a primary
    hit and (hypothetically) would also qualify as an expansion candidate; assert `inclusion ==
    "primary"`.
11. **Cap truncation, whole-set-or-nothing, no skip-ahead**: construct enough distinct unresolved
    components (with distinct `conflict_only_count`s and `tiebreak_score`s, patch/import the real
    `CONTEXT_EXPANSION_CONTRADICTS_CAP` rather than hardcoding it) so that the second-ranked
    component would overflow the cap but a THIRD, smaller, lower-ranked component would have fit.
    Assert the third component is NOT admitted (no skip-ahead), `truncated is True`, and
    `dropped_count` counts every component from the first overflow onward.
12. **Tiebreak uses `max(primary_hit_score)` across a set's primary-hit endpoints**: two unresolved
    components with equal `conflict_only_count`, differing only in their connected primary hits'
    scores; assert the higher-score one ranks first.
13. **Deterministic tertiary ordering**: two unresolved components tied on `tiebreak_score`; assert
    ordering by the sorted-member-id-tuple is stable across repeated calls.
14. **Missing entity row falls back to "Unknown"/"unknown"**: construct a `contradicts_edges` entry
    referencing an id with no corresponding `entities` row at all (delete it after creating the
    relation row directly, or insert a raw relation referencing a fabricated UUID). Assert that
    member's `title == "Unknown"` and `status == "unknown"`, and the call does not raise.
15. **Anchor's `get_lineage` error path is non-fatal**: construct a scenario where the anchor id
    somehow fails to resolve inside `get_lineage` (e.g. by monkeypatching `get_lineage` for this one
    test to return `{"error": "..."}`, since a real fixture can't naturally trigger this — see
    `context_expansion_service.py`'s own precedent of handling a defensive-only error path). Assert
    the component is treated as unresolved and the call does not raise.

## 5. Out of scope

- Anything in `src/saltmdb/domain/services/context_expansion_service.py` — this slice calls no
  function from it directly; it consumes its *output* dict as a plain data structure. No signature
  change to `expand_context_candidates`.
- Any modification to `get_lineage`, `analyze_lineage`, `_lineage_node`, `analyze_dependencies`, or
  any other existing `relation_service.py` function — called exactly as they exist today.
- Any modification to `_resolve_supersession_chains` or anything else in
  `memory_service/ranking.py` — `SUPERSESSION_CHAIN_MAX_DEPTH` is imported, not touched.
- `lineage[head]` assembly, `was_flagged_contradiction`/`contradicted_with`, and the
  `lineage.historical` cap — slice A3, entirely. This slice's classification (resolved vs.
  unresolved) is consumed by A3 as an input signal in a future slice's own spec; this spec does not
  define or anticipate that interface.
- The `retrieve_context` MCP tool itself, its request-parameter signature, any wiring into
  `mcp/tools.py`/`daemon/dispatch.py`/`daemon/protocol.py`, or the final `metadata.fan_out.*`/
  `metadata.budget.*` namespacing — slice A5. This slice's own `contradicts_cap` key is a
  standalone, unnamespaced field at this layer, matching A1's own `fan_out` precedent; A5 folds it
  into the final envelope later.
- Context-token-budget packing (`primary_truncated`/`expansion_truncated`, fastembed token
  counting) — slice A4.
- Fixing the `consolidated_from` merge-topology blind spot in the connectivity check (§1 decision
  7) — explicitly deferred, not a bug to fix opportunistically here.
- Recalibrating `CONTEXT_EXPANSION_CONTRADICTS_CAP`'s numeric value — Milestone B.
- Any caller-supplied override of the lifecycle-predicate scope, the depth bound, or the
  resolution-granularity policy — all foreclosed by §1 decisions 1-3 for Milestone A.

## 6. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_conflict_set_service.py -v
```
must exit 0, and every scenario in §4 must correspond to at least one passing test (a reviewer
checks this by name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite, including `tests/test_context_expansion_service.py`
and `tests/test_relation_service.py` — this slice adds a new file and one new constant only).

```bash
uv run ruff check src/saltmdb/domain/services/conflict_set_service.py tests/test_conflict_set_service.py && \
uv run ruff format --check src/saltmdb/domain/services/conflict_set_service.py tests/test_conflict_set_service.py && \
uv run mypy src/saltmdb/domain/services/conflict_set_service.py
```
must exit 0, matching this repo's documented lint/type gate (`CONTRIBUTING.md`).

## 0. Status

**LOCKED**

**Scope**: may create `src/saltmdb/domain/services/conflict_set_service.py` (new file) and
`tests/test_conflict_set_service.py` (new file); may edit `src/saltmdb/config.py` (add exactly one
new constant, `CONTEXT_EXPANSION_CONTRADICTS_CAP`, per §2 — no other edit to that file). Does not
touch: `src/saltmdb/domain/services/context_expansion_service.py` and
`tests/test_context_expansion_service.py` (A1's output is consumed as data, never as a function
this slice calls into or modifies — verified by re-reading both files in full before locking this
spec; neither currently references conflict sets, contradicts grouping, or anything this slice
would collide with), `src/saltmdb/domain/services/relation_service.py` (reused as-is via
`get_lineage` — no signature change, no new parameter; re-read in full, confirms `_lineage_node`'s
missing-row fallback shape cited in §1/§3 is exactly `{"title": "Unknown", "status": "unknown"}`
plus other keys this slice doesn't need), `src/saltmdb/domain/services/memory_service/ranking.py`
(only `SUPERSESSION_CHAIN_MAX_DEPTH` is imported from it — the module itself, including
`_resolve_supersession_chains`, is untouched; re-read in full, confirms the constant's current value
is `10` and its only existing importers are within that module's own package), `src/saltmdb/mcp/
tools.py` / `src/saltmdb/daemon/dispatch.py` / `src/saltmdb/daemon/protocol.py` (no MCP tool surface
in this slice — Milestone A slice A5), `src/saltmdb/utils/predicate_vocabulary.py` (the closed
predicate vocabulary is read indirectly via already-stored `contradicts` edges, never written), and
`src/saltmdb/db/schema.py` (no schema change — `relations` and `entities` are read-only here).

**Pre-lock gate completed against the current tree** (spec-writing skill, all 9 steps): sections
above were drafted in Why → mechanical → Out of scope → Acceptance order before this Status section
was finalized. Acceptance's three commands were checked against the actual current tree state
(`context_expansion_service.py`/`test_context_expansion_service.py` exist at the paths named
throughout; `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`/`SUPERSESSION_CHAIN_MAX_DEPTH` both exist in
`config.py` at the values cited). No lockfile or generated-manifest side effect applies to this
slice (no new dependency). No existing string/constant content is being changed (only a new
constant is added — grepped `CONTEXT_EXPANSION_CONTRADICTS_CAP` across the tree: zero prior
occurrences, confirming this name is not already in use anywhere, e.g. as a stale hardcoded test
literal). §3's algorithm prose and its "exact keys, exact types" Output contract block were
diffed against each other line-by-line (the `title`/`status: None`-for-non-conflict_only rule and
the `conflict_only_count`/`tiebreak_score` working-fields-stripped-before-output rule both appear
identically in both places) — this is the exact failure shape A1's own Amendment 1 caught, checked
here before locking rather than after. A concrete worked instance (scenario 8's 3-member
shared-neighbor component) was traced through every locked rule (union-find grouping, the
inclusion-precedence rule, the tiebreak rule, the cap rule) before writing it into §4, and a second
worked instance (the `consolidated_from` merge case) was traced through the connectivity check
specifically to surface §1 decision 7 as an explicit, user-confirmed limitation rather than an
undocumented gap — this is what produced grilling round 2's Q7, not something discovered after
locking.

## Amendment 1 — Extract `classify_contradicts_components`; fix the Q7 merge-diamond blind spot

**Adjudicated decision (2026-09-12, with zbalint, during Milestone A slice A3's grilling round)**:
§1 decision 7 above — "accepted, documented limitation" for the `consolidated_from` merge-diamond
blind spot — is **reversed**. zbalint set a new standing rule this session (memory `74f6b4c0`):
*"we do not accept anything as limitation as long we can implement or fix it and does not require
anything beyond our jurisdiction."* The original "low-stakes, reversible" framing that justified
accepting this gap was actually solving for the wrong risk (avoiding touching already-shipped
code) — this module is still unmerged (`feature/context-retrieval-a2` @ `295e715`, not on
`master`), so there is no real backward-compatibility cost to fixing it properly. This amendment
does that, and simultaneously extracts the classification logic into its own function so that
Milestone A slice A3 (lineage assembly, `SPEC-CONTEXT-RETRIEVAL-A3-LINEAGE-ASSEMBLY.md`) can reuse
the exact same resolved/unresolved verdict rather than re-deriving a second, possibly-divergent
one — closing a related gap surfaced in A3's own grilling round (a component A2 classifies
`unresolved` must never simultaneously get flagged inside A3's `lineage` output; sharing this one
function is what guarantees that by construction, not by two independently-written checks agreeing
by luck).

**Why extraction, not just a bugfix in place**: A3 needs the identical "is this contradicts pair
lifecycle-resolved" answer A2 already computes. Two independently-written versions of the same
30-line union-find + connectivity + archived-count check is exactly what Coding Standards rule 16
("don't copy-paste logic that already exists elsewhere — import/reuse it") forecloses. Pulling it
out to a shared function, called by both A2's `assemble_conflict_sets` and A3's `assemble_lineage`,
is the only version of "fix Q7" that also serves A3 without duplication.

### A. Before — current `assemble_conflict_sets` (lines 40-136 of the committed file)

The committed function currently does everything itself, inline, after its early-empty-return
(lines 29-38, **unchanged by this amendment**): opens a connection; resolves `pit`; builds
`primary_hit_ids`/`primary_hit_score`/`expansion_candidate_ids`; runs the union-find over
`contradicts_edges` to build `components_by_root`; does one batched `entity_info` fetch (`title`,
`status`) over every id appearing in any edge; then, in a single loop over
`components_by_root.values()` (lines 99-178), computes the connectivity check **from one anchor
only** (`anchor = min(component_member_ids)`, one pair of `get_lineage` ancestors/descendants
calls), decides resolved-vs-unresolved inline, and — for unresolved components only — immediately
builds the `members`/`edges`/`conflict_only_count`/`tiebreak_score` shape used by the later
ranking/cap steps (7-9, unchanged, see part C).

### B. After — `classify_contradicts_components`, new public function in the same file

Insert this as a new top-level function, placed before `assemble_conflict_sets` in the file (so
`assemble_conflict_sets` can call it without a forward reference). `assemble_conflict_sets` itself
is reduced to a thin caller from this point in its algorithm onward — see part C for its new shape.

```python
def classify_contradicts_components(
    contradicts_edges: list[dict[str, Any]],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Group contradicts_edges into connected components via union-find and classify each as
    lifecycle-resolved or unresolved. The single source of truth for this classification, shared by
    assemble_conflict_sets (this module) and lineage_assembly_service.assemble_lineage (Milestone A
    slice A3) -- callers must never re-derive their own version of this check, so the two slices can
    never disagree about the same component (a component this function marks unresolved appearing in
    A2's conflict_sets must never also be flagged inside A3's lineage output, and vice versa).

    Returns one entry per contradicts-connected component:
    {"member_ids": set[str], "edges": list[dict], "resolved": bool}
    Order is not significant -- callers that need a deterministic order (A2's own ranking step) sort
    the entries they keep themselves, as this function's own caller already did before this
    extraction.

    An empty contradicts_edges list returns [] immediately without opening a connection (mirrors
    assemble_conflict_sets's own existing empty-input short-circuit, unaffected by this amendment).
    """
    if not contradicts_edges:
        return []

    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        pit = point_in_time or datetime.now(UTC).isoformat()

        # Union-find grouping -- moved verbatim from the original inline logic, unchanged.
        parent: dict[str, str] = {}

        def find(entity_id: str) -> str:
            if entity_id not in parent:
                parent[entity_id] = entity_id
            if parent[entity_id] != entity_id:
                parent[entity_id] = find(parent[entity_id])
            return parent[entity_id]

        def union(first: str, second: str) -> None:
            first_root = find(first)
            second_root = find(second)
            if first_root != second_root:
                parent[second_root] = first_root

        for edge in contradicts_edges:
            union(edge["source_id"], edge["target_id"])

        components_by_root: dict[str, dict[str, Any]] = {}
        for edge in contradicts_edges:
            source_id = edge["source_id"]
            target_id = edge["target_id"]
            root = find(source_id)
            component = components_by_root.setdefault(root, {"member_ids": set(), "edges": []})
            component["member_ids"].update((source_id, target_id))
            component["edges"].append(edge)

        # Batched entity-STATUS fetch for this function's own classification purposes only.
        # Deliberately separate from assemble_conflict_sets's own entity_info fetch (title+status,
        # needed for its conflict_only member-shaping) -- keeping this function self-contained and
        # independently testable is worth one extra small query; contradicts-connected components
        # are tiny (zero live edges in the corpus today), so this is not a real cost (Coding
        # Standards rule 12 -- no speculative optimization without measured need).
        member_ids = set(parent)
        status_by_id: dict[str, str] = {}
        placeholders = ",".join("?" for _ in member_ids)
        rows = conn.execute(
            f"SELECT id, status FROM entities WHERE id IN ({placeholders})",
            tuple(member_ids),
        ).fetchall()
        status_by_id = {row[0]: row[1] for row in rows}
        for entity_id in member_ids:
            status_by_id.setdefault(entity_id, "unknown")  # _lineage_node's missing-row convention

        results: list[dict[str, Any]] = []
        for component in components_by_root.values():
            component_member_ids: set[str] = component["member_ids"]
            resolved = _component_lifecycle_resolved(
                component_member_ids, status_by_id, pit, conn
            )
            results.append(
                {
                    "member_ids": component_member_ids,
                    "edges": component["edges"],
                    "resolved": resolved,
                }
            )
        return results
    finally:
        if should_close:
            close_connection(conn)


def _component_lifecycle_resolved(
    member_ids: set[str],
    status_by_id: dict[str, str],
    point_in_time: str,
    conn: sqlite3.Connection,
) -> bool:
    """Q7 FIX: multi-round BFS-to-closure connectivity check, seeded from one anchor
    (`min(member_ids)`, same deterministic choice as before) but — unlike the original single-call
    check — continuing to expand from every NEWLY discovered node too, not just the anchor. This is
    what actually closes the merge-diamond gap: for A and D each `consolidated_from`-merged into a
    new entity B, round 1 (querying only the anchor, say A) discovers B; the original code stopped
    there and never found D. This version's round 2 queries B (newly discovered in round 1) and
    finds D via B's own ancestors/descendants -- exactly the extra hop the old check was missing.

    Bounded to SUPERSESSION_CHAIN_MAX_DEPTH rounds (reusing the existing constant, not a new one):
    each round can only usefully extend the closure by one more get_lineage hop-set from a
    previously-undiscovered node, and get_lineage's own single call is already bounded to
    max_depth=SUPERSESSION_CHAIN_MAX_DEPTH hops in each direction -- needing more than
    SUPERSESSION_CHAIN_MAX_DEPTH rounds of "a brand new node needs its own full traversal" to reach
    closure would mean a supersession/consolidation graph far deeper and more convoluted than
    anything else in this codebase is designed to handle. Hitting the round cap without covering
    every member abstains (returns False / unresolved) -- the same conservative default this
    function already uses for a get_lineage error, and the same "never silently drop a real
    conflict" philosophy §1 decision 7 originally established.
    """
    anchor = min(member_ids)
    visited: set[str] = {anchor}
    frontier: set[str] = {anchor}
    for _ in range(SUPERSESSION_CHAIN_MAX_DEPTH):
        if not frontier:
            break
        newly_discovered: set[str] = set()
        for entity_id in frontier:
            ancestors_result = get_lineage(
                entity_id,
                direction="ancestors",
                max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
                point_in_time=point_in_time,
                db_connection=conn,
            )
            descendants_result = get_lineage(
                entity_id,
                direction="descendants",
                max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
                point_in_time=point_in_time,
                db_connection=conn,
            )
            if "error" in ancestors_result or "error" in descendants_result:
                logger.warning(
                    "Could not check lifecycle connectivity for entity %s in component %s: %s",
                    entity_id,
                    sorted(member_ids),
                    ancestors_result.get("error") or descendants_result.get("error"),
                )
                continue
            reached = {node["id"] for node in ancestors_result["nodes"]} | {
                node["id"] for node in descendants_result["nodes"]
            }
            newly_discovered |= reached - visited
        visited |= newly_discovered
        if member_ids <= visited:
            break
        frontier = newly_discovered
    if not (member_ids <= visited):
        return False  # connectivity not confirmed within the round cap -> unresolved

    non_archived = [m for m in member_ids if status_by_id.get(m, "unknown") != "archived"]
    return len(non_archived) == 1
```

### C. After — `assemble_conflict_sets` becomes a thin caller

Replace the union-find/component-building/connectivity block (the current lines 54-136) with:

```python
components = classify_contradicts_components(
    contradicts_edges, point_in_time=pit, db_connection=conn
)
member_ids = set().union(*(c["member_ids"] for c in components)) if components else set()
# ... existing batched entity_info (title+status) fetch over member_ids, UNCHANGED ...

unresolved_components: list[dict[str, Any]] = []
for component in components:
    if component["resolved"]:
        continue
    component_member_ids: set[str] = component["member_ids"]
    # ... existing members/edges/conflict_only_count/tiebreak_score construction (original
    # lines 138-178), UNCHANGED -- it never referenced the connectivity check directly, only
    # component_member_ids and component["edges"] ...
```

Everything from this point onward — the sort, the cap-admission loop, the output-shaping, the
`contradicts_cap` bookkeeping (original lines 180-209) — is **unchanged**, byte-for-byte. `pit`,
`primary_hit_ids`, `primary_hit_score`, `expansion_candidate_ids`, and the existing `entity_info`
(title+status) batched fetch are all still computed by `assemble_conflict_sets` itself, exactly as
before — only the union-find and connectivity-check portion moves out.

**Output contract of `assemble_conflict_sets` is unchanged.** This is a refactor plus a bugfix to
an internal helper, not a reshaping of A2's own locked contract from §3.1 — every one of the 15
existing scenarios in `tests/test_conflict_set_service.py` must still pass unmodified.

### D. New required test scenario

Add to `tests/test_conflict_set_service.py` (or a new `tests/test_conflict_set_classification.py`
if that reads more cleanly for `classify_contradicts_components`'s own unit-level coverage — either
file placement is acceptable, but the scenario itself is mandatory):

16. **Merge-diamond connectivity, proving the Q7 fix**: create three entities A, B, D. `A
    contradicts D` (the only `contradicts` edge — the component under test is exactly
    `{A, D}`; B is not itself a contradicts-edge member). Separately: `B consolidated_from A` and
    `B consolidated_from D` (B is a new entity that independently absorbed both A and D). Archive A
    only; leave B and D live. Neither A's own nor D's own direct `get_lineage` ancestors/descendants
    call reaches the other (each reaches only itself and B) — the pre-fix single-anchor check would
    classify this component `unresolved` (connectivity never confirmed, so the archived-count check
    is never even reached). Assert the fixed check classifies it `resolved`: round 1 (from anchor
    `min({A, D})`) discovers B; round 2 queries B itself and discovers the other member via B's own
    ancestors/descendants — closing `member_ids <= visited` — and exactly one of `{A, D}` (D) is
    non-archived. Name this test so its own docstring/test name states the pre-fix behavior it
    regresses against (e.g. `test_merge_diamond_connectivity_requires_multi_round_bfs`), so a future
    reader can confirm the fix is real without having to reconstruct this reasoning from scratch.

### E. Amendment acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_conflict_set_service.py -v
```
must exit 0, all 15 original scenarios plus new scenario 16 passing.

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must exit 0, no regression anywhere else in the suite.

```bash
uv run ruff check src/saltmdb/domain/services/conflict_set_service.py tests/test_conflict_set_service.py && \
uv run ruff format --check src/saltmdb/domain/services/conflict_set_service.py tests/test_conflict_set_service.py && \
uv run mypy src/saltmdb/domain/services/conflict_set_service.py
```
must exit 0.

**Scope for this amendment**: may edit `src/saltmdb/domain/services/conflict_set_service.py` (the
extraction + fix above) and `tests/test_conflict_set_service.py` (new scenario 16, and only the
minimal fixture/import changes the extraction itself requires — e.g. importing
`classify_contradicts_components` if any existing test calls the internals directly, which none of
the original 15 scenarios do per A2's own original spec's black-box testing convention). Does not
touch `config.py` (no new constant — `SUPERSESSION_CHAIN_MAX_DEPTH` is reused, not redefined),
`context_expansion_service.py`, `relation_service.py`, or any other file untouched by A2's original
spec.
