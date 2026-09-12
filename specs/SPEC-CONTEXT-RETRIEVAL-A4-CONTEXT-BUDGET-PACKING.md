# SPEC-CONTEXT-RETRIEVAL-A4-CONTEXT-BUDGET-PACKING

## 0. Status

**LOCKED, with Amendment 1** (see bottom of this document) — §3 step 4/5 and §4 scenario 9 are
superseded by Amendment 1's empty-text normalization; read those sections together with the
amendment, not in isolation.

**Scope**: may create `src/saltmdb/domain/services/context_budget_service.py` (new file) and
`tests/test_context_budget_service.py` (new file); may edit `src/saltmdb/config.py` (add exactly
two new constants, `CONTEXT_BUDGET_DEFAULT_TOKENS` and `CONTEXT_BUDGET_MAX_TOKENS`, per §2 — no
other edit to that file). Depends on, and consumes as data only, the already-shipped outputs of
`expand_context_candidates` (A1, `context_expansion_service.py`) and `assemble_conflict_sets` (A2,
`conflict_set_service.py`) — no signature change, no call *into* either module beyond reading their
return dicts. Does not touch: `src/saltmdb/domain/services/context_expansion_service.py`,
`src/saltmdb/domain/services/conflict_set_service.py`, `src/saltmdb/domain/services/lineage_assembly_service.py`
(A3's output is not part of this slice's budget-competing pool at all — re-confirmed against A3's
own Out-of-scope section, which already states this explicitly from its side); `src/saltmdb/domain/services/relation_service.py`;
`src/saltmdb/domain/services/embedding_service.py` (its public `get_model()` is reused as-is — no
signature change, no new wrapper); `src/saltmdb/mcp/tools.py` / `src/saltmdb/daemon/dispatch.py` /
`src/saltmdb/daemon/protocol.py` (no MCP tool surface in this slice — Milestone A slice A5); and
`src/saltmdb/db/schema.py` (no schema change — re-read in full during drafting, confirms `entities`
has no per-row cached token-count column; every count in this slice is computed live).

**Pre-lock gate completed against the current tree** (spec-writing skill): every section below was
drafted in Why → mechanical → Out of scope → Acceptance order before this Status section was
finalized. Acceptance's commands were checked against the actual tree state in this worktree
(branched from `context-aware-search` post-merge, containing A1+A2+A2-Amendment-1+A3 already).
Grepped `CONTEXT_BUDGET_DEFAULT_TOKENS`, `CONTEXT_BUDGET_MAX_TOKENS`, `context_budget_service`,
`pack_context_budget`, and `conflict_reserve_tokens_used` across the tree: zero prior occurrences of
any of them, confirming none collide with existing code or stale test literals. Two library facts
were verified live against the actually-installed `fastembed` version before locking §3's algorithm
(not assumed from the roadmap's prose, which only asserted the method's existence):

1. `TextEmbedding.token_count(texts: str | Iterable[str], batch_size: int = 1024, **kwargs) -> int`
   exists on the installed `fastembed` (confirmed via `inspect.signature`/`inspect.getsource` in
   this worktree's venv).
2. **Critical, previously-undocumented finding**: passing a list of multiple texts to
   `token_count()` returns the **sum** of tokens across all of them, not a per-text breakdown —
   confirmed empirically: `model.token_count(["hello world", "a much longer sentence with many
   more tokens in it"])` returns `17`, while `model.token_count("hello world")` alone returns `4`
   (i.e. the second string alone accounts for the other 13; there is no way to recover a per-item
   split from one batched call). The roadmap's `CONTEXT-budget accounting` decision (event
   `2e613e32`) named the method but did not anticipate this — it is recorded here as this slice's
   own grounding fact, not a deviation from that decision. **Consequence for §3: every candidate's
   token count MUST be computed via its own individual `token_count(text)` call — this slice makes
   one such call per distinct candidate, never a single batched call across the pool.**

Also confirmed live: the `entities` table's actual content column is `full_content` (`schema.py`
line 412) — not `content`. §3's token accounting reads this column by name; a spec or implementer
assuming a `content` column would silently query a nonexistent one.

**Note (Amendment 1)**: this pre-lock gate did not itself run `token_count("")` against the
installed tokenizer — it verified the batching-sum quirk (finding 2 above) and the `full_content`
column name, but §3 step 4's original claim that "token count of an empty string is `0`" was an
unverified assumption, not a third grounded finding alongside 1-2 above. OMP's worker ran the
missing gate check and found the installed tokenizer returns `2` for `""` (BOS/EOS special-token
overhead), which is what Amendment 1 resolves.

## 1. Why

This is slice **A4** of Milestone A ("local graph-aware context retrieval," the `retrieve_context`
MCP tool) from the `wayfinder:saltmdb:graph-aware-context-retrieval` roadmap, per the 5-slice
breakdown locked in memory `bed2478c-9c3a-45be-a446-ee090f5a28dc` (superseding correction in
`b9ec8a0e-04ca-4e9e-91af-03306a53f50d`). Slices A1 (expansion engine), A2 (conflict-set assembly, +
Amendment 1), and A3 (lineage assembly) are done, reviewed, and merged into `context-aware-search`
(memory `9520119`, this worktree's own branch point) — the integration branch this and all future
Milestone-A slices merge into per the branch-strategy rule locked in memory `64658415`.

A4 implements **context-budget packing** — wayfinder ticket G8 (`cdf2c7cf-805f-4801-aaa2-39b813dcfe5d`),
closed via grilling 2026-09-10 and elaborated in the roadmap's "Context-budget accounting" decision
(event `2e613e32`) — standing constraint 12. `retrieve_context` needs a deterministic, real-token-count
ceiling on the payload it hands back, distinct from A1's node-count fan-out cap (eligibility vs.
payload size are independent axes) and from A2's conflict-set reserve (additive, never competing for
the same space). Concretely: given A1's `expansion_result` (primary hits already searched +
expansion candidates already ranked) and A2's `conflict_sets_result`, decide which of those
candidates fit inside a token budget, via one continuous highest-relevance-first greedy pack across
primary hits and expansion candidates together — primary hits are **not** exempt — while conflict-set
members always ride along outside the budget entirely, reported separately.

**Locked design decisions** (grounded against the roadmap's G8 resolution, A1/A2's actual shipped
contracts, and this slice's own prototype precedent at
`src/saltmdb/mcp/prototype_retrieve_context.html` lines 338-399 — the only prior artifact that
worked this algorithm out concretely; restated here to the depth needed to implement):

1. **Accounting unit is real token count**, via `fastembed`'s already-resident BGE tokenizer
   (`embedding_service.get_model().token_count(text)`) — zero new dependency, confirmed live per
   §0. No predicate-based or memory-type-based budget discount (mirrors A1/G3's already-locked "no
   predicate-inclusion mechanism beyond the binary allowlist" stance, extended here: real token
   count already reflects each item's actual size).
2. **What text is counted**: each candidate's `entities.full_content` value alone — not
   `title`-concatenated, not a serialized preview of the eventual `memories[]` JSON entry.
   `full_content` dominates any real memory's token footprint; title overhead is comparatively
   negligible and, more importantly, coupling this slice's byte-accounting to A5's not-yet-locked
   exact envelope shape (which may add its own independent, roughly-fixed per-entry overhead) would
   make this slice's correctness depend on a spec that doesn't exist yet. This is a deliberate,
   documented accounting choice — not a hidden gap — analogous in kind to A3's "`archived_at` is
   sourced from `get_lineage`'s `updated_at`, there is no real column" call (§1 decision 5 there).
3. **The budget is a caller-supplied optional parameter with a server-side default and a hard
   ceiling.** This slice's public function takes `budget_tokens: int | None`; `None` resolves to
   `CONTEXT_BUDGET_DEFAULT_TOKENS`; any caller-supplied value (including the default) is clamped
   down to `CONTEXT_BUDGET_MAX_TOKENS` if it exceeds it — never clamped up. Only the two numeric
   values are Milestone-B placeholders (per `cd084ced`'s "similarity/budget thresholds are
   calibrated defaults, not universal constants" standing constraint) — the parameter/default/ceiling
   *shape* itself is locked now, mirroring `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`'s own treatment.
4. **Separate downstream axis from A1's fan-out cap.** This slice never re-applies or interacts with
   `expansion_result["fan_out"]` — it receives `expansion_result["expansion_candidates"]` exactly as
   A1 already capped it and packs *that* list against the token budget. Node-eligibility (A1) and
   payload-size (A4) are independent passes over independent axes, per G8's own locked framing.
5. **A2's conflict-set reserve is separate and additive — it never competes with primary/expansion
   content for budget space, and there is no shared pool or backtracking between the two axes**
   (re-confirmed by zbalint over a competing shared-pool/backtracking alternative sol:high proposed,
   per the roadmap's G8 resolution). Every `conflict_only`-tagged member across
   `conflict_sets_result["conflict_sets"]` is **always** included in this slice's output, regardless
   of the primary/expansion budget's state; its token cost is counted and reported
   (`conflict_reserve_tokens_used`) but never checked against `budget_tokens`.
6. **Primary hits are not exempt from the budget.** Truncation is one continuous
   highest-relevance-first pass across primary hits *and* expansion candidates, not two separately
   capped passes. Concretely (validated against the prototype's own working implementation, which is
   the only prior artifact to work this algorithm out to running code): build one ordered candidate
   list by **concatenating** `primary_hits` (in the order the caller supplied them — assumed
   already relevance-ordered by whatever produced them, per A1/A2's own identical no-re-sort
   treatment of `primary_hits`) followed by `expansion_result["expansion_candidates"]` (already
   ranked by A1's own `mutual_neighbor_count`/`tiebreak_score`/`entity_id` order — re-sorting here
   would contradict A1's own already-locked ranking). This is **not** a merged/interleaved sort by a
   shared cross-population score (primary hits and expansion candidates use structurally different
   scores — search relevance vs. graph proximity — with no locked formula to compare them
   directly); "one continuous pass" means the two already-ordered tiers are walked back-to-back in
   one greedy loop, giving primary hits first claim on the budget while still subjecting them to it.
7. **Per-candidate inclusion is independent, first-fit greedy — not stop-on-first-miss.** Walk the
   concatenated list in order; for each candidate, if `running_total + candidate_tokens <=
   effective_budget`, include it and add to the running total; otherwise, drop it **and keep
   evaluating the remaining candidates** (do not `break`) — a smaller later candidate can still fit
   in whatever budget remains even after an earlier, larger candidate was dropped. This exact
   behavior (no early exit) is what the prototype implements and is being locked here as the real
   algorithm, not merely a prototype simplification.
8. **Truncation signals are namespaced and scoped separately from A1's identically-named field.**
   G4 (A1) and G8 (A4) each independently locked a field literally named `expansion_truncated` for
   two genuinely different axes (fan-out node-eligibility vs. token-budget payload-size) — the
   roadmap's schema-prototype correction namespaces them `metadata.fan_out.expansion_truncated`
   (A1, already shipped as `expansion_result["fan_out"]["truncated"]`/`["dropped_count"]`) vs.
   `metadata.budget.expansion_truncated` (this slice). This slice's own output uses
   `primary_truncated`/`primary_dropped_count` and `expansion_truncated`/`expansion_dropped_count`
   as its own top-level keys (under this slice's own `budget` sub-dict) — the `metadata.budget.*`
   namespacing itself is A5's wiring concern (assembling the final MCP result envelope), not
   something this slice's return value needs to pre-namespace.
9. **Dropped-count semantics scoped to the eligible candidate window** (mirrors A1's own
   `fan_out.dropped_count` framing): `primary_dropped_count`/`expansion_dropped_count` count only
   candidates that were in this slice's own input pool and failed the budget check — never any
   count from A1's separate `fan_out.dropped_count` (nodes A1 itself never surfaced as candidates at
   all) or A2's separate `contradicts_cap.dropped_count`.
10. **No overlap between the three pools by construction, reused as a grounding fact, not
    re-verified defensively.** A1 already guarantees `expansion_candidates` entity ids are disjoint
    from `primary_hit_ids` (an expansion candidate is, by A1's own filter, never a primary hit
    itself). A2 already guarantees a `conflict_only`-tagged member is neither a primary hit nor an
    expansion candidate (A2's own `inclusion` computation excludes both explicitly). This slice
    relies on both guarantees rather than re-deriving them — a duplicate id appearing in more than
    one of the three role sets would be an upstream (A1/A2) contract violation, not something this
    slice defends against.

## 2. `src/saltmdb/config.py`

Add immediately after `LINEAGE_HISTORICAL_CAP = 5` (the last of the three existing Milestone-A
constants), before the `# NOTE: accept_or_abstain's...` comment block — mirroring
`CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`/`CONTEXT_EXPANSION_CONTRADICTS_CAP`'s own placeholder
framing exactly (this is the PLACEHOLDER case, unlike `LINEAGE_HISTORICAL_CAP`'s fixed-now case):

```python
# Milestone A slice A4 (wayfinder ticket G8, memory cdf2c7cf) -- retrieve_context's context-budget
# packing: the default real-token-count ceiling (via fastembed's TextEmbedding.token_count(), see
# context_budget_service.py) applied when a caller does not supply their own budget_tokens value.
# PLACEHOLDER: not yet benchmarked against SALTMDB's own corpus -- recalibrate in Milestone B per
# the project's live-usage-first evaluation posture (memory 5d3f073c). Do not remove the
# placeholder framing when tuning this; replace this comment with the benchmark citation once a
# real value is locked, matching the treatment already given to CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS/
# CONTEXT_EXPANSION_CONTRADICTS_CAP.
CONTEXT_BUDGET_DEFAULT_TOKENS = 4000

# Milestone A slice A4 (wayfinder ticket G8, memory cdf2c7cf) -- retrieve_context's context-budget
# packing: the hard ceiling a caller-supplied budget_tokens is clamped DOWN to if it exceeds this
# value (never clamped up -- a caller requesting less than this gets exactly what they asked for).
# Same PLACEHOLDER status as CONTEXT_BUDGET_DEFAULT_TOKENS above -- recalibrate together in
# Milestone B, do not remove the placeholder framing independently of that constant.
CONTEXT_BUDGET_MAX_TOKENS = 16000
```

## 3. New file: `src/saltmdb/domain/services/context_budget_service.py`

A new domain-service module, sibling to `context_expansion_service.py`, `conflict_set_service.py`,
and `lineage_assembly_service.py`, following the same conventions: module-level
`logger = logging.getLogger(__name__)`, the same `db_connection=None, db_path: str | None = None`
open-or-reuse-connection pattern.

### 3.1 `pack_context_budget` — the slice's one public function

```python
def pack_context_budget(
    expansion_result: dict[str, Any],
    primary_hits: list[dict[str, Any]],
    conflict_sets_result: dict[str, Any],
    *,
    budget_tokens: int | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
```

**Input contract**: `expansion_result` is A1's `expand_context_candidates` return dict, used
literally — only `expansion_result["expansion_candidates"]` (each entry's `"entity_id"`) is read;
`expansion_result["fan_out"]`/`["in_network_edges"]`/`["contradicts_edges"]` are not touched by this
slice (per §1 decision 4). `primary_hits` is the same `list[{"id": str, "score": float}]` A1/A2/A3
all receive for this call — only `"id"` is used here, in the order given (no re-sort, per §1
decision 6). `conflict_sets_result` is A2's `assemble_conflict_sets` return dict, used literally —
only `conflict_sets_result["conflict_sets"]` (each entry's `"members"` list, each member's `"id"`
and `"inclusion"`) is read; `conflict_sets_result["contradicts_cap"]` is not touched. No
`point_in_time` parameter — unlike A1/A2/A3, this slice performs no lineage/dependency traversal and
has no time-sensitive query.

**Algorithm** (implement exactly this sequence):

1. `effective_budget = min(budget_tokens if budget_tokens is not None else
   CONTEXT_BUDGET_DEFAULT_TOKENS, CONTEXT_BUDGET_MAX_TOKENS)` — the clamp-down-only rule of §1
   decision 3, applied uniformly whether or not the caller supplied a value.
2. Build the three id sets, in this exact precedence order (mirrors §1 decision 10's non-overlap
   grounding fact — used here only to build role-tagged rows, not re-verified):
   - `primary_ids: list[str] = [hit["id"] for hit in primary_hits]` (list, not set — order matters
     for the greedy pack; duplicates, if any slipped through upstream, are not de-duplicated here —
     an upstream contract violation, not this slice's concern per §1 decision 10).
   - `expansion_ids: list[str] = [c["entity_id"] for c in expansion_result["expansion_candidates"]]`
     (list, in A1's own already-locked order).
   - `conflict_only_ids: set[str] = {member["id"] for conflict_set in
     conflict_sets_result["conflict_sets"] for member in conflict_set["members"] if
     member["inclusion"] == "conflict_only"}` (set — order doesn't matter, no budget competition, no
     greedy pack).
   If `primary_ids`, `expansion_ids`, and `conflict_only_ids` are all empty, return the zero-result
   shape from step 6 immediately — do not open a connection.
3. Open one connection (`db_connection` if given, else `get_connection(db_path or get_db_path())`,
   closing it at the end only if this function opened it) — mirror A1/A2/A3's `should_close` pattern
   exactly.
4. Batch-fetch `full_content` for every distinct id across all three sets in **one** query (mirrors
   A2's own batched entity-materialization query pattern):
   ```python
   all_ids = set(primary_ids) | set(expansion_ids) | conflict_only_ids
   placeholders = ",".join("?" for _ in all_ids)
   rows = conn.execute(
       f"SELECT id, full_content FROM entities WHERE id IN ({placeholders})", tuple(all_ids)
   ).fetchall()
   content_by_id = {row[0]: row[1] for row in rows}
   ```
   An id with no matching row (a stale reference — same defensive posture A2 gives a missing entity
   row) falls back to `""` via `content_by_id.get(entity_id, "")`. **See Amendment 1**: the installed
   tokenizer does not return `0` for an empty string (it returns a nonzero BOS/EOS overhead), so step
   5 below normalizes empty text to `0` explicitly rather than passing it through the raw tokenizer
   call — this does not fail the tokenizer call, does not silently inflate or crash the pack.
5. Compute `token_counts: dict[str, int] = {}` — **one call per distinct id**, either
   `get_model().token_count(text)` for non-empty text or the explicit `0` normalization for empty
   text (never a single batched `token_count` call across multiple ids, per §0's critical finding).
   **Per Amendment 1**, empty text (whether from a missing row's `""` fallback or a real row whose
   `full_content` is itself an empty string) is defined as costing exactly `0` tokens, overriding
   whatever the raw tokenizer returns for `""`:
   ```python
   from saltmdb.domain.services.embedding_service import get_model
   model = get_model()
   for entity_id in all_ids:
       text = content_by_id.get(entity_id, "")
       token_counts[entity_id] = 0 if text == "" else model.token_count(text)
   ```
6. Greedy pack (§1 decisions 6-7, implementing the prototype's exact loop): walk `primary_ids` then
   `expansion_ids`, concatenated, in that order, as one continuous pass:
   ```python
   used = 0
   primary_packed: list[str] = []
   primary_dropped: list[str] = []
   expansion_packed: list[str] = []
   expansion_dropped: list[str] = []
   for entity_id in primary_ids:
       cost = token_counts[entity_id]
       if used + cost <= effective_budget:
           primary_packed.append(entity_id)
           used += cost
       else:
           primary_dropped.append(entity_id)
   for entity_id in expansion_ids:
       cost = token_counts[entity_id]
       if used + cost <= effective_budget:
           expansion_packed.append(entity_id)
           used += cost
       else:
           expansion_dropped.append(entity_id)
   ```
   No `break` in either loop (§1 decision 7) — every candidate in both lists is independently
   evaluated against whatever budget remains at the time it is reached.
7. `conflict_reserve_tokens_used = sum(token_counts[entity_id] for entity_id in conflict_only_ids)`
   — always fully included, never checked against `effective_budget` (§1 decision 5).
8. Return:
   ```python
   {
       "packed_entity_ids": {"primary": primary_packed, "expansion": expansion_packed},
       "dropped_entity_ids": {"primary": primary_dropped, "expansion": expansion_dropped},
       "conflict_only_entity_ids": sorted(conflict_only_ids),
       "token_counts": token_counts,
       "budget": {
           "unit": "tokens",
           "limit": effective_budget,
           "used": used,
           "primary_truncated": len(primary_dropped) > 0,
           "primary_dropped_count": len(primary_dropped),
           "expansion_truncated": len(expansion_dropped) > 0,
           "expansion_dropped_count": len(expansion_dropped),
           "conflict_reserve_tokens_used": conflict_reserve_tokens_used,
       },
   }
   ```
   `token_counts` (every distinct id considered, across all three pools) is returned so A5 does not
   need to re-open a connection or re-call the tokenizer when assembling the final `memories[]`
   envelope — mirrors A1's own `retrieval_provenance`-attached-once-not-recomputed-downstream
   precedent.

**Step 2's early-return zero-result shape** (all three input sets empty):
```python
{
    "packed_entity_ids": {"primary": [], "expansion": []},
    "dropped_entity_ids": {"primary": [], "expansion": []},
    "conflict_only_entity_ids": [],
    "token_counts": {},
    "budget": {
        "unit": "tokens", "limit": effective_budget, "used": 0,
        "primary_truncated": False, "primary_dropped_count": 0,
        "expansion_truncated": False, "expansion_dropped_count": 0,
        "conflict_reserve_tokens_used": 0,
    },
}
```
`effective_budget` (step 1) is computed before this check, so the zero-result `budget.limit` still
reflects the resolved/clamped value rather than a hardcoded placeholder.

This function raises nothing itself for resolvable/valid input; there is no defensive/error-path
branch analogous to A1/A2/A3's `get_lineage`/`analyze_dependencies` `"error"` key handling, because
this slice makes no traversal call — only a plain `SELECT ... WHERE id IN (...)` (which cannot
itself signal a per-row "error", only omit missing rows, handled by step 4's fallback) and
`token_count()` calls (which raise on genuine model failure — not caught here, left to propagate,
matching A1/A2/A3's own precedent of never swallowing a real exception silently, per this
workspace's Coding Standards rule 15).

## 4. `tests/test_context_budget_service.py` (new file)

Follow `tests/test_conflict_set_service.py`/`tests/test_lineage_assembly_service.py`'s exact fixture
conventions: `init_db` + real SQLite (no mocks for the database), the same `_memory_id`/`_memory`
helper set (copy them, do not import across test files, per established precedent). Build
`expansion_result`/`conflict_sets_result`-shaped dicts either by calling `expand_context_candidates`/
`assemble_conflict_sets` for real against fixtures, or by constructing the minimal dict literal
directly, mirroring A2/A3's own test-file convention. **Do not mock `get_model()` or
`token_count()`** — the real bundled ONNX tokenizer is already a test dependency for every other
embedding-touching test in this suite (no network call, already-local model); a test relying on
"roughly N tokens for this string" should assert bounds/relative comparisons (e.g. "string A costs
more tokens than string B", "an empty-content id costs exactly 0") rather than hardcoding an exact
token count that could silently drift if the bundled tokenizer is ever swapped.

Required test scenarios (write test-first, red before green, per this workspace's `tdd` skill and
Coding Standards rule 19):

1. **All three inputs empty**: `pack_context_budget({"expansion_candidates": []}, [], {"conflict_sets": []})`
   returns the step-2 zero-result shape without opening a connection (pass no `db_connection`/
   `db_path` and assert no exception raised), with `budget.limit == CONTEXT_BUDGET_DEFAULT_TOKENS`.
2. **Everything fits under budget**: a handful of small-content primary hits and expansion
   candidates, total tokens well under `effective_budget`. Assert every id appears in
   `packed_entity_ids`, both dropped lists are empty, `primary_truncated`/`expansion_truncated` are
   both `False`, `budget.used` equals the sum of their individually-verified `token_counts` entries.
3. **Primary hits alone exceed the budget**: construct primary-hit content large enough that not all
   primary hits fit (use a tiny explicit `budget_tokens` override, not the real default, to keep the
   fixture small). Assert some primary ids land in `dropped_entity_ids["primary"]`,
   `primary_truncated is True`, and — proving §1 decision 6 (primary not exempt) — that expansion
   candidates present in the same call are evaluated regardless (assert at least one expansion id's
   fate, packed or dropped, is determined correctly by the *remaining* budget after primary hits
   consumed their share).
4. **First-fit continues past a miss (proving §1 decision 7, no early exit)**: construct an ordered
   list where an early, large candidate doesn't fit, followed by a later, small candidate that does
   fit in the remainder. Assert the small later candidate IS packed despite the earlier miss — this
   is the scenario that would fail under a naive `break`-on-first-miss implementation.
5. **Conflict-only members always included, never checked against budget**: set `budget_tokens` to a
   value too small to admit even one primary/expansion candidate, and include a `conflict_sets_result`
   with a large-content `conflict_only` member. Assert that member's id appears in
   `conflict_only_entity_ids` regardless, its tokens are reflected in
   `budget.conflict_reserve_tokens_used`, and `budget.used` (the primary/expansion running total)
   does NOT include its cost.
6. **`budget_tokens` clamped down to the ceiling**: call with `budget_tokens` far above
   `CONTEXT_BUDGET_MAX_TOKENS`. Assert `budget.limit == CONTEXT_BUDGET_MAX_TOKENS`, not the
   caller-supplied value.
7. **`budget_tokens` below the ceiling is honored exactly**: call with an explicit small
   `budget_tokens` well under the ceiling. Assert `budget.limit` equals exactly that value (not
   clamped, not replaced by the default).
8. **`budget_tokens=None` resolves to the default**: assert `budget.limit ==
   CONTEXT_BUDGET_DEFAULT_TOKENS` when the parameter is omitted entirely.
9. **Missing entity row falls back to empty content, zero tokens, no crash**: include an id in
   `primary_hits` that has no corresponding `entities` row. Assert `token_counts[that_id] == 0`, the
   id is packed (an empty/zero-cost candidate always fits), and no exception is raised — mirrors A2's
   own `test_missing_entity_row_falls_back_to_unknown` precedent for the same defensive posture. **Per
   Amendment 1**, this `0` is the explicit empty-text normalization, not a coincidental raw-tokenizer
   result — do not assert this by mocking or stubbing `token_count`; the real tokenizer must actually
   be bypassed by the `text == ""` check in the implementation, not merely happen to agree with it.
10. **No overlap assumption exercised, not re-verified**: a realistic multi-pool scenario (some
    primary hits, some expansion candidates, some conflict-only members, all distinct real ids from
    fixtures built via the actual `expand_context_candidates`/`assemble_conflict_sets` calls, not
    hand-typed dicts) runs through `pack_context_budget` end-to-end without needing any
    deduplication logic in the function itself — confirms §1 decision 10's reliance on upstream
    guarantees holds in an integration-shaped test, not just isolated unit fixtures.

Cross-check before finalizing the test file: every one of §1's ten locked decisions above must
correspond to at least one scenario in this list (decisions 1-2 underlie every scenario's basic
setup; decisions 3-10 each has its own dedicated scenario above, decision 9 exercised inline within
whichever scenarios happen to include a missing row).

## 5. Out of scope

- Anything in `src/saltmdb/domain/services/context_expansion_service.py`,
  `conflict_set_service.py`, or `lineage_assembly_service.py`, or their respective test files — A1's,
  A2's, and A3's outputs are consumed as data only; this slice calls none of their functions.
- The `retrieve_context` MCP tool itself, its request-parameter signature (including whether/how a
  caller-supplied `budget_tokens` is exposed at the MCP layer), or any wiring into
  `mcp/tools.py`/`daemon/dispatch.py`/`daemon/protocol.py` — slice A5.
- Assembling the final `memories[]` JSON envelope (title, memory_type, inclusion tag,
  retrieval_provenance nesting) — slice A5. This slice returns entity-id lists and a token-accounting
  dict; A5 is responsible for joining those against the fuller per-memory metadata needed for the
  actual result shape.
- Any caching or reuse of `token_counts` across separate `retrieve_context` calls — every call
  recomputes from scratch; no per-process or persistent token-count cache is introduced.
- Recalibrating `CONTEXT_BUDGET_DEFAULT_TOKENS`/`CONTEXT_BUDGET_MAX_TOKENS`'s numeric values —
  explicitly deferred to Milestone B (§1 decision 3, §2's placeholder framing) — do not tune these
  without also updating the comment framing, matching `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`'s own
  precedent.
- Any type-weighted or predicate-weighted budget discount (§1 decision 1) — foreclosed for Milestone
  A, matching G3's already-locked binary-allowlist-only stance.
- A shared or backtracking budget pool between primary/expansion and the conflict-set reserve (§1
  decision 5) — explicitly rejected by zbalint during G8's resolution; not reopened here.

## 6. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_context_budget_service.py -v
```
must exit 0, and every scenario in §4 must correspond to at least one passing test (a reviewer
checks this by name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite — this slice adds a new file and two new
constants only, on top of A1+A2+A2-Amendment-1+A3's already-verified-green changes; the full suite
was independently re-confirmed green — 1596 passed, 18 subtests — on this worktree's own branch
point immediately after the A1-A3 merge into `context-aware-search`, before this spec was written).

```bash
uv run ruff check src/saltmdb/domain/services/context_budget_service.py tests/test_context_budget_service.py && \
uv run ruff format --check src/saltmdb/domain/services/context_budget_service.py tests/test_context_budget_service.py && \
uv run mypy src/saltmdb/domain/services/context_budget_service.py
```
must exit 0, matching this repo's documented lint/type gate (`CONTRIBUTING.md`).

## Amendment 1 — Explicit empty-text-to-zero-tokens normalization (§3 steps 4-5, §4 scenario 9)

**Adjudicated decision (2026-09-12, with zbalint, in response to OMP's `BLOCKED — SPEC
ADJUDICATION REQUIRED` report on agent session `01a096f0-0e90-7444-9ecc-d72f1cfe8c1f`)**: §3 step
4's original claim — "token count of an empty string is `0`" — was an unverified assumption baked
into the locked spec text, not a grounded finding like §0's two verified `fastembed` facts. OMP's
worker ran the missing check and found the installed tokenizer returns `token_count("") == 2`
(BOS/EOS special-token overhead the tokenizer adds to every input, empty or not), directly
contradicting §3 step 5's original unconditional `model.token_count(...)` assignment and §4
scenario 9's `token_counts[that_id] == 0` requirement. The worker's own uncommitted patch
(`0 if text == "" else count`) independently arrived at the same fix proposed below, but was
correctly left uncommitted pending adjudication rather than assumed authorized.

**Contract chosen: semantic empty-content accounting.** Any candidate whose resolved text is `""`
— whether from a missing `entities` row's `content_by_id.get(entity_id, "")` fallback, or from a
real row whose `full_content` column is itself stored as an empty string — is defined as costing
exactly `0` tokens. This normalization explicitly overrides the raw tokenizer's return value for
that one case; every non-empty text is still tokenized via the real, unbatched
`model.token_count(text)` call exactly as already locked, with no other special-casing.

**Why this contract over the alternative** (preserving the literal unconditional tokenizer call
and amending §4 scenario 9 to expect `2` instead): §1 decision 1's "no predicate/type-based
discount" principle is about not hand-tuning the budget by content *category* — it does not
require treating a tokenizer's BOS/EOS wrapper overhead on a genuinely empty string as real
content size. The missing-row fallback path exists specifically to degrade gracefully (mirrors
A2's own `test_missing_entity_row_falls_back_to_unknown` precedent: a stale/absent reference
should cost nothing, not accrue a tokenizer implementation artifact). The normalization is a single
uniformly-applied rule keyed only on the resolved text being empty, not on *why* it's empty or
*which* pool the candidate came from — it does not reopen the door §1 decision 1 was closing.

**Changes made by this amendment** (all other sections and all other locked decisions in §1
unchanged):

1. §3 step 4: corrected the false "token count of an empty string is `0`" claim to state the real
   installed tokenizer's actual nonzero return for `""`, and to point forward to this amendment's
   explicit normalization as the reason step 5 no longer performs a bare direct assignment.
2. §3 step 5: replaced the unconditional `token_counts[entity_id] = model.token_count(...)`
   assignment with:
   ```python
   from saltmdb.domain.services.embedding_service import get_model
   model = get_model()
   for entity_id in all_ids:
       text = content_by_id.get(entity_id, "")
       token_counts[entity_id] = 0 if text == "" else model.token_count(text)
   ```
   Still exactly one call site per distinct id (the `token_count` call itself remains unbatched,
   per §0 finding 2 — this amendment changes what gets fed into it, not the one-call-per-id shape).
3. §4 scenario 9: added a note that the asserted `token_counts[that_id] == 0` is this amendment's
   explicit normalization, not a coincidental raw-tokenizer result — implementers/reviewers must
   not satisfy this scenario by mocking or stubbing `token_count` to return `0`; the real bundled
   tokenizer must still be called for every non-empty text elsewhere in the same test file (per
   §4's existing "do not mock `get_model()` or `token_count()`" rule, unchanged).

**New scenario implied, not yet in §4's numbered list**: a real `entities` row whose `full_content`
is itself an empty string (distinct from scenario 9's *missing* row) must also normalize to `0`
tokens under this amendment's contract — the implementer should add this as an eleventh scenario
(or fold it into scenario 9 as a second sub-case within the same test) rather than leaving it
untested; the amendment's contract explicitly covers this case even though the original scenario 9
prose only exercised the missing-row path.

**Pre-lock re-check of this amendment against the spec-writing skill's gate**: the amended step 5
code was traced against scenario 2 (everything fits) and scenario 3 (primary hits alone exceed
budget) — both already assume non-empty fixture content, so `text == ""` is never true for those
paths and the amendment changes nothing about their expected outcomes. Scenario 5 (conflict-only
large-content member) and scenario 6/7/8 (budget clamping) are likewise unaffected — none of them
depend on empty content. No other section references `token_count` or the empty-string case, so no
further amendment is needed elsewhere in the document.
