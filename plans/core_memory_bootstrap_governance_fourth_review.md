# Fourth and Final Code Review: Core-Memory Bootstrap Governance

Reviewer: Codex  
Date: 2026-08-18  
Branch/state reviewed: `rework` at committed base `a8c5ae0`, with Claude's implementation and final fix present as uncommitted working-tree changes  
Prior review: `plans/core_memory_bootstrap_governance_third_review.md`

## Verdict

**Approved at code-review level. No remaining implementation blocker found.**

Claude correctly fixed the final effective-`memory_type` sizing mismatch. Prospective bootstrap rendering now resolves the same memory type that persistence will commit:

- omitted type on a new memory resolves to `fact`;
- omitted type on an existing memory preserves the stored type;
- a legacy null type resolves to `fact`;
- an explicitly supplied valid type wins;
- preview and authoritative transaction paths call the same resolver.

The former bypass now rejects independently with zero state change. The expanded tests cover every non-`fact` type, explicit type changes, omitted-type preservation, a digest boundary that distinguishes false `fact` sizing from true `preference` sizing, and exact prospective-versus-committed rendering equivalence.

No implementation files were modified during this review.

## Final finding status

No P0, P1, or P2 code findings remain from this review series.

### Final prior blocker: fixed

The third review found that both prospective-row builders used:

```python
"memory_type": memory_type or "fact"
```

for updates, while SQL preserved the existing type. The implementation now uses `resolve_effective_memory_type` in both paths:

- `src/saltmdb/domain/services/memory_service/write.py:143–147`
- `src/saltmdb/domain/services/memory_service/write.py:613–619`
- canonical resolver at `src/saltmdb/domain/services/core_governance_service.py:799–819`

The resolver mirrors the SQL `COALESCE(?, entities.memory_type)` semantics. This restores the core invariant that the row used for overdue/capacity rendering matches the row bootstrap loads after commit.

Independent rerun of the former overdue-`preference` reproducer:

```text
Error: Cannot enlarge a core memory's rendered bootstrap contribution ... while core ... is overdue for review ...
TYPE preference
RENDERED_DELTA 0
```

The rejected update preserved both title and type.

## Review-series closure

The implementation now resolves every issue raised across the four Codex reviews:

1. Track A cannot create undeclared core detail links.
2. Consolidation core-parent checks and parent state are revalidated inside the transaction, including single-parent paths.
3. Review archival is owner-neutral only for eligible core memories and cannot archive ordinary memories.
4. `core_detail_memory_ids` is the sole governed declaration and is revalidated on writes and bootstrap.
5. Lifecycle free text is normalized/redacted and effective preserved fields are validated.
6. `is_core` input is strictly parsed.
7. Bootstrap is core-only, complete, canonical, and fail-closed.
8. Bootstrap error output is bounded with omission counters.
9. Timestamps persist canonically in UTC and new review times are future-bounded.
10. Overdue state blocks every positive rendered-contribution delta and every core-producing consolidation until explicit review/demotion/archive.
11. Capacity failures remain side-effect-free and return actionable totals/reductions/inventory.
12. Effective `memory_type` now matches persistence in every admission/rendering path.

## Verification performed

### Independent behavioral check

The exact prior `preference`-core bypass was rerun against a temporary database. It now rejects for overdue rendered growth and leaves the canonical rendered contribution unchanged.

### Automated verification

```text
Focused core/API/memory-type suite: 227 tests, OK
Full unittest discovery: 929 tests in 31.705s, OK
mypy src: Success, 55 source files
Ruff check on touched implementation/test files: passed
Ruff format --check on touched implementation/test files: passed (15 files)
```

Known unrelated repository-wide Ruff/Bandit/Deptry debt was already documented in the first review and was not rerun. The implementation's scoped changed-file gates are clean.

## Remaining non-code rollout gates

Approval here covers the reviewed implementation, not the intentionally deferred production-data mutation.

Before live rollout:

1. Run the planned cold Luna usability evaluation against the final MCP schemas/descriptions.
2. Resolve any critical weaker-model misunderstanding found by that evaluation.
3. Commit Claude's implementation and tests as a coherent descriptive commit; they remain uncommitted in the working tree at the time of this review.
4. Back up the live SALTMDB database through the supported mechanism.
5. Perform the documented agent-driven core-memory cleanup using SALTMDB tools only—never direct SQL.
6. Verify the final live core set is at most five entries, every entry is at most 2,500 content characters, and the exact rendered digest is at most 15,000 characters.
7. Start a fresh agent session and confirm complete injection with no hook spill/truncation warning.

The live-data transition still requires separate user authorization because it changes the user's real memory graph. This review does not authorize or perform it.

## Final recommendation

Proceed to the Luna usability gate. If Luna passes after any description-only refinements, commit the implementation and prepare the separately authorized live-data transition. No further Codex code-fix round is required based on the reviewed state.
