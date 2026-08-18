# Third Implementation Review: Core-Memory Bootstrap Governance

Reviewer: Codex  
Date: 2026-08-18  
Branch/state reviewed: `rework` at committed base `e84e15e`, with Claude's implementation and third-round fixes present as uncommitted working-tree changes  
Prior review: `plans/core_memory_bootstrap_governance_followup_review.md`

## Verdict

**One change requested — nearly ready, but not yet safe for live-data transition.**

Claude correctly fixed all three blockers from the follow-up review:

1. Overdue enforcement now compares the full canonical pessimistic rendered contribution rather than only `full_content` length.
2. Core-producing consolidation no longer excludes overdue parent cores; explicit review/demotion/archive must happen first.
3. Preserved reason, exit condition, review timestamp, and detail declarations are now validated as effective final state.

The new regression coverage is strong, the focused suite passes 199 tests, the full suite passes 915 tests, and MyPy/scoped Ruff/formatting pass.

One newly exposed effective-state mismatch remains. It can bypass both overdue rendered-growth enforcement and the hard 15,000-character capacity calculation for updates to non-`fact` core memories.

No implementation files were modified during this review.

## Finding

### [P1] Omitted `memory_type` is sized as `fact` even though the committed update preserves the existing type

**Evidence:** Both prospective-row builders use:

```python
"memory_type": memory_type or "fact"
```

at:

- `src/saltmdb/domain/services/memory_service/write.py:146`
- `src/saltmdb/domain/services/memory_service/write.py:611`

That is correct for a new memory, but wrong for an update where `memory_type` is omitted. The SQL write preserves the existing value through:

```sql
memory_type = COALESCE(?, entities.memory_type)
```

Therefore governance renders and sizes a different row from the one the transaction actually commits.

For an existing `preference` core, the prospective entry is rendered twice with `fact`—once in the XML `type` attribute and once in YAML—while the committed/bootstrap row remains `preference`. The undercount is 12 characters before considering other edits. Other non-`fact` types have similar deltas.

This affects two hard guarantees:

1. **Overdue boundary:** the false `fact` shortening creates artificial headroom, allowing title/reason/exit/content growth while governance believes the rendered contribution did not increase.
2. **Capacity admission:** near the 15,000-character boundary, the prospective digest can be admitted below the cap and then commit a larger exact digest above it.

Independent reproduction with an overdue `preference` core, omitted `memory_type`, unchanged content, and a longer title:

```text
Knowledge stored successfully with ID: 5dd29620-292e-4713-8037-ba840a4151b5
TYPE preference
RENDERED_DELTA 6
```

The committed bootstrap contribution grew by six characters even though overdue governance admitted the update.

**Root cause:** core governance correctly resolves effective lifecycle/detail fields, but `memory_type` is resolved outside that effective-state model. The preview and transaction paths independently substitute the new-memory default instead of using the update target's persisted type.

**Required fix:** Resolve the effective memory type exactly once using the same semantics as persistence:

- new memory + omitted type → `fact`;
- existing memory + omitted type → existing `entities.memory_type` (falling back to `fact` only for a legacy null);
- supplied valid type → supplied type.

Use that effective value consistently in:

- the proposed payload used by Track A/token matching if relevant;
- preview `prospective_entry`;
- transactional `prospective_entry`;
- capacity admission;
- overdue rendered-delta comparison;
- the eventual SQL write semantics.

Prefer a shared effective-state resolver or returning effective `memory_type` alongside the resolved core state. Do not fix only one of the two prospective builders; preview and authoritative transaction logic must remain identical.

**Required regression tests:**

1. Create an overdue `preference` core, omit `memory_type`, and grow the title by fewer characters than the `preference`→`fact` undercount; the update must reject for rendered growth.
2. Repeat for `procedure`, `decision`, and `event` to prevent type-specific assumptions.
3. Omitted type with no rendered growth preserves the original type and succeeds.
4. Explicitly changing a type uses the new type in both sizing and persistence.
5. Construct a digest just below 15,000 where the false `fact` sizing would pass but the preserved non-`fact` value exceeds the cap; the corrected path must return `CORE_CAPACITY_EXCEEDED` with zero side effects.
6. Assert preview and in-transaction admission use the same effective type under a stale/concurrent update scenario.
7. Verify the exact canonical digest after every accepted update is no larger than the prospective digest used for admission.

## Confirmed fixes from the previous review

### Rendered-delta overdue enforcement

`enforce_overdue_boundary` now loads the current complete row, renders both current and prospective entries with `due=False`, and rejects a positive total delta. Tests cover title, reason, exit condition, equal size, offsetting shrink/growth, and another overdue core. This architecture is correct once the effective `memory_type` mismatch above is removed.

### Overdue-parent consolidation

`has_overdue_core` now considers every active core and core-producing consolidation calls it without excluding parents. Tests verify rejection with zero side effects, successful non-core consolidation, and successful retry after explicit `review_core_memory(retain)`.

### Preserved lifecycle validation

Preserved reason/exit values now pass through canonical validation. Preserved timestamps are canonicalized/structurally validated while valid overdue timestamps remain eligible for non-expanding cleanup. Tests cover malformed fields, repair paths, and rollback behavior.

## Verification performed

```text
Focused core/API suite: 199 tests, OK
Full unittest discovery: 915 tests in 32.742s, OK
mypy src: Success, 55 source files
Ruff check on touched implementation/test files: passed
Ruff format --check on touched implementation/test files: passed (14 files)
```

The known unrelated repository-wide Ruff/Bandit/Deptry debt was not rerun; scoped changed-file checks remain clean.

## Recommended next step

Fix the effective `memory_type` resolution, add the boundary tests above, rerun focused/full/static verification, and request one final narrow review. If that passes, proceed to the already-planned cold Luna usability evaluation before any live database transition.

## Final acceptance condition for this round

For every accepted store update, the row used for overdue and capacity rendering must be byte-for-byte equivalent—in every rendered field—to the row bootstrap will load after commit. No omitted-field preservation rule may cause prospective sizing to differ from persisted state.
