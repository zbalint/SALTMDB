# Implementation Review: Core-Memory Bootstrap Governance

Reviewer: Codex  
Date: 2026-08-18  
Branch/state reviewed: `rework` at committed base `213cfec`, with Claude's implementation present as uncommitted working-tree changes  
Specification: `plans/core_memory_bootstrap_governance_detailed.md`  
Prior design review: `plans/core_memory_bootstrap_governance_review.md`

## Verdict

**Changes requested — not ready for live-data transition or release.**

The implementation establishes a strong foundation: the schema additions, central governance service, strict `is_core` parser, Track A restriction, relation guard, MCP/daemon registration, simplified core-only bootstrap path, ownership-neutral internal archival helper, documentation, and broad test suite are all structurally sound. The focused and full unit suites pass.

However, eight correctness gaps remain. The first five violate locked governance invariants through normal public operations. Four were independently reproduced against temporary databases. The bootstrap also fails to validate declared detail state and its error renderer is not actually bounded. These are release blockers because the feature's purpose is hard, transactionally enforced invariants rather than advisory behavior.

No implementation files were modified during this review.

## Findings

### 1. [P1] An overdue core can enlarge itself

**Evidence:** `src/saltmdb/domain/services/core_governance_service.py:588` excludes the entity being updated from `has_overdue_core`. If the target is the only overdue core, `has_overdue_core` returns `None`, so the later content-length comparison at lines 602–610 never executes.

This contradicts the locked rule that **any overdue core blocks core enlargement**, including enlargement of the overdue memory itself. The allowed operation is a non-expanding/capacity-reducing edit; resolving the overdue lifecycle requires `review_core_memory`.

The existing test `test_content_enlargement_blocked_while_overdue` is a false positive. Its proposed content is mostly repeated `x` characters and is rejected earlier by the generic entropy quality gate, not by overdue governance. A varied, quality-valid enlargement succeeds:

```text
Knowledge stored successfully with ID: 04c7c0a5-8672-4692-95ec-f417c6d0935d
```

**Required fix:** Do not exclude the update target when detecting overdue state. Detect whether any active core—including the target—is overdue, then allow only a demotion, archive, review, or a prospective non-expanding edit. Compare the complete rendered contribution if lifecycle/title changes can expand the digest; at minimum enforce content non-expansion as specified.

**Required regression tests:**

- A quality-valid, varied content enlargement of the sole overdue core is rejected specifically for the overdue boundary.
- A shrink of the sole overdue core succeeds.
- A non-expanding edit with another overdue core succeeds.
- A title/lifecycle change that expands rendered output is handled according to the intended “no core expansion” boundary.

### 2. [P1] Core-producing consolidation ignores the overdue-write boundary

**Evidence:** `resolve_consolidation_core_state` validates lifecycle fields and capacity (`core_governance_service.py:741–803`), and `commit_consolidation` invokes it transactionally (`relation_service.py:944–981`), but neither path calls `enforce_overdue_boundary` or otherwise rejects a new consolidated core while an unrelated active core is overdue.

Reproduction against a temporary database:

```text
CONSOLIDATE_WHILE_OVERDUE Successfully committed consolidated memory with ID: 064181ce-4a1d-4ec9-87d8-b32d24741af4
```

This directly violates the locked rule that overdue state blocks “consolidation that produces a core.”

**Required fix:** Inside `_do_commit`, after resolving fresh parent/core state and before any write, apply the overdue boundary using the transaction connection. Parent cores archived by the same transaction may be excluded only if that operation actually resolves the overdue state; unrelated overdue cores must block the commit. Bulk mode must recompute against transaction-visible state for every item.

**Required regression tests:**

- Single-parent explicit core consolidation is rejected while an unrelated core is overdue.
- Multi-parent explicit core consolidation is rejected under the same condition.
- Bulk core-producing consolidation is rejected atomically.
- Consolidating/demoting the overdue parent into a non-core result remains allowed.
- A core result that replaces the only overdue parent has an explicitly tested policy; it must not silently reset review lifecycle outside `review_core_memory`.

### 3. [P1] `review_core_memory(outcome="archive")` can archive any ordinary active memory

**Evidence:** The archive branch at `core_governance_service.py:905–917` checks only `status == "archived"`; it never requires `is_core_now`. It then invokes the intentionally ownership-neutral `_archive_entity_unchecked` helper.

Reproduction with an ordinary non-core memory owned by `alice`, reviewed by `bob`:

```text
ARCHIVE_NORMAL Memory 'f4b3cf09-05d5-423f-b607-405b139a4f2a' was reviewed and archived.
```

The helper itself is appropriate for the resolved owner-mismatch design gap, but the public review tool is defined as reviewing a core memory. As implemented, it becomes a second general archival API that bypasses `archive_memory`'s ownership guard for every normal memory.

**Required fix:** Require the target to be an active core before the archive mutation. Preserve a no-op only for an already archived former core when historical core lifecycle fields prove that it was a core; otherwise reject ordinary non-core targets. If distinguishing former cores is not reliable for legacy rows, choose and document a conservative rule rather than granting a general owner-neutral archive path.

**Required regression tests:**

- Active ordinary non-core memory is rejected and remains unchanged.
- Active core owned by another agent is archived successfully by the reviewer.
- Already archived former core returns the intended no-op.
- Already archived never-core memory does not masquerade as a reviewed core.

### 4. [P1] Updating a linked core can preserve declarations after removing required references

**Evidence:** `resolve_store_core_state` validates detail IDs only when `detail_memory_ids` is explicitly provided (`core_governance_service.py:717–722`). When it is omitted, it copies `previous_detail_ids` without revalidating them against the proposed new content.

This violates the invariant that the core content must mention the canonical title and full UUID of **every declared detail memory**. An update can replace the entire content, omit `detail_memory_ids`, remove every reference, and still persist the old JSON declaration and relation.

Reproduction:

```text
Knowledge stored successfully with ID: ceb75ad8-be4c-4b4c-ad30-fec906e5b5b7
["71ddbd61-3888-4401-8ac1-0905126dacb5"]
False  # declared UUID no longer appears in full_content
```

**Required fix:** After resolving the effective declaration—whether supplied or preserved—always validate the full list against the prospective content and current detail entities. `None` means “preserve IDs,” not “skip validation.” Do this both in advisory preflight and authoritatively inside the transaction.

**Required regression tests:**

- Omitting `detail_memory_ids` while retaining all title/UUID references succeeds.
- Omitting the field while removing a title or UUID rejects with zero side effects.
- A declared detail that became private/core/missing between preflight and commit causes transactional rejection.

### 5. [P1] Bootstrap does not validate `core_detail_memory_ids` invariants

**Evidence:** `load_active_cores` (`core_governance_service.py:430–450`) does not select `core_detail_memory_ids`. `find_invariant_violations` (`:462–496`) therefore cannot validate JSON shape, the three-item limit, full UUIDs, existence, shared/non-core detail state, or required title/UUID mentions.

The approved plan explicitly requires bootstrap to fail closed on lifecycle **and detail-ID** invariants. This is essential because detail entities can change independently after core creation: a detail can be promoted to core, made private, retitled, corrupted, or its declaration can be malformed by legacy data. The current bootstrap will inject such a core as valid.

**Required fix:** Load the declared JSON and validate it in the canonical invariant pass. Bootstrap validation must be read-only and should report malformed JSON rather than throwing. It must permit archived details, because exact-ID archived retrieval is intentionally supported. Do not mutate or reconcile relations during bootstrap.

**Required regression tests:**

- Malformed JSON, more than three IDs, short/non-UUID IDs, missing entity, private detail, core detail, missing title mention, and missing UUID mention each produce only `<core-bootstrap-error>`.
- Archived shared non-core detail remains valid.
- No core content appears beside the error report.

### 6. [P1] The fail-closed bootstrap error report is not bounded

**Evidence:** `render_bootstrap_error` appends an inventory line for every row (`core_governance_service.py:374–421`) with no character budget or omitted-count marker. This contradicts its own docstring and the locked plan requirement that even a heavily corrupt database produce a bounded report.

A synthetic 100-row corrupt set with maximum-length titles produced:

```text
ERROR_LEN 26901
```

That exceeds the 15,000-character operational envelope and can itself trigger the hook truncation/spill behavior this feature exists to prevent.

**Required fix:** Construct the report under an explicit maximum (preferably comfortably below 15,000), reserve room for closing tags/instructions, append inventory entries only while they fit, and add `omitted_core_count=N`. Escape or bound every user-controlled value included in the error. Add a final assertion/fallback ensuring the returned report never exceeds the chosen cap.

**Required regression tests:**

- Hundreds/thousands of corrupt rows still yield a syntactically closed report under the cap.
- The report states the omitted count.
- Long titles/owners/timestamps cannot break the cap or XML structure.

### 7. [P2] Review timestamps are not canonical UTC, and new cores may be born overdue

**Evidence:** `_parse_iso_utc` (`core_governance_service.py:135–139`) assigns UTC to a naive timestamp instead of rejecting it, and `parse_core_review_after` returns `dt.isoformat()` without converting an aware offset to UTC (`:142–171`). A `+02:00` input remains `+02:00`:

```text
TZ 2026-08-19T15:07:01.048765+02:00
```

The plan requires SALTMDB's canonical timezone-aware **UTC** representation. Additionally, `parse_core_review_after` enforces only the upper 30-day bound; it accepts past timestamps for new core creation/consolidation. `review_core_memory(retain)` separately rejects non-future timestamps, but create/promote paths can create a core that is overdue immediately.

**Required fix:** Decide the exact existing project convention and enforce it consistently: reject naive timestamps or explicitly canonicalize them, convert aware inputs with `astimezone(UTC)`, and persist one UTC ISO form. Require future review timestamps for create, promotion, consolidation, and retain. Equality is due and therefore not a valid “next review.”

**Required regression tests:**

- Offset input persists in canonical UTC.
- Naive-input policy is explicit and tested.
- Past/equal timestamps reject on create, promotion, consolidation, and retain.
- Default remains 14 days and maximum remains 30 days relative to one captured `now`.

### 8. [P2] Single-parent consolidation still lacks complete parent-state TOCTOU revalidation

**Evidence:** `_do_commit` revalidates parent `content_hash`/`status` only when `cohesion_gate_applicable`, which is false for a single parent (`relation_service.py:915–942`). The new core resolver does re-read `is_core` and `status`, but it uses status only to determine whether a parent is an active core; it does not reject a parent that became archived or whose content changed after resolution. Therefore the prior review's requirement to thread parent core/state checks through the existing observed-state mechanism is only fully met for two-or-more-parent consolidation.

This can produce a consolidated child from a parent whose content/status changed between resolution and commit. It also makes behavior depend on parent count for reasons unrelated to cohesion.

**Required fix:** Capture and transactionally compare minimal parent state for every consolidation, including single-parent promotion. Cohesion-vector work may remain skipped for one parent, but state revalidation must not be coupled to whether the cohesion gate runs. At minimum verify existence, eligible status, content hash, and core status used for the decision.

**Required regression tests:**

- Single parent content change between preflight and commit aborts.
- Single parent archival between preflight and commit aborts.
- Single parent core-status change is rejected/re-evaluated according to explicit `is_core` rules.
- No parent or child mutation survives a stale-state rejection.

## Secondary specification mismatches

These are not independent release blockers if corrected while addressing the findings above:

1. `check_capacity_admission` does not return the plan's `current.rendered_characters` or `required_reduction` fields. The response is useful, but agents are missing the exact amount they must reduce. Align the shape with the documented contract or update the contract deliberately.
2. Bootstrap invariant checking validates reason/exit only by length, not through the canonical bounded-text validators. Legacy/control-character/redaction anomalies can therefore pass differently at read time than write time.
3. `_escape_yaml_line` escapes quotes and newlines but not backslashes. In YAML double-quoted scalars, caller text containing sequences such as `\n`, `\t`, or `\u...` can be interpreted differently by a YAML consumer. Use a real deterministic scalar encoder or also escape backslashes/control characters.
4. The plan file currently declares `Status: Implemented ... pending codex review` and claims phases 1–7 are complete. After this review it should state “changes requested” until the P1 findings and missing acceptance work are resolved.
5. Luna usability evaluation remains unperformed. It is an explicit acceptance criterion, although it should occur after correctness fixes stabilize the schema/descriptions.

## What was implemented well

- Centralizing rules/rendering in `core_governance_service.py` is the correct architecture.
- Schema additions and the partial review index use existing migration patterns.
- `parse_is_core` correctly rejects ambiguous booleans in store and consolidation domain paths.
- Track A no longer offers `elaborate` for core candidates.
- Direct/manual new `elaborates_on` edges into active cores are guarded while exact duplicates remain no-ops.
- The internal ownership-neutral archive helper preserves the public `archive_memory` owner guard; only the caller eligibility check is missing.
- Store writes repeat core admission inside the write transaction.
- Consolidation core-parent status is read inside its transaction, and bulk uses the transaction connection.
- Lifecycle free text is redacted and line-normalized before validation/persistence.
- CLI/hook bootstrap is substantially simpler: core-only, no project search, no hidden core limit, no N+1 fetch.
- MCP/daemon protocol registration follows existing architecture.
- Documentation clearly explains scarce temporary core memory and autonomous rebalancing.
- The new tests are extensive and the complete existing unit suite remains green.

## Verification performed

### Passing

```text
Focused core/API suite: 149 tests, OK
Full unittest discovery: 865 tests in 33.684s, OK
mypy src: Success, 55 source files
Ruff check on touched implementation/test files: passed
Ruff format --check on touched implementation/test files: passed (14 files)
```

### Repository-wide gates still nonzero

```text
ruff check .        -> 22 pre-existing findings in scripts/benchmarking/evaluate_gate_d_development.py
ruff format --check -> 31 pre-existing/unrelated files would be reformatted
bandit -r src -q    -> 57 medium-confidence/medium-severity SQL-expression findings, no high severity
deptry .            -> 281 existing dependency findings
```

These global failures appear unrelated to the core-governance diff, but the plan's literal acceptance statement that the full repository verification suite passes is not currently true. The scoped changed-file Ruff result and green unit/mypy results should be reported separately from known repository debt.

## Recommended repair order

1. Fix archive eligibility and overdue enforcement in store/consolidation.
2. Revalidate effective detail declarations on every write and at bootstrap.
3. Bound the error renderer.
4. Canonicalize/validate timestamps.
5. Decouple single-parent parent-state revalidation from the cohesion gate.
6. Add the regression tests listed above and ensure failures assert the intended governance error, not merely any earlier generic error.
7. Re-run full unit/mypy/scoped Ruff, then perform the cold Luna usability audit.
8. Only after a clean follow-up review should the live database transition begin.

## Acceptance for follow-up review

A follow-up is ready when all eight findings have direct regression coverage; the four reproduced bypasses now reject with zero side effects; malformed detail declarations fail bootstrap closed; the largest tested error report stays under its declared cap; timestamps persist canonically in UTC; single-parent consolidation detects stale parent state; all 865+ tests pass; and the plan status accurately reflects the review state.
