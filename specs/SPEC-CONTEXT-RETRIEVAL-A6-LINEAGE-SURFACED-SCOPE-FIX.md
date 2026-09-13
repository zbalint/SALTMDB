# SPEC-CONTEXT-RETRIEVAL-A6: Prune `lineage` to Heads Actually Surfaced in `memories[]`

## 1. Why

Milestone A ("local graph-aware context retrieval," the `retrieve_context` MCP tool) shipped and
merged into `context-aware-search` at `45676e5` (memory `693c7ff3`). A live smoke test against the
real corpus on 2026-09-13/14, run directly against the merged tool (not a synthetic fixture),
surfaced a genuine cross-slice gap between A3 (lineage assembly) and A4 (context-budget packing)
that none of A5's five OMP `BLOCKED` adjudication rounds caught, because it's a gap *between* two
already-locked specs rather than a contradiction inside either one.

`retrieve_context`'s own docstring (`src/saltmdb/mcp/tools.py`) states the tool returns "a lineage
map for every **surfaced** head with a known supersession chain." In the current implementation,
`assemble_retrieve_context` (`src/saltmdb/domain/services/retrieve_context_service.py:102-107`)
calls `assemble_lineage(expansion_result, primary_hits, ...)` — per A3's own spec and the wayfinder
map's memory `b9ec8a0e` "CORRECTED" note, `assemble_lineage` depends only on the **full pre-budget**
`expansion_result` (every primary hit plus every A1 expansion candidate that survived the G4
fan-out cap), never on A4's `budget_result`. `assemble_lineage` (`lineage_assembly_service.py:50-53`)
builds its own `head_candidate_ids` from exactly that same full set. The function's return value is
passed straight through to the final response (`retrieve_context_service.py:241`,
`"lineage": lineage_result`) with no filtering against which entities actually survived A4's budget
pass into `memories[]`.

Net effect, reproduced live: a query returned `memories[]` with 8 entries, but `lineage` carried 4
head entries, 3 of which (`77f8dbd4`, `b9ec8a0e`, `c5fb4cc1`) were never in `memories[]` at all —
each pulled in as an A1 expansion candidate reachable from a *different* primary hit that was
itself budget-truncated out of the final result. This is not a hypothetical: A4's
`expansion_dropped_count` for that call was 8, several of which had non-empty supersession chains,
so their heads made it into `lineage` purely because A3 computed them before A4's truncation, with
nothing pruning them back out afterward.

This directly contradicts the tool's own contract ("for every **surfaced** head") and the effort's
governing invariant locked via wayfinder ticket G6: *"every memory `retrieve_context` returns can
explain how it entered the context bundle."* A caller sees a `lineage` entry for an entity it was
never told exists anywhere else in the response — no `retrieval_provenance`, no `memories[]` row,
no explanation. It also silently inflates response payload with content A4's budget pass explicitly
decided not to include.

Confirmed via direct code read (not assumed from prose) that A3's own service function
(`lineage_assembly_service.py`) is intentionally decoupled from budget accounting — its own spec
(`SPEC-CONTEXT-RETRIEVAL-A3-LINEAGE-ASSEMBLY.md:395-398`) states this explicitly: *"This slice's
`lineage` output is not part of A4's budget-competing pool (lineage is deliberately lightweight and
separate from the budget pass)."* That design choice — lineage bytes never compete for token
budget — is correct and stays unchanged by this fix. The bug is narrower: nothing downstream of
that intentional decoupling ever re-joins `lineage`'s key set back against final `memories[]`
membership once both A3 and A4 have independently run. The fix belongs entirely in A5's own
orchestration function (`assemble_retrieve_context`), which is the one place that already knows
both `lineage_result` and the final, fully-reconciled `memories` list (including G7's equal-
visibility conflict-recovery step) in the same scope — not in A3's `assemble_lineage`, which has no
visibility into A4's outcome and should not be given one (that would re-couple budget and lineage
computation, contradicting A3's own already-locked design).

## 2. `src/saltmdb/domain/services/retrieve_context_service.py`

**Exact anchor**: the `memories` list is fully built by line 206 (the last `memories.append(...)`
inside the `for entity_id in budget_result["conflict_only_entity_ids"]:` loop that starts at line
191). Immediately after that loop closes and before line 208 (`edges = expansion_result[...]`),
insert:

```python
        surfaced_ids = {memory["entity_id"] for memory in memories}
        lineage_result = {
            head_id: entry for head_id, entry in lineage_result.items() if head_id in surfaced_ids
        }
```

This computes the filter from `memories`' own final contents — the single already-fully-reconciled
source of truth for "what this response actually surfaced" (it already accounts for primary hits,
survived expansion candidates, *and* G7's `conflict_only` force-includes and equal-visibility
recovery) — rather than re-deriving a second, parallel id-set expression that could drift out of
sync with `memories` construction later. Do not filter inside `assemble_lineage` itself
(`lineage_assembly_service.py`) and do not pass `budget_result` or `memories` into it — A3's
function signature and its independence from A4 stay exactly as locked; this is a pure downstream
narrowing of an already-computed dict, entirely local to A5's orchestration function.

No other line in this file changes. `edges`, `conflict_sets_output`, and `metadata` are unaffected
— none of them derive from or reference `lineage_result`.

## 3. Out of scope

- `src/saltmdb/domain/services/lineage_assembly_service.py` — `assemble_lineage`'s own signature,
  algorithm, and independence from `budget_result` are unchanged. Do not add a `surfaced_ids` or
  `memories` parameter to it.
- `src/saltmdb/domain/services/context_budget_service.py` (A4) — unchanged. This is not a budget
  accounting change; `metadata.budget.*` numbers are unaffected (lineage bytes were never counted
  against the budget before this fix and still aren't).
- `src/saltmdb/mcp/tools.py`'s `retrieve_context` docstring — already accurately describes the
  correct (fixed) behavior ("a lineage map for every surfaced head"); no text change needed since
  the code is what's being brought into line with the doc, not the reverse.
- Re-litigating whether `lineage` should instead be *expanded* to explain orphaned heads (e.g. a
  "would appear with more budget" signal) instead of pruning them — that's a real alternative
  design but a different, larger feature; this spec implements pruning only, matching what the
  tool already documents itself as doing.
- Any change to `conflict_sets_result`, `edges`, or the equal-visibility reconciliation logic
  (`retrieve_context_service.py:109-140`) — untouched, and `surfaced_ids` is computed *after* that
  reconciliation already ran, so recovered conflict members are correctly still counted as
  surfaced.
- `tests/test_lineage_assembly_service.py` — tests `assemble_lineage` directly and is unaffected,
  since that function's own behavior does not change.

## 4. Tests — `tests/test_retrieve_context_service.py`

Add one new test, following the exact fixture/helper conventions already used by
`test_equal_visibility_reconciliation_preserves_unrelated_budget_drop` (lines 369-408) and
`test_three_generation_supersession_chain_appears_in_lineage_only` (lines 269-287) directly above
it in the same file:

```python
def test_budget_dropped_expansion_head_pruned_from_lineage(self):
    primary = self._memory(
        "Lineage prune primary",
        "lineage-prune-query primary content",
    )
    dropped_head = self._memory(
        "Lineage prune dropped expansion",
        "An expansion body deliberately sized to exceed the remaining budget.",
    )
    ancestor = self._memory("Lineage prune ancestor")
    self._relation(primary, dropped_head, "depends_on")
    self._raw_relation(dropped_head, ancestor, "supersedes")
    primary_tokens = self._content_tokens(primary)
    _, expansion_result, packed = self._real_budget_result(
        "lineage-prune-query", primary_tokens
    )
    self.assertIn(
        dropped_head,
        {candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]},
    )
    self.assertIn(dropped_head, packed["dropped_entity_ids"]["expansion"])

    result = self._assemble(
        "lineage-prune-query",
        limit=1,
        budget_tokens=primary_tokens,
    )

    visible_ids = {memory["entity_id"] for memory in result["memories"]}
    self.assertNotIn(dropped_head, visible_ids)
    self.assertNotIn(
        dropped_head,
        result["lineage"],
        "a lineage entry survived for a head that budget truncation dropped from memories[]",
    )
```

This scenario is the minimal real reproduction of the live-observed bug: `dropped_head` is a real
A1 expansion candidate (via `depends_on`) with a real, non-empty supersession chain (via
`supersedes` to `ancestor`), sized so A4's budget pass truncates it out of `memories[]` entirely
(mirroring `ordinary_expansion` in the existing
`test_equal_visibility_reconciliation_preserves_unrelated_budget_drop` fixture, plus a `supersedes`
edge so `assemble_lineage` actually produces a non-empty entry for it pre-fix). Before this fix,
`dropped_head` would appear as a key in `result["lineage"]` despite `assertNotIn(dropped_head,
visible_ids)` passing — this is exactly what the live smoke test observed. After the fix, both
assertions pass together.

Confirm the two adjacent already-existing tests named above are unaffected: in both, `head`/`primary`
is itself a primary search hit under `limit=1` with no competing candidates, so it is always inside
`final_primary_ids` and therefore always in `surfaced_ids` — the new filter is a no-op for those
two tests specifically (`git diff` after the fix must show zero changes to either test's body).

## 5. Acceptance

```
python -m pytest tests/test_retrieve_context_service.py tests/test_retrieve_context_wiring.py tests/test_lineage_assembly_service.py -v
```
Must exit 0, with the new `test_budget_dropped_expansion_head_pruned_from_lineage` present and
passing, and every pre-existing test in all three files passing unchanged (no existing assertion
text may be edited to accommodate this fix — if one needs to change, stop and report which one and
why, rather than editing it).

```
ruff check src/saltmdb/domain/services/retrieve_context_service.py tests/test_retrieve_context_service.py
ruff format --check src/saltmdb/domain/services/retrieve_context_service.py tests/test_retrieve_context_service.py
mypy src/saltmdb/domain/services/retrieve_context_service.py
```
All three must exit 0.

```
git diff --stat
```
Must show exactly two files changed: `src/saltmdb/domain/services/retrieve_context_service.py` and
`tests/test_retrieve_context_service.py`. No other file (including
`lineage_assembly_service.py`, `context_budget_service.py`, `mcp/tools.py`, or any spec file other
than this one) may appear in the diff.

## 0. Status

**LOCKED.**

**Scope**: may edit `src/saltmdb/domain/services/retrieve_context_service.py` (the filtering
insertion in §2) and `tests/test_retrieve_context_service.py` (the new test in §4) only. Does not
touch `lineage_assembly_service.py`, `context_budget_service.py`, `conflict_set_service.py`,
`context_expansion_service.py`, `mcp/tools.py`, `daemon/dispatch.py`, `daemon/protocol.py`,
`src/saltmdb/config.py`, `tests/test_lineage_assembly_service.py`,
`tests/test_retrieve_context_wiring.py`, or any other spec file.

**Pre-lock gate, run against the current tree (this session, 2026-09-14):**
1. Read all of §1-§5 above in full before writing this Status section — done, in this order.
2. Acceptance's own commands are runnable pytest/ruff/mypy invocations — will be run by OMP as the
   actual last-step verification per Coding Standards rule 18; not run speculatively here per this
   workspace's Role boundary (spec verification stays a probe, never a build) — the two narrow
   reads already performed (the exact call site at `retrieve_context_service.py:100-107`, and
   `lineage_assembly_service.py:41-136` in full) are the probes, not a build of the fix itself.
3. Every file named by the diff-stat acceptance command is already enumerated in §0's scope list
   above (exactly the two files §2 and §4 edit) — confirmed by construction, since both were
   authored together in this document.
4. No mechanical/generated-file side effect applies here (no lockfile, no codegen) — this is a
   pure Python source + test edit, nothing else regenerates.
5. Grepped for the old behavior's textual footprint before locking:
   `grep -rn "lineage_result" src/ tests/` → only `retrieve_context_service.py` (the one line being
   changed) and `lineage_assembly_service.py` (its own internal variable name, `output`, actually —
   confirmed `lineage_assembly_service.py` does not use the name `lineage_result` at all, it returns
   a local `output` dict; no other file references the old unfiltered-passthrough behavior by name).
   `grep -rln "assemble_lineage\|lineage_result" src/ tests/` → `lineage_assembly_service.py`,
   `retrieve_context_service.py`, `viewer/routes/entity_detail.py`, `tests/test_lineage_assembly_service.py`,
   `tests/test_relation_service.py` — confirmed `entity_detail.py` and `test_relation_service.py`
   call the unrelated `get_lineage`/`analyze_lineage` (A3's underlying primitive, pre-existing and
   general-purpose), not `assemble_lineage` — grep matched on the substring `lineage` inside those
   two files' own unrelated function names, not a real reference to this slice's code path. No
   hardcoded duplicate of the old passthrough behavior exists anywhere else in the tree.
6. Opened every file on the "does not touch" list that could plausibly already contain something
   this fix's acceptance bar demands gone: `lineage_assembly_service.py` (read in full, §2 above)
   contains no budget-awareness to remove; `mcp/tools.py`'s docstring (read in full, quoted in §1)
   already states the *correct* post-fix contract and needs no edit.
7. The one data-shape claim repeated in more than one place — "lineage map for every surfaced
   head" (tool docstring) vs. the actual pre-fix behavior (every A1 candidate regardless of A4
   outcome) — is exactly the contradiction this spec closes; diffed word-for-word in §1, and the
   fix brings the code to match the doc rather than the reverse.
8. Concrete worked instance: primary hit `4dfaf02c`-shaped scenario from the live smoke test
   (§1) — a primary hit survives into `memories[]`; one of *its own* expansion candidates has a
   supersession chain and gets pruned by A4's budget pass; that candidate's head must not appear in
   `lineage`. Traced against G7's equal-visibility reconciliation (§2 of `retrieve_context_service.py`,
   lines 109-140): a `conflict_only` force-included entity recovered from `dropped_entity_ids` by
   that step *is* still counted as surfaced (it's read from `memories` after reconciliation runs,
   per §2's exact insertion point below reconciliation) — so a resolved-conflict lineage entry
   attached to a force-included head is correctly preserved, not accidentally pruned. Also traced
   against A3's `historical_truncated`/`was_flagged_contradiction` fields (constraints 14/15): these
   live inside each surviving `lineage[head]` entry unchanged; pruning operates only at the
   head-key level, never inside a retained entry's own `historical` list.
9. Third-party/library behavior: none relied upon here — this is pure in-process dict filtering
   over already-computed Python structures, no new library call, no changed interaction with
   `fastembed`, `sqlite3`, or any existing test's mocking pattern.

**Handoff**: worktree `SALTMDB-a6-lineage-scope-fix` (branch `feature/context-retrieval-a6-lineage-scope-fix`,
based on `context-aware-search`), `uv sync` already run. Merge target for this branch, per the
standing branch-strategy rule (memory `64658415`) and the categorical master-touch restriction
(memory `36b96c60`): **`context-aware-search` only, never `master`, never pushed to `origin` without
separate explicit instruction.**
