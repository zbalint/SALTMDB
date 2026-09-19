# SPEC-CONTEXT-RETRIEVAL-A1-EXPANSION-ENGINE

## 0. Status

**LOCKED**

**Scope**: may create `src/saltmdb/domain/services/context_expansion_service.py` (new file) and
`tests/test_context_expansion_service.py` (new file); may edit `src/saltmdb/config.py` (add two
new constants only, see §2). Does not touch: `src/saltmdb/domain/services/relation_service.py`
(reused as-is via its public `analyze_dependencies` function — no signature change, no new
parameter), `src/saltmdb/mcp/tools.py` / `src/saltmdb/daemon/dispatch.py` / `src/saltmdb/daemon/protocol.py`
(no MCP tool surface in this slice — that is Milestone A slice A5), `src/saltmdb/utils/predicate_vocabulary.py`
(the closed predicate vocabulary is read, not written), `src/saltmdb/domain/services/memory_service/**`
(no interaction with `search_memory`, ranking, or lifecycle resolution in this slice), and
`src/saltmdb/db/schema.py` (no schema change — the `relations` table is read-only here).

## 1. Why

This is slice **A1** of Milestone A ("local graph-aware context retrieval," the `retrieve_context`
MCP tool) from the `wayfinder:saltmdb:graph-aware-context-retrieval` roadmap. Milestone A is being
shipped as 5 independently-specced, sequenced slices (A1–A5) rather than one monolithic spec, per
the standing user preference recorded in SALTMDB memory `887cbf7a` ("omp works the best when we
give him smaller tasks") and the plan locked in memory `bed2478c-9c3a-45be-a446-ee090f5a28dc`.

A1 implements the **predicate-allowlist + fan-out-cap graph-expansion engine** — the piece that,
given a set of primary search hits, decides which directly-connected memories are worth pulling
into a context bundle, and bounds how many. This is standing constraints 8 and 9 from the
wayfinder map (tickets **G3** and **G4**):

- **G3 (predicate allowlist, ticket `16d1d5f3`, resolved as event `0ebfb511`)**: predicate-based
  context expansion uses a binary allowlist, not a weighted cost. Default-include:
  `elaborates_on`, `depends_on`, `corrects`, `derived_from`, `resolves`. Default-exclude:
  `related_to`, `distinguishes_from`, `similar_to`, `verifies`, `caused_by`, `part_of`.
  `contradicts` is force-included but is **explicitly out of this slice's scope** — its
  representation, conflict-set grouping, and additive cap are slice A2's job (ticket G7). Reserved
  lifecycle predicates (`supersedes`/`consolidated_from`/`revises`) are out of scope for this
  allowlist entirely. Traversal is direction-symmetric. No caller override for Milestone A.
- **G4 (fan-out/node-count bound, ticket `aaac326b`, resolved as event `5d578d13`)**: out-of-network
  neighbor expansion (a neighbor not itself a primary hit) is bounded by a per-primary-hit-scaled
  global cap, adopting GraphRAG's local-search formula as-is: `max_out_of_network_neighbors =
  top_k_relationships * num_primary_hits`. In-network relationships (both endpoints already primary
  hits) are always kept, outside this cap. Survivors are ranked by mutual-neighbor-count, ties
  broken by FTS/vector relevance score alone (no recency component, per the standing
  precision-over-latency default `e605d707`). Truncation is always surfaced explicitly. The exact
  numeric value of `top_k_relationships` is deferred to Milestone B calibration per the project's
  live-usage-first evaluation posture (memory `5d3f073c`) — this spec ships a named, clearly-marked
  placeholder default rather than blocking on a number that doesn't exist yet.

**Codebase reuse (verified against current source, not assumed)**: `analyze_dependencies`
(`src/saltmdb/domain/services/relation_service.py:591-748`) already performs exactly the 1-hop
(or deeper) recursive-CTE traversal this slice needs — cycle-guarded, direction-aware
(`outbound`/`inbound`/`both`), point-in-time-aware — for a single root entity. It applies **no
predicate filter at all** (confirmed by reading `_dependency_cte_sql`, lines 543-590: the CTE
joins purely on `source_id`/`target_id`, with no `WHERE predicate IN (...)` clause), so it returns
every relation type touching the root, including reserved lifecycle predicates and legacy
`similar_to`. This slice's whole job is: call it once per primary hit at `max_depth=1`, then apply
the G3 allowlist gate and G4 cap/ranking in Python across the union of those per-hit results. No
new SQL, no change to `analyze_dependencies` itself.

**Locked design decisions this spec makes that were left open upstream** (both flagged as
open in the roadmap's "Not yet specified" section and in memory `bed2478c`):

1. **Traversal depth = 1 hop, no caller override.** The wayfinder map's own Destination section
   describes Milestone A as "bounded **one-hop-plus** expansion," and the G4-adopted GraphRAG
   formula is itself inherently single-hop (in-network = already-primary-hit pairs; out-of-network
   = "one more hop"). A deeper multi-hop walk is not part of Milestone A's design; `analyze_dependencies`
   is called with `max_depth=1`. This mirrors G3's "no caller override" precedent — depth is not a
   parameter this slice exposes.
2. **`retrieval_provenance` exact shape for an expansion candidate** (G6 left this narrative —
   "originating primary hit, traversed predicate, direction, hop depth/path" — not a locked key
   list): `{"reason": "graph_expansion", "origin_hit_id": <str>, "predicate": <str>, "direction":
   "outbound"|"inbound", "hop_depth": 1}`. `direction` is from the primary hit's point of view:
   `"outbound"` if the primary hit is the edge's `source_id`, `"inbound"` if it is the `target_id`.
   A candidate reached from more than one primary hit carries one such record per distinct
   `(origin_hit_id, predicate, direction)` triple it was actually reached by (see §2, `provenance`
   field) — this is what lets slice A5 render a full "why is this here" explanation rather than
   only the single edge that happened to win the ranking.
3. **Relevance tie-break score for an out-of-network candidate.** G4 locks "ties broken by FTS/vector
   relevance score alone" but an out-of-network candidate is not itself a search hit and has no
   score of its own. This spec defines it as **the maximum `score` among the primary hits it is
   connected to** — the natural secondary signal once mutual-neighbor-count is already the primary
   ranking key, and consistent with the precision-over-latency default (favor a candidate connected
   to at least one highly-relevant hit over one connected only to weakly-relevant hits).
4. **Numeric placeholders, explicitly marked for Milestone B recalibration**:
   `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS = 10` (matches GraphRAG's own documented default,
   verified against source during the G4 research ticket, memory `5a3694d1`). This is the *only*
   numeric constant A1 needs — the `contradicts`-specific cap (also deferred) belongs to slice A2,
   not here.

## 2. `src/saltmdb/config.py`

Insert immediately after the existing `RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS` block (current
lines 232-237), before the blank line and the `# Rework Phase 6` comment at line 239:

```python
# Milestone A slice A1 (wayfinder ticket G4, memory 5d578d13) -- retrieve_context's graph-expansion
# fan-out bound: max_out_of_network_neighbors = CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS *
# num_primary_hits, adopting GraphRAG local-search's own formula/default as-is (verified against
# GraphRAG source during wayfinder research ticket 5a3694d1). PLACEHOLDER: not yet benchmarked
# against SALTMDB's own corpus -- recalibrate in Milestone B per the project's live-usage-first
# evaluation posture (memory 5d3f073c). Do not remove the placeholder framing when tuning this;
# replace this comment with the benchmark citation once a real value is locked, matching the
# treatment already given to RELATION_GATE_MIN_SIMILARITY_THRESHOLD/SUPERSESSION_MIN_SIMILARITY_THRESHOLD.
CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS = 10
```

No other constants are added by this slice. (`contradicts`'s own additive cap is slice A2's
concern, added when A2 is specced — do not add it here speculatively.)

## 3. New file: `src/saltmdb/domain/services/context_expansion_service.py`

A new domain-service module, sibling to `relation_service.py`, following this codebase's existing
module conventions (module-level `logger = logging.getLogger(__name__)`, the same
`db_connection=None, db_path: str = None` open-or-reuse-connection pattern used throughout
`relation_service.py`, e.g. `analyze_dependencies` lines 620-625/746-748).

### 3.1 Module-level constant

```python
# Default-include predicates for Milestone A context expansion (wayfinder ticket G3, memory
# 0ebfb511). `contradicts` is deliberately NOT in this set -- it is force-included but handled
# entirely by slice A2 (conflict-set assembly), never by this slice's general allowlist/cap path.
# Reserved lifecycle predicates (supersedes/consolidated_from/revises) and legacy similar_to are
# likewise deliberately absent -- they are excluded, not merely unlisted, per G3's locked policy.
DEFAULT_CONTEXT_EXPANSION_PREDICATES: frozenset[str] = frozenset(
    {"elaborates_on", "depends_on", "corrects", "derived_from", "resolves"}
)
```

### 3.2 `expand_context_candidates` — the slice's one public function

```python
def expand_context_candidates(
    primary_hits: list[dict],
    *,
    point_in_time: str | None = None,
    db_connection=None,
    db_path: str | None = None,
) -> dict:
```

**Input contract**: `primary_hits` is a list of `{"id": str, "score": float}` dicts — the caller
(slice A5, eventually) has already run `search_memory` and extracted just these two fields per
hit. This function does not call `search_memory` itself and has no dependency on ranking/RRF
internals. An empty `primary_hits` list is valid input (not an error) and must return the "no
candidates" shape below with all lists empty and `fan_out.cap == 0`.

**Algorithm** (implement exactly this sequence — this is the load-bearing logic, not a suggestion):

1. Open one connection (`db_connection` if given, else `get_connection(db_path or get_db_path())`,
   closing it at the end only if this function opened it) — mirror `analyze_dependencies`'s own
   `should_close` pattern exactly, including using the same `point_in_time` (defaulted to
   `datetime.now(UTC).isoformat()` if not given, matching `analyze_dependencies`'s own default) for
   every call below, so all per-hit traversals see the same bitemporal snapshot.
2. Build `primary_hit_ids: set[str]` and a `primary_hit_score: dict[str, float]` from `primary_hits`.
3. For each primary hit id `h`, call `analyze_dependencies(root_entity_id=h, max_depth=1,
   direction="both", point_in_time=<the shared pit>, db_connection=conn)`. If the call returns an
   `"error"` key (e.g. the id fails to resolve — defensive only; primary hits should always
   resolve), log a `logger.warning` naming the hit id and the error, and skip that hit's
   contribution entirely rather than failing the whole call — one bad hit must not blank out
   context for the others.
4. From each successful call's `edges` list, keep only edges whose `predicate` is in
   `DEFAULT_CONTEXT_EXPANSION_PREDICATES` **or** equals `"contradicts"`. Every other predicate
   (reserved lifecycle, legacy `similar_to`, any unlisted string) is dropped here and never
   reappears in this function's output — this is the G3 allowlist gate, and it applies uniformly to
   in-network and out-of-network edges alike (G3 draws no such distinction).
5. Partition the surviving non-`contradicts` edges into:
   - **in-network**: both `source_id` and `target_id` are in `primary_hit_ids`. Dedupe by
     `relation_id` across all per-hit calls (the same edge can be discovered from either endpoint).
     Keep all of them, uncapped.
   - **out-of-network**: exactly one endpoint is in `primary_hit_ids`; the other (call it `N`) is
     not. For each such edge, record a provenance tuple `(origin_hit_id, predicate, direction)`
     against node `N`, where `origin_hit_id` is whichever endpoint is the primary hit,
     `direction = "outbound"` if that primary hit is `source_id` else `"inbound"`.
6. Group out-of-network provenance tuples by target node `N`. For each distinct `N`:
   - `mutual_neighbor_count` = number of *distinct* `origin_hit_id`s across `N`'s provenance tuples
     (not the number of tuples/edges — a node reached from the same hit via two predicates still
     counts as 1 for this purpose).
   - `tiebreak_score` = `max(primary_hit_score[origin_hit_id] for each distinct origin_hit_id
     reaching N)`.
7. Rank the distinct out-of-network nodes by `(mutual_neighbor_count desc, tiebreak_score desc,
   entity_id asc)` — the trailing `entity_id asc` is a pure determinism tie-break (mirrors the
   existing `sorted(..., key=lambda n: (n["depth"], n["id"]))` precedent at
   `relation_service.py:914`), not a design decision open to recalibration.
8. Compute `cap = CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS * len(primary_hits)` (0 if `primary_hits`
   is empty). Keep the top `cap` ranked nodes; the rest are dropped. `dropped_count = max(0,
   eligible_count - cap)` where `eligible_count` is the number of distinct out-of-network nodes
   found in step 6, before capping. `truncated = dropped_count > 0`.
9. For each surviving out-of-network node, build one `retrieval_provenance` entry per distinct
   `(origin_hit_id, predicate, direction)` triple that reached it (§1 decision 2) — a node reached
   two different ways carries a 2-element `provenance` list, not just the one that won the ranking.
10. Separately, collect **every** `contradicts`-predicate edge touching any primary hit (from the
    same per-hit `analyze_dependencies` calls in step 3, re-scanned for `predicate == "contradicts"`)
    into `contradicts_edges`, completely unfiltered and uncapped — raw `{relation_id, source_id,
    target_id, predicate}` dicts, deduped by `relation_id`. This slice does no ranking, capping, or
    grouping of these; that is slice A2's job. Do not omit this list to "simplify" the output —
    slice A2 depends on it existing exactly in this shape.

**Output contract** (exact keys, exact types — slice A5 will consume this literally):

```python
{
    "in_network_edges": [
        {"relation_id": str, "source_id": str, "target_id": str, "predicate": str},
        ...
    ],
    "expansion_candidates": [
        {
            "entity_id": str,
            "title": str,
            "mutual_neighbor_count": int,
            "tiebreak_score": float,
            "retrieval_provenance": [
                {
                    "reason": "graph_expansion",
                    "origin_hit_id": str,
                    "predicate": str,
                    "direction": "outbound" | "inbound",
                    "hop_depth": 1,
                },
                ...  # one per distinct (origin_hit_id, predicate, direction) reaching this node
            ],
        },
        ...  # ranked order (highest-ranked first), already capped
    ],
    "contradicts_edges": [
        {"relation_id": str, "source_id": str, "target_id": str, "predicate": str},
        ...  # predicate is always the literal string "contradicts" here (see Amendment 1)
    ],
    "fan_out": {
        "cap": int,
        "eligible_count": int,
        "truncated": bool,
        "dropped_count": int,
    },
}
```

`title` in `expansion_candidates` comes from the corresponding `target_title`/`source_title` field
already returned by `analyze_dependencies`'s `edges` list — do not issue a second query for it.

This function raises nothing itself for a resolvable/valid input; the only place an underlying
`analyze_dependencies` error is possible (an unresolvable primary-hit id) is handled per step 3
above (logged and skipped, not raised).

## 4. `tests/test_context_expansion_service.py` (new file)

Follow this suite's existing fixture conventions exactly, matching `tests/test_relation_service.py`'s
imports and setup pattern (`saltmdb.db.schema.init_db`, `saltmdb.domain.services.memory_service.store_memory`
to create fixture entities, `saltmdb.domain.services.relation_service.store_relation`/
`bulk_store_relations` to create edges, `_axis_vector`-style orthogonal embeddings only if a test
needs `store_memory` to succeed without triggering unrelated dedup/quality gates — copy that
helper rather than reinventing it). Real SQLite via `init_db`, not mocks — this module's whole
value is the graph algebra over real rows, and `analyze_dependencies` is reused as-is, unmocked.

Required test scenarios (write these test-first, red before green, per this workspace's `tdd`
skill and Coding Standards rule 19 — this list is the acceptance bar's content, not exhaustive
implementation guidance for a scenario OMP is free to skip):

1. **Empty input**: `expand_context_candidates([])` returns the all-empty shape from §3.2, with
   `fan_out == {"cap": 0, "eligible_count": 0, "truncated": False, "dropped_count": 0}`.
2. **Allowlist gate, positive**: a primary hit with a `depends_on` edge to a non-primary node
   surfaces that node in `expansion_candidates` with the correct `retrieval_provenance`.
3. **Allowlist gate, negative**: a primary hit with a `related_to` edge to a non-primary node does
   NOT surface that node anywhere in the output (not in `expansion_candidates`, not counted in
   `fan_out.eligible_count`).
4. **Allowlist gate excludes reserved/legacy predicates too**: a primary hit connected via
   `supersedes` (create it the same way `test_relation_service.py`'s lineage tests do, or via
   `store_relation` directly if the reserved-predicate write gate allows test-level construction —
   check `resolve_or_create_predicate`'s reserved handling first) or `similar_to` does not surface
   that neighbor either.
5. **In-network vs out-of-network split**: three primary hits A, B, C where A and B are connected
   by an included-predicate edge, and A is also connected to non-primary D by an included
   predicate. Assert the A-B edge appears in `in_network_edges` and D appears in
   `expansion_candidates` — and that the A-B edge does NOT also appear in `expansion_candidates`
   or count toward `fan_out`.
6. **Mutual-neighbor-count ranking**: a non-primary node reached from 2 distinct primary hits
   ranks above one reached from only 1, regardless of relevance score ordering.
7. **Relevance tiebreak**: two non-primary nodes each reached from exactly 1 primary hit, same
   mutual-neighbor-count (1); the one whose connecting primary hit has the higher `score` ranks
   first.
8. **Deterministic tertiary tiebreak**: two non-primary nodes tied on both mutual-neighbor-count
   and tiebreak_score rank by `entity_id` ascending, and this ordering is stable across repeated
   calls.
9. **Fan-out cap truncation**: construct enough distinct out-of-network candidates to exceed
   `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS * len(primary_hits)` (patch/import the real constant,
   don't hardcode `10` in the assertion) and assert `fan_out.truncated is True`,
   `fan_out.dropped_count` is exactly right, and the survivors are exactly the top-ranked ones by
   the §3.2 ordering.
10. **In-network edges are never capped**: construct more in-network edges than the fan-out cap
    value and assert none of them are dropped.
11. **Multi-provenance node**: a non-primary node reached from the same primary hit via two
    different included predicates (or from two different primary hits) carries a
    `retrieval_provenance` list with one entry per distinct reaching triple, not just one.
12. **`contradicts` edges bypass the allowlist and the cap entirely**: a primary hit connected via
    `contradicts` to a non-primary node appears in `contradicts_edges` as a raw
    `{relation_id, source_id, target_id, predicate}` dict (predicate == "contradicts", per
    Amendment 1), does NOT appear in `expansion_candidates`, and does not count toward `fan_out.eligible_count`
    or `fan_out.dropped_count` — even when the general cap is already exceeded by unrelated
    candidates.
13. **Unresolvable primary-hit id is skipped, not fatal**: a `primary_hits` entry whose `id` does
    not resolve to a real entity does not raise; the call still returns a valid result reflecting
    the remaining resolvable hits (construct this by passing a syntactically-plausible but
    nonexistent UUID as one of several hits).
14. **Direction is hit-relative, both ways**: one primary hit is the edge's `source_id` for one
    included-predicate edge and the `target_id` for another; assert `retrieval_provenance.direction`
    reads `"outbound"` and `"inbound"` respectively, not both the same.

## 5. Out of scope

- The `retrieve_context` MCP tool itself, its request-parameter signature, and any wiring into
  `mcp/tools.py`/`daemon/dispatch.py`/`daemon/protocol.py` — slice A5.
- `contradicts` conflict-set grouping, resolved/unresolved visibility, and `contradicts`'s own
  additive cap — slice A2. This slice only surfaces the raw `contradicts_edges` list A2 needs; it
  must not rank, cap, or group them.
- Lineage assembly, `was_flagged_contradiction`/`contradicted_with`, and the `lineage.historical`
  cap — slice A3.
- Context-token-budget packing (`primary_truncated`/`expansion_truncated`, fastembed token
  counting) — slice A4. This slice's own `fan_out` truncation signal is a distinct axis
  (node-eligibility) from A4's budget (payload-size); do not conflate them or pre-emptively add a
  `metadata.fan_out`-shaped wrapper here — that JSON namespacing is A5's assembly job.
- Calling `search_memory` to produce `primary_hits` — that remains the caller's (eventually A5's)
  responsibility; this function only ever receives already-computed `{id, score}` pairs.
- Recalibrating `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`'s numeric value — Milestone B.
- Any change to `analyze_dependencies`, `_dependency_cte_sql`, or any other existing
  `relation_service.py` function — this slice calls them exactly as they exist today.
- Predicate-weighted (non-binary) inclusion, caller-supplied allowlist overrides, or a
  caller-supplied traversal depth — all explicitly foreclosed by G3/G4/decision-1 above for
  Milestone A.

## 6. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_context_expansion_service.py -v
```
must exit 0, and every scenario in §4 must correspond to at least one passing test (a reviewer
checks this by name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite — this slice adds a new file and two new
constants only, so nothing else should be affected).

```bash
uv run ruff check src/saltmdb/domain/services/context_expansion_service.py tests/test_context_expansion_service.py && \
uv run ruff format --check src/saltmdb/domain/services/context_expansion_service.py tests/test_context_expansion_service.py && \
uv run mypy src/saltmdb/domain/services/context_expansion_service.py
```
must exit 0, matching this repo's documented lint/type gate (`CONTRIBUTING.md` §on pre-commit
checks).

## Amendment 1 — `contradicts_edges` entry shape (adjudicated after OMP BLOCKED)

OMP correctly flagged a genuine internal contradiction in the original §3.2, not a false positive:
step 10's algorithm text specified raw `contradicts_edges` dicts as `{relation_id, source_id,
target_id, predicate}` (4 keys), while the "exact keys, exact types" Output contract JSON block
immediately below it specified only `{relation_id, source_id, target_id}` (3 keys, no
`predicate`). §4 scenario 12 referenced "raw, as specified in §3.2" without resolving which of the
two shapes that meant. These are mutually exclusive under the spec's own "exact keys, exact types"
requirement, so OMP was right to stop rather than silently pick one.

**Adjudicated decision: `contradicts_edges` entries carry all four keys** —
`{relation_id, source_id, target_id, predicate}` — matching step 10's dict literal and
`in_network_edges`'s shape exactly. `predicate` is always the literal string `"contradicts"` for
every entry in this list, since step 10's own collection criterion (`predicate == "contradicts"`)
guarantees it — the field carries no variable information, only a constant label.

**Why 4 keys over 3, given the field is provably constant:**
1. **Shape uniformity.** Both `in_network_edges` and `contradicts_edges` are "raw, undecorated edge"
   lists in the same output contract; giving them different key sets means a consumer (A2, A5, or a
   future caller) iterating generically over "the raw edge lists in this payload" hits an
   unnecessary special case. `in_network_edges` needs `predicate` because its value varies across
   the 5 allowlisted predicates; `contradicts_edges` doesn't need it for that reason, but including
   it costs nothing and removes the asymmetry.
2. **Zero marginal cost.** `analyze_dependencies`'s edge dicts already carry `predicate`; this is a
   dict-comprehension key, not a new query, computation, or abstraction — it doesn't trip Coding
   Standards rule 14 (no single-use helpers) since nothing new is being built.
3. **Self-documentation.** A raw JSON dump of `contradicts_edges` (e.g. during debugging or in a
   log) reads as self-describing edge records without requiring the reader to already know, purely
   from the list's key name, what predicate every entry implicitly has.
4. **Forward safety under uncertainty.** Slice A2 (which consumes this list for conflict-set
   assembly) is not yet specced. Preserving a field that's free to keep and cheap to ignore is lower
   risk than omitting it and potentially reopening this shape once A2's actual needs are known —
   consistent with this spec's existing precision-over-latency/conservative bias (§1 decision 3).

This was a user-adjudicated call (not clerically self-evident from the existing text alone) — both
shapes were defensible before this amendment; 4 keys is now the single locked answer.

**Changes made** (both in this same commit):
- §3.2 Output contract: `contradicts_edges` entries now specify
  `{"relation_id": str, "source_id": str, "target_id": str, "predicate": str}`.
- §4 scenario 12: replaced the ambiguous "raw, as specified in §3.2" with the explicit 4-key dict
  shape and the `predicate == "contradicts"` invariant, so the test file can be written without
  re-deriving this decision from a now-fixed §3.2.

No other section of this spec is affected. §3.2 step 10's original 4-key wording was already
correct and required no change; the fix was solely to the Output contract block and the scenario-12
cross-reference that both disagreed with it.
