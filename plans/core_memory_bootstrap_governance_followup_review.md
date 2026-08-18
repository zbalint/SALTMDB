# Follow-Up Implementation Review: Core-Memory Bootstrap Governance

Reviewer: Codex  
Date: 2026-08-18  
Branch/state reviewed: `rework` at committed base `da3055d`, with Claude's implementation and follow-up fixes still present as uncommitted working-tree changes  
Prior review: `plans/core_memory_bootstrap_governance_implementation_review.md`

## Verdict

**Changes requested — substantially improved, but not ready for live-data transition.**

Claude correctly fixed all eight concrete defects from the first implementation review. The expanded focused suite passes 187 tests, the complete repository suite passes 903 tests, and scoped static/format checks pass.

The follow-up nevertheless leaves three reproducible governance gaps. Two come from intentionally narrowing the overdue boundary beyond the approved specification, and one is a remaining effective-state validation bypass. These should be fixed before Luna usability validation or live database migration.

No implementation files were modified during this review.

## Status of the original eight findings

| Prior finding | Follow-up result |
|---|---|
| 1. Sole overdue core could enlarge its content | **Fixed for `full_content` growth.** The test now uses quality-valid varied prose and asserts the governance error. A broader rendered-growth gap remains as new finding 1 below. |
| 2. Core-producing consolidation ignored unrelated overdue cores | **Fixed for unrelated overdue cores** in single, multi-parent, and bulk paths. An invented same-transaction exception remains as new finding 2 below. |
| 3. Review archive could archive ordinary memories | **Fixed.** Active/archived never-core targets reject; cross-owner active cores archive; archived former cores no-op; the public archive ownership guard remains intact. |
| 4. Preserved detail IDs skipped validation | **Fixed.** The effective declaration is revalidated against prospective content in preview and transaction paths. |
| 5. Bootstrap skipped detail declaration validation | **Fixed.** Bootstrap loads and validates JSON, cap, UUID, existence, scope/core state, and title/UUID mentions while allowing archived details. |
| 6. Bootstrap error was unbounded | **Fixed.** The report has an explicit cap, omission counters, reserved footer space, and large/corrupt-set tests. |
| 7. Timestamps were noncanonical and could start overdue | **Fixed.** Aware offsets normalize to UTC, naive input follows a documented UTC convention, and create/promote/consolidate/retain require a strictly future value within 30 days. |
| 8. Single-parent consolidation lacked parent-state revalidation | **Fixed.** Single-parent paths now capture state independently of cohesion and revalidate it inside the transaction. |

## Remaining findings

### 1. [P1] Overdue cores can still expand their rendered bootstrap contribution

**Evidence:** `enforce_overdue_boundary` in `src/saltmdb/domain/services/core_governance_service.py` compares only `len(full_content)` with the current content length. It does not compare the prospective canonical rendered entry or digest. The new test `test_title_only_growth_not_treated_as_content_expansion` explicitly blesses title growth while overdue and claims the prior review accepted this narrower boundary. It did not.

The approved plan is explicit:

- “Allowed: updating a core when the prospective total does not increase.”
- “Blocked: any increase in rendered core size.”

The same bypass applies to expanding `core_reason` or `core_exit_condition`, not only the title. All are injected and consume the globally scarce rendered budget.

Independent reproduction:

```text
TITLE_EXPANSION Knowledge stored successfully with ID: 44a2b07d-2bbe-4d91-bd8c-07d11100de14
```

The target was the sole overdue core; content was unchanged; only the title grew substantially.

**Required fix:** Pass the complete prospective rendered row into the overdue boundary and compare its pessimistic canonical rendered contribution against the current row's pessimistic contribution. If any active core is overdue, reject a positive delta. A decrease or exact equality remains allowed. Do not duplicate renderer arithmetic in the write service.

**Required regression tests:**

- Title-only rendered growth of an overdue core rejects.
- `core_reason`-only and `core_exit_condition`-only growth reject.
- Equal rendered contribution succeeds.
- Shrinking content enough to offset a longer title/reason is decided by the total rendered delta, not by any one field in isolation.
- Another overdue core permits only non-increasing rendered edits to the target core.

Remove or reverse `test_title_only_growth_not_treated_as_content_expansion`; it currently codifies behavior contrary to the specification.

### 2. [P1] Core consolidation can replace an overdue parent without a review

**Evidence:** `commit_consolidation` calls `enforce_overdue_boundary(... exclude_ids=resolved_parents)` at `src/saltmdb/domain/services/relation_service.py:994–1002`. This excludes every parent about to be archived, allowing a core-producing consolidation when the sole overdue core is among those parents. The new test `test_core_result_replacing_sole_overdue_parent_succeeds_with_fresh_lifecycle` explicitly establishes this exception.

That exception was not approved. The locked decision says an overdue core blocks “consolidation that produces a core.” Retaining a due core requires review rationale, reviewer identity, and a new review timestamp through `review_core_memory`. Replacing it with a newly created core silently resets the lifecycle without recording review provenance and bypasses the dedicated review operation.

The allowed autonomous recovery paths are already sufficient:

1. Consolidate/demote/archive the overdue core into a **non-core** result; or
2. Review it (`retain`, `demote`, or `archive`), then perform a later core-producing consolidation when no overdue core remains.

**Required fix:** Do not exclude parent IDs from overdue detection when the consolidation result is core. Any overdue active core at transaction start blocks a core-producing consolidation, including a parent that the same transaction would archive. Keep explicit non-core consolidation allowed.

**Required regression tests:**

- Reverse `test_core_result_replacing_sole_overdue_parent_succeeds_with_fresh_lifecycle`: it must reject with zero side effects.
- The overdue parent remains active/core and no child/relations/events are created.
- After a successful explicit review or demotion, retrying the same consolidation follows normal admission.
- Non-core consolidation of overdue parents remains allowed.

### 3. [P1] Preserved lifecycle fields are not revalidated on core updates

**Evidence:** `resolve_store_core_state` validates `core_reason` and `core_exit_condition` only when the caller supplies a replacement. Truthy existing values are copied verbatim without the canonical validators. Existing `core_review_after` is likewise copied without parsing/canonical validation when omitted.

This means a malformed active core can be updated successfully while remaining malformed, even though the approved transaction order requires structural lifecycle validation of the effective final state before every committed mutation.

Independent reproduction after corrupting an existing core's reason to `"short"`:

```text
MALFORMED_PRESERVED Knowledge stored successfully with ID: 44a2b07d-2bbe-4d91-bd8c-07d11100de14
REASON short
```

The update shortened content and omitted lifecycle fields. It committed while preserving an invalid five-character reason, so bootstrap still fails closed afterward.

This is the same class of bug that was fixed correctly for preserved `detail_memory_ids`: omission means “preserve the value,” not “skip validation of the effective value.”

**Required fix:** Resolve the effective reason, exit condition, and review timestamp first, then validate/normalize the effective values regardless of whether they were supplied or preserved. For an overdue but structurally valid timestamp, preserve it during an allowed non-expanding edit; “future-only” is an admission rule for setting a new review time, not a reason to prevent cleanup of an already-overdue core. Malformed/unparseable timestamps must require repair or demotion/archive.

**Required regression tests:**

- Preserved too-short/too-long reason rejects.
- Preserved invalid exit condition rejects.
- Preserved malformed/noncanonical review timestamp rejects or canonicalizes according to the documented policy.
- A valid but overdue timestamp may be preserved during a rendered-size-reducing edit.
- Supplying corrected lifecycle fields and a non-expanding repair succeeds transactionally.
- Every rejection leaves content, lifecycle fields, tags, relations, history rows, and embedding jobs unchanged.

## Additional observations

1. The bounded error renderer is now materially safer. Its tests verify size and closing tags, but a future hardening pass could also test adversarial XML-like title/owner strings. This is not a blocker for the approved plan because the current renderer already applies deterministic line escaping and the report cap works.
2. Capacity rejection now includes current rendered characters and exact required reductions, resolving the secondary response-shape mismatch.
3. Backslash escaping was added to YAML-line rendering, resolving the prior secondary note.
4. Luna usability evaluation remains pending and should occur only after the three blockers above are resolved.

## Verification performed

### Passing

```text
Focused core/API suite: 187 tests, OK
Full unittest discovery: 903 tests in 31.691s, OK
mypy src: Success, 55 source files
Ruff check on touched implementation/test files: passed
Ruff format --check on touched implementation/test files: passed (14 files)
```

The repository-wide Ruff/Bandit/Deptry debt documented in the first review was not rerun because it was already shown to be unrelated to this implementation. The relevant changed-file gates remain clean.

## Recommended repair order

1. Replace field-specific overdue enlargement logic with canonical rendered-delta enforcement.
2. Remove the same-transaction parent exclusion for core-producing consolidation.
3. Revalidate all effective preserved lifecycle fields, mirroring the corrected detail-declaration path.
4. Add tests that assert the specific governance reason and zero side effects rather than accepting any generic `Error`.
5. Rerun focused/full tests, MyPy, and scoped Ruff/format.
6. Request one final Codex review, then run the planned cold Luna usability evaluation.

## Follow-up acceptance criteria

This implementation is ready for final acceptance when:

- overdue state blocks every positive canonical rendered-size delta;
- no core-producing consolidation can consume an overdue parent without an explicit prior review/demotion/archive;
- every core update validates the complete effective lifecycle state, including preserved fields;
- the three reproductions above reject with zero side effects;
- all focused and full verification remains green;
- Luna correctly operates the final tool descriptions without coaching.
