# SPEC-CONTEXT-RETRIEVAL-A3-LINEAGE-ASSEMBLY

## 0. Status

**LOCKED**

**Scope**: may create `src/saltmdb/domain/services/lineage_assembly_service.py` (new file) and
`tests/test_lineage_assembly_service.py` (new file); may edit `src/saltmdb/config.py` (add exactly
one new constant, `LINEAGE_HISTORICAL_CAP`, per §2 — no other edit to that file). Depends on, and
must be implemented **after**, this same worktree's `SPEC-CONTEXT-RETRIEVAL-A2-CONFLICT-SET-ASSEMBLY.md`
**Amendment 1** (extracts `classify_contradicts_components` and fixes its merge-diamond
connectivity blind spot) — that amendment must land first; this slice imports the extracted
function by name. Does not touch: `src/saltmdb/domain/services/context_expansion_service.py` and
`tests/test_context_expansion_service.py` (A1's output is consumed as data only — no signature
change, no call into it); `src/saltmdb/domain/services/conflict_set_service.py` beyond what
Amendment 1 already specifies (this slice calls `classify_contradicts_components`, already public
after that amendment — it does not modify that file further); `src/saltmdb/domain/services/relation_service.py`
(reused as-is via `get_lineage` — no signature change, no new parameter; re-read in full before
locking this spec, confirms `_lineage_node`'s node shape — `id, title, status, owner_id,
updated_at, depth, generation_depth` — is exactly what §3 relies on, and that `get_lineage`'s
`nodes` list always includes the root itself at depth 0); `src/saltmdb/domain/services/memory_service/ranking.py`
(only `SUPERSESSION_CHAIN_MAX_DEPTH` is imported, re-confirmed unchanged at value 10); `src/saltmdb/mcp/tools.py`
/ `src/saltmdb/daemon/dispatch.py` / `src/saltmdb/daemon/protocol.py` (no MCP tool surface in this
slice — Milestone A slice A5); `src/saltmdb/utils/predicate_vocabulary.py`; and `src/saltmdb/db/schema.py`
(no schema change — re-read in full, confirms `entities` has no `archived_at` column; `archive_memory`
stamps `updated_at` at archive time, which is what this slice's `archived_at` field is sourced from).

**Pre-lock gate completed against the current tree** (spec-writing skill, all 9 steps): every
section below was drafted in Why → mechanical → Out of scope → Acceptance order before this Status
section was finalized. Acceptance's commands were checked against the actual tree state present in
this worktree after Amendment 1 is applied (not before) — `classify_contradicts_components` is
addressed as a function that Amendment 1 introduces, not one this spec re-derives or duplicates.
Grepped `LINEAGE_HISTORICAL_CAP`, `lineage_assembly_service`, `assemble_lineage`, and
`included_via` across the tree: zero prior occurrences of any of them, confirming none collide with
existing code or stale test literals. §3's algorithm prose and its "exact keys, exact types"
Output contract block were diffed against each other line-by-line — the `included_via` values,
the `historical_dropped_count` definition (ancestors genuinely absent, not merely "beyond the
cap"), and the `current`-vs-`historical[i]` flag-sibling shape all appear identically in both
places (this is the exact failure shape A1's own Amendment 1 caught, checked here before locking).
A concrete worked instance of every one of §4's nine required scenarios was traced by hand through
§3's algorithm before finalizing §4's list, including the two hardest ones: the 4cbf26ac
force-include case (traced through steps 5f-5j to confirm an ancestor beyond the cap is pulled back
in, marked distinctly, with its contradiction fact intact) and the resolved-vs-unresolved gating
case (traced through step 4 to confirm an unresolved component's `contradiction_peers` entries are
never populated at all, so no downstream step can accidentally flag them — the mutual-exclusivity
guarantee holds by construction, not by a second independent check that could drift).

## 1. Why

This is slice **A3** of Milestone A ("local graph-aware context retrieval," the `retrieve_context`
MCP tool) from the `wayfinder:saltmdb:graph-aware-context-retrieval` roadmap, per the 5-slice
breakdown locked in memory `bed2478c-9c3a-45be-a446-ee090f5a28dc`. Slices A1 (predicate-allowlist +
fan-out-cap expansion engine) and A2 (contradiction/conflict-set assembly) are done; A2's own
Amendment 1 (this worktree, `SPEC-CONTEXT-RETRIEVAL-A2-CONFLICT-SET-ASSEMBLY.md`) lands immediately
before this slice, as a direct prerequisite, not a separate future dependency.

A3 implements **lineage assembly with contradiction-flag propagation** — standing constraints
13-refinement/14/15 from the wayfinder map: for each memory `retrieve_context` surfaces (a primary
hit or an A1 expansion candidate), build a `lineage[head]` entry describing its own supersession
history (`current` + `historical`, most-recent-first, capped), and, for a lifecycle-resolved
`contradicts` pair, preserve the fact that the two sides once explicitly disagreed via per-entry
`was_flagged_contradiction`/`contradicted_with` fields — exact-edge-only, never transitive.

**This slice's design went through a full grilling round with zbalint this session** (memories
`bed2478c`, `0cb1d191`, `4cbf26ac`, `74f6b4c0`, `482c8986`, plus the live conversation that produced
this spec) that materially corrected the slice's originally-assumed shape. Recorded here so a
future reader doesn't have to reconstruct it from chat history:

- **A3 has no data dependency on A2's own output.** `bed2478c`'s original slice description said
  "depends on A2 (needs contradicts-edge/conflict-set info)" — investigating this against A2's
  actual shipped contract showed that's not true: G7's policy already routes a lifecycle-resolved
  conflict through `lineage` exclusively and an unresolved one through `conflict_sets` exclusively,
  and A1's own grounding fact (every `contradicts_edges` entry has ≥1 primary-hit endpoint) means
  this slice can classify contradictions itself from A1's raw output. What A3 actually depends on
  is a **code** dependency: A2's own classification logic, needed so the two slices can never
  disagree about which components are resolved. Rather than duplicating that logic (a Coding
  Standards rule 16 violation) or leaving A3 to guess with a simplified, potentially-divergent
  local check, A2's Amendment 1 (this same worktree) extracts it into a shared, public function,
  `classify_contradicts_components`, that this slice imports and calls directly.
- **A standing rule was set mid-session** (memory `74f6b4c0`): *"we do not accept anything as
  limitation as long we can implement or fix it and does not require anything beyond our
  jurisdiction."* Two design gaps that would otherwise have been documented as accepted
  limitations are instead fully closed by this spec: the wayfinder gap `4cbf26ac` (both
  `contradicts`-edge endpoints falling outside the history window, previously accepted as a known
  edge case) is fixed via force-inclusion (§3 step 5g); and a same-depth ancestor ordering
  ambiguity under a `consolidated_from` merge (surfaced during this session's own self-critique,
  memory `482c8986`) is fixed via an explicit tiebreak (§3 step 5e), not documented as fog.
- **A cleaner design emerged during drafting than what was verbally agreed mid-grilling.** The
  live conversation settled on A3 calling `get_lineage` in *both* directions (ancestors and
  descendants) per head-candidate, to catch a contradiction pointing the "wrong way" relative to an
  archived head-candidate. Working through the concrete algorithm for this spec showed that
  concern is already fully subsumed by `classify_contradicts_components`'s own (now bidirectional,
  post-Amendment-1) connectivity check: flagging is a lookup keyed by raw entity id against a
  globally-built `contradiction_peers` map (§3 step 4), entirely independent of which direction, if
  any, connects that id to the head being rendered. A3 therefore only ever calls
  `get_lineage(..., direction="ancestors")` — one call per head-candidate, not two — which is
  simpler, cheaper, and still fully correct. This is a refinement of what was verbally agreed, not
  a reduction in scope: every locked answer from the grilling round (Q1, Q3, Q4, Q5, Q6, Q7, Q8,
  and the `4cbf26ac` fix) is preserved exactly; only the *mechanism* for Q2/Q4 got simpler once
  written out precisely.

**Codebase reuse, grounded against actual current source, not roadmap prose:**

- `get_lineage(entity_id, direction, max_depth, point_in_time, db_connection)`
  (`relation_service.py:783-934`) is reused exactly as-is. Its `nodes` list always includes the
  root itself (at `depth: 0`), sorted `(depth, id)` ascending; its `root` field is the same
  `_lineage_node` shape as every other entry (`id, title, status, owner_id, updated_at, depth,
  generation_depth`) — this slice reuses `root` directly for `current`'s `id`/`title`, issuing no
  extra query. There is no `archived_at` column anywhere in `db/schema.py`'s `entities` table;
  `archive_memory` (`memory_service/lifecycle.py`) stamps `updated_at` at archive time, so this
  slice's `historical[i].archived_at` is sourced from each ancestor node's own `updated_at` field.
- `classify_contradicts_components` (new public function, `conflict_set_service.py`, introduced by
  this worktree's A2 Amendment 1) is this slice's other required import — see that amendment for
  its exact contract (`list[{"member_ids": set[str], "edges": list[dict], "resolved": bool}]`).
- `SUPERSESSION_CHAIN_MAX_DEPTH` (`config.py`, value 10) is reused as `get_lineage`'s `max_depth`
  bound for this slice's own ancestor traversal — no new depth constant.
- `expand_context_candidates`'s (A1) output — `expansion_candidates` (each entry's `entity_id`) and
  `contradicts_edges` (raw `{relation_id, source_id, target_id, predicate}` dicts) — is consumed as
  a plain data structure; this slice calls no function from `context_expansion_service.py`.

**Locked design decisions** (all resolved via this session's grilling round; restated here to the
depth needed to implement, full rationale in the memories cited above):

1. **Head-candidate population = every distinct id in `primary_hits` ∪ A1's `expansion_candidates`,
   used literally as given.** No resolution-up to some other "true current head" — if a given id is
   itself an older, superseded (and typically archived) entity surfaced because `search_memory` was
   called in a history-preserving mode, its `lineage[id]` entry simply documents *that id's own*
   ancestor chain. Building a "resolve arbitrary id to its live head" step would duplicate
   `_resolve_supersession_chains`-shaped logic (already rejected once by A2 for the same reason) and
   would silently change which id anchors the lineage view.
2. **`historical` is ancestors-only.** `get_lineage(id, direction="ancestors", ...)` is the only
   traversal call this slice makes per head-candidate (see the "cleaner design" note above for why
   a separate `descendants` call turned out to be unnecessary).
3. **This function's own inputs are `expansion_result` (A1's raw output) and `primary_hits` only —
   never A2's `conflict_sets`/`contradicts_cap`.** It does import and call
   `classify_contradicts_components` (a code dependency on the now-shared classification function,
   not a data dependency on A2's own result).
4. **Flagging is gated by the real, shared resolved/unresolved classification — never a locally
   re-derived guess.** A component `classify_contradicts_components` marks `resolved: False` can
   never contribute an entry to `contradiction_peers` (§3 step 4), so it can never be flagged here —
   guaranteeing, by construction, that no component `conflict_sets` reports can also be flagged in
   `lineage`.
5. **`archived_at` is sourced from `get_lineage`'s node `updated_at` field** — there is no real
   `archived_at` column.
6. **`lineage[head]` is emitted only when a head-candidate has at least one ancestor** (i.e.
   `historical` would be non-empty before any cap is applied) — a head-candidate with no
   supersession history at all gets no `lineage` key.
7. **A `get_lineage` error for a head-candidate is non-fatal**: log `logger.warning` naming the id
   and the error, omit that head's `lineage` entry, continue with the rest of the call.
8. **Same-depth ancestor ties (a `consolidated_from` merge fan-in) are broken by `updated_at`
   descending, then `id` ascending, before the cap is applied** — closes the ordering ambiguity
   `(depth, id)` alone would leave under a merge topology, fixed rather than documented (memory
   `482c8986`).
9. **The wayfinder gap `4cbf26ac` (both `contradicts`-edge endpoints falling outside the history
   window, total signal loss) is fixed via force-inclusion**, not accepted as a limitation: an
   ancestor beyond the normal cap-of-5 is still included in `historical` if it is itself a resolved
   `contradicts`-edge endpoint, marked distinctly (`included_via`) so a consumer can tell why it's
   outside the normal recency window.

## 2. `src/saltmdb/config.py`

Already applied in this worktree ahead of this spec being written (verified present, exact text
below) — listed here for completeness and for OMP's own re-verification, not as a pending edit:

```python
# Milestone A slice A3 (wayfinder standing constraint 15, memory 0ca97ddc) -- retrieve_context's
# lineage.historical display cap: the number of most-recent supersession-chain entries shown by
# default per lineage[head]. Deliberately decoupled from get_lineage's own general-purpose
# max_depth=10 traversal bound above (a different, downstream concern -- display size, not
# traversal depth). UNLIKE CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS/CONTEXT_EXPANSION_CONTRADICTS_CAP,
# this is NOT a Milestone-B placeholder -- constraint 15 already fixed this number now (getting it
# wrong is low-stakes and reversible, one extra get_lineage call recovers the full chain), so do
# not add placeholder framing here or flag it for recalibration. An ancestor beyond this cap can
# still appear in lineage[head].historical when it is force-included as a resolved contradicts-edge
# endpoint (Milestone A slice A3, wayfinder gap 4cbf26ac) -- this constant bounds only the normal,
# non-force-included window.
LINEAGE_HISTORICAL_CAP = 5
```

Immediately after `SUPERSESSION_CHAIN_MAX_DEPTH = 10`, before the `# NOTE: accept_or_abstain's...`
comment block. If OMP finds this constant already present with this exact text when starting
implementation, that is expected (it was applied while drafting this spec) — do not re-add it or
treat its presence as a merge conflict.

## 3. New file: `src/saltmdb/domain/services/lineage_assembly_service.py`

A new domain-service module, sibling to `context_expansion_service.py` and `conflict_set_service.py`,
following the same conventions: module-level `logger = logging.getLogger(__name__)`, the same
`db_connection=None, db_path: str | None = None` open-or-reuse-connection pattern.

### 3.1 `assemble_lineage` — the slice's one public function

```python
def assemble_lineage(
    expansion_result: dict,
    primary_hits: list[dict],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, dict]:
```

**Input contract**: `expansion_result` is A1's `expand_context_candidates` return dict, used
literally — `expansion_result["expansion_candidates"]` (each entry's `"entity_id"`) and
`expansion_result["contradicts_edges"]` (passed straight through to
`classify_contradicts_components`). `primary_hits` is the same `list[{"id": str, "score": float}]`
A1 and A2 both receive for this call — only `"id"` is used here (no score-based logic in this
slice). **Caller contract**: `point_in_time` MUST be the identical value passed to
`expand_context_candidates`/`assemble_conflict_sets` for this same `retrieve_context` call (or, if
none was passed to any of them, each resolves its own `datetime.now(UTC).isoformat()` close enough
in time that this is a non-issue in practice) — mirrors A1/A2's own single-shared-`pit`-per-call
discipline exactly.

**Algorithm** (implement exactly this sequence):

1. `head_candidate_ids: set[str] = {h["id"] for h in primary_hits} | {c["entity_id"] for c in
   expansion_result["expansion_candidates"]}`. If empty, return `{}` immediately — do not open a
   connection for this case.
2. Open one connection (`db_connection` if given, else `get_connection(db_path or get_db_path())`,
   closing it at the end only if this function opened it) — mirror A1/A2's `should_close` pattern
   exactly. Resolve `pit = point_in_time or datetime.now(UTC).isoformat()`.
3. Call `classify_contradicts_components(expansion_result["contradicts_edges"], point_in_time=pit,
   db_connection=conn)` **once** for the whole call (not once per head). Keep only entries where
   `resolved is True`.
4. Build `contradiction_peers: dict[str, set[str]]` from the kept (resolved) components' `edges`
   only: for each edge `{source_id, target_id, ...}` in each resolved component's `edges` list,
   `contradiction_peers.setdefault(source_id, set()).add(target_id)` and the symmetric
   `contradiction_peers.setdefault(target_id, set()).add(source_id)`. An unresolved component's
   edges never enter this map — this is the mechanism that guarantees an entry `conflict_sets`
   reports can never also be flagged here (§1 decision 4).
5. For each `head_id` in `sorted(head_candidate_ids)` (sorted for deterministic output and
   deterministic test assertions — the returned dict's own key order carries no contract meaning
   beyond that):
   a. `ancestors_result = get_lineage(head_id, direction="ancestors",
      max_depth=SUPERSESSION_CHAIN_MAX_DEPTH, point_in_time=pit, db_connection=conn)`.
   b. If `"error" in ancestors_result`: `logger.warning` naming `head_id` and the error; skip to the
      next head-candidate (no key for this `head_id` in the returned dict).
   c. `full_ancestor_nodes = [n for n in ancestors_result["nodes"] if n["id"] != head_id]` (excludes
      the root, which `get_lineage` always includes in `nodes` at `depth: 0`).
   d. If `full_ancestor_nodes` is empty: skip to the next head-candidate (§1 decision 6 — no
      `lineage[head_id]` key at all).
   e. Sort `full_ancestor_nodes` by `(depth asc, updated_at desc, id asc)` — the `updated_at`
      tiebreak is §1 decision 8's fix for a same-depth `consolidated_from` merge fan-in; plain
      `(depth, id)` alone (the ordering `get_lineage`'s own `nodes` list ships with) is not
      sufficient and must be re-sorted here, not assumed already correct for this purpose.
   f. `recency_kept = full_ancestor_nodes[:LINEAGE_HISTORICAL_CAP]`;
      `recency_kept_ids = {n["id"] for n in recency_kept}`.
   g. `force_included = [n for n in full_ancestor_nodes if n["id"] not in recency_kept_ids and
      contradiction_peers.get(n["id"])]` — §1 decision 9's fix for `4cbf26ac`: an ancestor beyond
      the normal window is pulled back in solely because it is itself a resolved contradicts-edge
      endpoint, regardless of whether its peer is separately visible anywhere else in this output.
   h. `final_nodes = sorted(recency_kept + force_included, key=<same (depth, updated_at, id) order
      as step e>)`.
   i. `historical_dropped_count = len(full_ancestor_nodes) - len(final_nodes)`;
      `historical_truncated = historical_dropped_count > 0`.
   j. Build each `historical` entry from `final_nodes`:
      `{"id": node["id"], "title": node["title"], "archived_at": node["updated_at"],
      "included_via": "recency" if node["id"] in recency_kept_ids else
      "contradicts_force_include"}` — then, if `contradiction_peers.get(node["id"])` is non-empty,
      add `"was_flagged_contradiction": True, "contradicted_with": sorted(contradiction_peers[node["id"]])`
      as additional sibling keys on that same dict.
   k. Build `current = {"id": head_id, "title": ancestors_result["root"]["title"]}` — then, if
      `contradiction_peers.get(head_id)` is non-empty, add the same two sibling keys as in step j.
   l. `output[head_id] = {"current": current, "historical": final_historical_list,
      "historical_truncated": historical_truncated, "historical_dropped_count":
      historical_dropped_count}`.
6. Return `output`.

This function raises nothing itself for resolvable/valid input; the only defensive path (a
head-candidate's `get_lineage` call returning an `"error"` key) is handled per step 5b (logged and
skipped, never raised).

**Output contract** (exact keys, exact types):

```python
{
    "<head_id>": {
        "current": {
            "id": str,
            "title": str,
            "was_flagged_contradiction": True,      # only present when flagged
            "contradicted_with": [str, ...],        # only present when flagged; sorted, deduped
        },
        "historical": [
            {
                "id": str,
                "title": str,
                "archived_at": str | None,           # sourced from get_lineage's node updated_at
                "included_via": "recency" | "contradicts_force_include",
                "was_flagged_contradiction": True,   # only present when flagged
                "contradicted_with": [str, ...],     # only present when flagged; sorted, deduped
            },
            ...  # sorted (depth asc, updated_at desc, id asc); length may exceed
                 # LINEAGE_HISTORICAL_CAP when force-included entries are present
        ],
        "historical_truncated": bool,
        "historical_dropped_count": int,
    },
    ...  # only for head_ids that had at least one ancestor (§1 decision 6)
}
```

`was_flagged_contradiction`/`contradicted_with` are omitted entirely (not present with a `False`/
empty value) on an entry that was not flagged — matches this project's existing sparse-field
convention for optional per-entry signals (e.g. A2's own `title`/`status: None` treatment is the
nearest precedent for "field presence carries meaning," though here the fields are omitted rather
than null-valued; both are legitimate per-field choices already used elsewhere in this codebase,
and this slice's own three example scenarios in the prototype at `src/saltmdb/mcp/prototype_retrieve_context.html`
already use the omitted-when-absent convention for these exact two fields — matching that, not the
null-valued alternative).

## 4. `tests/test_lineage_assembly_service.py` (new file)

Follow `tests/test_conflict_set_service.py`'s exact fixture conventions: `init_db` + real SQLite (no
mocks), the same `_memory_id`/`_memory`/`_relation`/`_raw_relation`/`_archive` helper set (copy them,
do not import across test files, per that file's own established precedent). Build
`expansion_result`-shaped dicts by hand in each test (either call `expand_context_candidates` for
real against the same fixtures, or construct the minimal dict literal directly) rather than mocking
it, mirroring A2's own test-file convention.

Required test scenarios (write test-first, red before green, per this workspace's `tdd` skill and
Coding Standards rule 19):

1. **No head-candidates at all**: `assemble_lineage({"expansion_candidates": [], "contradicts_edges":
   []}, [])` returns `{}` without opening a connection (pass no `db_connection`/`db_path` and assert
   no exception raised).
2. **Head-candidate with no supersession history**: a primary hit with zero `revises`/`supersedes`/
   `consolidated_from` edges anywhere. Assert the returned dict has no key for that id at all (§1
   decision 6).
3. **Plain chain under the cap**: a head with 3 ancestors (fewer than `LINEAGE_HISTORICAL_CAP`).
   Assert all 3 appear in `historical`, each `included_via == "recency"`, `historical_truncated is
   False`, `historical_dropped_count == 0`, ordered most-recent-first.
4. **Chain over the cap, normal truncation**: a head with 8 ancestors, none involved in any
   `contradicts` edge. Assert exactly the 5 most recent appear (all `included_via == "recency"`),
   `historical_truncated is True`, `historical_dropped_count == 3`.
5. **Resolved contradiction flags `current`**: `current` contradicts one of its own ancestors; that
   pair's supersession chain makes the component lifecycle-resolved (mirror A2's own resolved-scenario
   fixture shape — one non-archived member, connectivity confirmed). Assert `current` carries
   `was_flagged_contradiction: True` and `contradicted_with` naming the ancestor's id, and that same
   ancestor's own `historical[i]` entry (if within the normal cap) does NOT redundantly carry the
   flag unless it is itself also a distinct edge endpoint of another resolved pair (exact-edge
   semantics — an ancestor is flagged only if IT is literally an edge endpoint, which in this
   scenario it also is, so assert it DOES carry the flag too, naming `current`'s id back).
6. **Resolved contradiction flags a `historical[i]` entry only**: two ancestors (not `current`)
   contradict each other, both part of the same resolved chain. Assert neither `current` nor any
   *other* historical entry is flagged, and the two specific ancestors are each flagged naming the
   other.
7. **Force-include case, proving the `4cbf26ac` fix**: construct a chain 8+ deep where a resolved
   contradicts pair sits entirely beyond the normal cap-of-5 window (e.g. positions 6 and 7, neither
   `current` nor within the top 5 by recency). Assert BOTH ancestors appear in the final `historical`
   list (total length exceeding 5), each marked `included_via == "contradicts_force_include"`, each
   carrying `was_flagged_contradiction`/`contradicted_with` naming the other — the contradiction
   fact is fully visible, not merely referenced by an entry that isn't there. Assert
   `historical_dropped_count` reflects only the ancestors genuinely absent (not the two
   force-included ones).
8. **Unresolved contradiction is never flagged in lineage**: two chain members contradict each other
   but the component is classified unresolved by `classify_contradicts_components` (construct via
   the same fixture shape A2's own test scenario 5 or 6 uses — e.g. both members archived, or both
   live). Assert neither entry is flagged anywhere in this call's output — proving the shared
   classifier's gating actually prevents a `conflict_sets`/`lineage` double-signal, not merely
   documents the intent.
9. **Same-depth tiebreak, proving the Q8 fix**: construct a `consolidated_from` merge where two
   distinct ancestors land at the same `depth` from the head, with distinct, deliberately-out-of-id-order
   `updated_at` values, under a cap tight enough that only one of the two fits (e.g. temporarily
   treat `LINEAGE_HISTORICAL_CAP` as effectively 1 for this scenario by constructing exactly one
   closer, unambiguous ancestor plus the two same-depth tied ones — or patch/import the real
   constant per A2's own scenario-11 precedent rather than hardcoding `5`). Assert the entry with
   the later `updated_at` is the one kept, proving `(depth, id)` alone would have picked the wrong
   one (whichever has the lexicographically smaller id, if that happens to differ from the
   later-`updated_at` one in the constructed fixture — choose fixture ids so this is unambiguous).
10. **`get_lineage` error path is non-fatal**: monkeypatch/stub `get_lineage` for one specific
    head-candidate id to return `{"error": "..."}` (a real fixture can't naturally trigger this,
    mirroring A2's own scenario-15 precedent for the same defensive-only path). Assert that head has
    no key in the output, the call does not raise, and any OTHER head-candidate in the same call
    still resolves normally.

Cross-check before finalizing the test file: every one of §1's nine locked decisions above must
correspond to at least one scenario in this list (decisions 1-3 together with decision 5's "no
descendants call" refinement are exercised implicitly by every scenario using only ancestor
fixtures; decisions 4/6/7/8/9 each has its own dedicated scenario above).

## 5. Out of scope

- Anything in `src/saltmdb/domain/services/context_expansion_service.py` or
  `tests/test_context_expansion_service.py` — A1's output is consumed as data only.
- Any part of `src/saltmdb/domain/services/conflict_set_service.py` or
  `tests/test_conflict_set_service.py` beyond what A2's own Amendment 1 already specifies — this
  slice calls `classify_contradicts_components` as a black box; it does not modify A2's ranking,
  cap, or output-shaping logic for unresolved sets in any way.
- Any modification to `get_lineage`, `analyze_lineage`, `_lineage_node`, `analyze_dependencies`, or
  any other existing `relation_service.py` function.
- The `retrieve_context` MCP tool itself, its request-parameter signature, or any wiring into
  `mcp/tools.py`/`daemon/dispatch.py`/`daemon/protocol.py` — slice A5.
- Context-token-budget packing (`primary_truncated`/`expansion_truncated`, fastembed token
  counting) — slice A4. This slice's `lineage` output is not part of A4's budget-competing
  `memories[]` pool at all (matches the prototype's own finding that lineage entries are
  lightweight and separate from the budget pass).
- Recalibrating `LINEAGE_HISTORICAL_CAP`'s numeric value — explicitly **not** deferred to
  Milestone B (§1 decision block, constraint 15 already fixed this number now; do not add
  placeholder framing to it or flag it for future recalibration the way
  `CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS`/`CONTEXT_EXPANSION_CONTRADICTS_CAP` are).
- Any caller-supplied override of the head-candidate population, the ancestor-only traversal
  direction, the historical cap, or the force-include rule — all foreclosed by §1 for Milestone A.

## 6. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_lineage_assembly_service.py -v
```
must exit 0, and every scenario in §4 must correspond to at least one passing test (a reviewer
checks this by name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/test_conflict_set_service.py -v
```
must exit 0 (16 scenarios: the original 15 plus Amendment 1's new merge-diamond scenario) — this
slice's own correctness depends on Amendment 1 already being in place and green.

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite — this slice adds a new file and one new
constant only, on top of Amendment 1's already-verified-green changes).

```bash
uv run ruff check src/saltmdb/domain/services/lineage_assembly_service.py tests/test_lineage_assembly_service.py && \
uv run ruff format --check src/saltmdb/domain/services/lineage_assembly_service.py tests/test_lineage_assembly_service.py && \
uv run mypy src/saltmdb/domain/services/lineage_assembly_service.py
```
must exit 0, matching this repo's documented lint/type gate (`CONTRIBUTING.md`).
