# SALTMDB Core-Memory Bootstrap Governance and Lifecycle Plan

Status: Approved brainstorming specification; implementation has not started.

Intended reviewer: Claude. Review this plan against the current source before any implementation begins.

## 1. Goal and rationale

Redefine `is_core=True` as a scarce, temporary bootstrap-delivery mechanism for information an agent must know before it could reasonably know to search for it. Core memory is not a general “important knowledge” tier. Stable coding rules, user preferences, and agent behavior normally belong in `AGENTS.md`, `CLAUDE.md`, skills, or equivalent instruction files; durable project facts remain searchable normal memories. Appropriate core candidates are urgent cross-session hazards, temporary overrides, active bugs, migrations, or environment failures that could materially harm work before an agent naturally searches for them.

Current evidence gathered during planning:

- The live database has 20 active core memories.
- Their raw content totals approximately 41,118 characters.
- The exact rendered bootstrap is approximately 45,435 characters / 45,565 UTF-8 bytes.
- Codex CLI 0.147.0 reported 11,392 original tokens and retained approximately 4,969 after truncation. The harness limit is undocumented, but this strongly suggests an approximately 5,000-token hook-output envelope.
- A 15,000-character rendered limit is expected to be roughly 3,750 tokens at the current corpus ratio, leaving useful margin below the 4,500-token operating target without adding a tokenizer dependency.
- The current bootstrap also has a hidden `--core-limit 20`, so a twenty-first core would be silently omitted even without harness truncation.
- Current `store_memory` paths can bypass disposition preflight through explicit updates, same-title updates, and `skip_duplicate_check=True`.
- Current consolidation silently inherits core status when any parent is core.
- Direct entity-ID lookup retrieves archived memories, so archived detail-memory IDs remain durable references.
- Repeating an exact relation is already an idempotent no-op.
- Events have already been removed from the project conceptually and must not return through this work.

The implementation must guarantee after every committed mutation:

```text
active injectable core count <= 5
each core full_content length <= 2,500 Unicode code points
exact complete rendered core digest <= 15,000 Unicode code points
```

These are independent hard limits. The maximum theoretical stored content is therefore 12,500 characters, leaving 2,500 characters for XML/YAML wrappers, titles, lifecycle metadata, and separators.

## 2. Locked product decisions

### Meaning, visibility, and authority

1. Limits are global across the database, not per owner, agent, project, or scope.
2. Private core memories are prohibited. Every active core must have `scope="shared"`.
3. SALTMDB exists for agents, and agents have full authority to maintain its memories regardless of author or whether a memory originated as a user preference.
4. Agents must autonomously decide what to retain, shorten, consolidate, demote, supersede, or archive. Capacity management must not require a human decision.
5. No class of SALTMDB memory is human-protected. Provenance informs judgment but is not a maintenance veto.
6. Core status is normally temporary. When urgency ends, detailed knowledge remains as a normal memory after demotion or archival.
7. Existing asynchronous consolidation/supersession request queues must not be reintroduced.

### Counting and capacity

8. Maximum active non-archived core memories: 5.
9. Maximum stored `full_content` per core: 2,500 Unicode code points.
10. Maximum exact rendered bootstrap digest: 15,000 Unicode code points.
11. A character means one Python Unicode code point measured with `len(text)`, not bytes, graphemes, or tokens.
12. Core content is counted after secret redaction, Markdown auto-formatting, LF line-ending normalization, and all other transformations applied before persistence.
13. The rendered limit includes outer XML, per-memory XML/YAML wrappers, title, lifecycle fields, separators, whitespace, and content.
14. The 2,500-character per-memory limit excludes title and lifecycle columns but includes Markdown syntax and whitespace inside `full_content`.
15. Markdown headers and formatting consume the budget; documentation must encourage concise, actionable formatting.
16. Capacity failure is a hard, side-effect-free failure: no proposed memory, fallback normal memory, relation, event, review token, automatic demotion, or other state is created.
17. Capacity validation precedes duplicate/supersession disposition review. The agent first rebalances and retries; ordinary Track A review then runs.
18. Rejection returns totals and a balanced inventory of every active core: ID, title, type, owner, review timestamp, due state, and rendered contribution. It does not return full contents.

### Lifecycle metadata and stale-core prevention

19. Every core needs a concrete `core_reason` describing the harm that could occur before natural retrieval.
20. Every core needs an observable `core_exit_condition`.
21. Every core needs an absolute `core_review_after` timestamp.
22. `core_reason` and `core_exit_condition` are each 20–500 characters.
23. `core_review_rationale` is 20–1,000 characters, is stored for provenance, and is not injected.
24. Default review interval is 14 days; maximum interval is 30 days.
25. Use the same timezone-aware UTC ISO timestamp representation and shared time helpers already used elsewhere in SALTMDB.
26. An overdue review never automatically demotes or archives a memory.
27. Overdue cores are injected first and explicitly render `review_due: true`.
28. Any overdue core blocks new core creation, promotion, enlargement, or consolidation into core.
29. Normal memory writes, review operations, core demotion/archive, and capacity-reducing core edits remain allowed.
30. Retaining a due core requires a new rationale, reviewer identity, and next review no more than 30 days ahead.

### Linked detail memories

31. A core must be independently actionable even if a lazy or weaker agent never follows a link.
32. Injected content must directly state the operative warning/rule, scope, required and prohibited behavior, critical exceptions, and exit condition.
33. Rationale, chronology, evidence, examples, and implementation detail should move to normal memories when they distract from immediate action or would exceed 2,500 characters.
34. Each core may declare at most three detail memories.
35. References use full UUIDs, not prefixes.
36. Core content must mention the canonical title and full UUID of every declared detail memory.
37. A detail memory must exist, be shared, non-core, and may be active or archived.
38. Core creation/update atomically maintains `detail_memory --elaborates_on--> core_memory` relations.
39. Repeating that relation manually through `manage_relation` remains an idempotent no-op.
40. Archiving or consolidating a detail memory does not invalidate its textual ID because exact ID lookup returns archived memories.
41. Detail references are not automatically rewritten to successors; graph lineage records subsequent evolution.

### Bootstrap behavior

42. Bootstrap injects core memories only.
43. Remove project-memory injection, event injection, pending-request material, and the associated CLI options and machinery.
44. Remove the arbitrary `--core-limit 20`.
45. Every entity with `is_core=True` and `status != "archived"` counts and is injected.
46. Archived entities and historical versions do not count. Superseded but non-archived cores still count until demoted or archived.
47. Ordering is: overdue reviews first, earliest upcoming review, creation time, UUID tie-breaker.
48. Bootstrap fails closed if any active-core invariant is malformed or capacity is exceeded.
49. Failure injects one bounded `<core-bootstrap-error>` report, never an arbitrary subset, truncated digest, or oversized digest.
50. An overdue but otherwise valid core is not a bootstrap invariant failure.

### Consolidation and review

51. Consolidation must never silently inherit core status.
52. If any parent is core and `is_core` is omitted, reject with an actionable error.
53. A consolidated core requires explicit `is_core=True` plus every required lifecycle field.
54. Explicit `is_core=False` may consolidate/archive core parents into a normal result.
55. Keep Track A’s ordinary `consolidate` restriction against core candidates; deliberate core consolidation uses `commit_consolidation` directly.
56. Add a synchronous `review_core_memory` tool with `retain`, `demote`, and `archive` outcomes.
57. Review is a direct operation, not a request, queue, event-driven workflow, or background task.
58. Meaningful content revision remains a `store_memory` update. The review tool changes lifecycle state only.
59. The confusing current overwrite mechanism—same title or explicit ID—must be revisited later for weaker models, but a general update API is out of scope here.

### Documentation and model usability

60. Rules may not exist only in constants, comments, or tests.
61. Document them in MCP schemas/descriptions, README, AGENT_GUIDE, hook documentation, and SALTMDB skills/global integration guidance.
62. Replace old guidance that treats core as permanent behavioral law or permits private subagent cores.
63. Luna usability testing is required for the public schemas and instructions. It must clarify that review timestamps are absolute, reviewer `owner_id` is identity rather than an ownership permission, core-only fields are rejected rather than ignored, blocking boundaries are explicit, and detail relation direction/atomicity is explicit.

## 3. Data model and canonical governance service

Add nullable entity columns through the existing idempotent `_add_column_if_missing` migration mechanism:

```text
core_reason TEXT
core_exit_condition TEXT
core_review_after DATETIME
core_last_reviewed_at DATETIME
core_last_reviewed_by TEXT
core_review_rationale TEXT
core_detail_memory_ids TEXT
```

`core_detail_memory_ids` stores a JSON array of full UUIDs. Relations remain the semantic graph; the JSON declaration preserves intended references if archival expires an edge. Add a partial index on review time/creation for `is_core=1 AND status != 'archived'`.

Update every entity-copy/write surface: raw insert/update, archived SCD history copy, single consolidation, bulk consolidation, fixture schemas, and test DDL. Demoted normal memories may retain historical lifecycle data, but enforcement and rendering ignore it while `is_core=False`.

Create one focused core-governance service used by bootstrap and every mutation path. It owns constants, normalization and validation, active-core loading, overdue detection, exact rendering, per-memory contribution sizing, prospective-state admission, balanced rejection responses, fail-closed error rendering, detail validation, and review operations. Do not duplicate these rules in CLI, memory, and consolidation services.

Canonical constants:

```text
CORE_MAX_ACTIVE = 5
CORE_MAX_CONTENT_CHARS = 2500
CORE_MAX_RENDERED_CHARS = 15000
CORE_REASON_MIN_CHARS = 20
CORE_REASON_MAX_CHARS = 500
CORE_EXIT_MIN_CHARS = 20
CORE_EXIT_MAX_CHARS = 500
CORE_REVIEW_RATIONALE_MIN_CHARS = 20
CORE_REVIEW_RATIONALE_MAX_CHARS = 1000
CORE_MAX_DETAIL_MEMORY_IDS = 3
CORE_DEFAULT_REVIEW_DAYS = 14
CORE_MAX_REVIEW_DAYS = 30
```

The canonical renderer must deterministically and safely encode title, type, `is_core`, reason, exit condition, review timestamp, due flag, and `full_content`; normalize line endings; escape literal closing memory tags; size the exact final digest; and use stable ordering. Prospective sizing should render the longer due-flag representation if representation lengths differ so becoming overdue cannot increase the payload beyond admission.

## 4. Public APIs and mutation semantics

### `store_memory`

Add `core_reason`, `core_exit_condition`, `core_review_after`, and `detail_memory_ids` parameters.

- A new core requires shared scope, reason, exit condition, a valid review timestamp (default now +14 days), valid detail IDs/textual references, and all capacity checks.
- Updating a core with `is_core=None` preserves omitted lifecycle/detail fields and validates the effective final state.
- Promotion from normal to core applies every new-core requirement.
- A new/effective non-core rejects newly supplied core-only fields rather than silently ignoring them.
- `detail_memory_ids=None` preserves the declaration; `[]` clears it; a replacement list atomically reconciles declared relations.
- Explicit ID, same-title update, and `skip_duplicate_check=True` never bypass core enforcement.
- `check_duplicates_only=True` stays read-only and does not attempt capacity admission.
- Preserve the existing parseable `ID: <uuid>` success contract.

Validation/transaction order:

1. Normalize, redact, and format input.
2. Resolve effective target and final core state.
3. Validate structural/lifecycle/detail fields.
4. Detect malformed or overdue existing state.
5. Perform side-effect-free prospective capacity calculation.
6. If admitted, run Track A duplicate/supersession preflight.
7. On commit, repeat authoritative lifecycle, detail, overdue, and capacity validation inside the write transaction before mutation.
8. If concurrent state changed, return a structured rejection and roll back.

The daemon’s single-writer coordinator is useful but is not the correctness boundary; transactions are.

Capacity errors use `status=REJECTED`, `error_code=CORE_CAPACITY_EXCEEDED`, violated dimensions, limits, current/proposed totals, required reductions, and the balanced inventory. The message explicitly confirms that no state was written.

While any core is overdue, allow normal writes, review, demotion/archive, and non-expanding or reducing core edits. Block creation, promotion, rendered-size increase, core-producing consolidation, and changing the next-review timestamp except through valid `retain` review.

### `review_core_memory`

```text
review_core_memory(
    entity_id: str,
    outcome: "retain" | "demote" | "archive",
    review_rationale: str,
    owner_id: str,
    core_review_after: str | None = None,
)
```

`owner_id` identifies the reviewing agent; it need not match the entity owner and does not transfer ownership. `core_review_after` is an absolute canonical timestamp. Retain requires a future timestamp no more than 30 days ahead. Demote/archive reject a supplied next timestamp.

- `retain` updates last-review time/by/rationale and next review without changing content, owner, status, or relations.
- `demote` sets `is_core=False`, removes derived `#core`, clears next review, preserves provenance/reason/exit/detail IDs, and leaves the memory active/searchable.
- `archive` uses existing archival semantics and relation expiry while preserving exact-ID retrievability and historical fields.
- Repeated demote/archive returns a structured no-op; retain against non-core/archived state rejects.
- Review metadata changes directly, without a new full SCD content version, event, or request.

Register the tool in MCP, daemon dispatch, mutating-tool classification, protocol write-tool classification, documentation, and tests.

### Consolidation

Extend single and bulk consolidation with explicit core/lifecycle/detail parameters. If any resolved parent is active core and `is_core` is omitted, reject. Explicit core output requires complete validation; explicit normal output may archive core parents. Prospective sizing removes parents archived by the same transaction and adds the result. Bulk evaluation is sequential within one transaction and all-or-nothing. Track A cannot bypass core checks through internal `commit_consolidation`. A `supersedes` edge alone never frees capacity while the old core remains active.

### Detail relations

For each full UUID, require an existing shared non-core memory, active or archived; require its canonical title and UUID in content; create `detail --elaborates_on--> core` through the existing relation service inside the memory transaction. Relation quality rules remain active. Any failure rolls back memory and all edges; exact duplicates remain no-ops.

## 5. Bootstrap and hook redesign

Move rendering into the canonical service and add an internal read-only daemon dispatch method returning the already-rendered digest. This eliminates CLI renderer drift, `search_memory(limit=20)`, N+1 content fetches, and project semantic search. It need not be public MCP but must be classified as a read method.

Simplify `saltmdb-cli bootstrap-digest`: remove project keywords/limit, core limit, semantic-search flags, project search, and parallel body fetches; print the canonical daemon response. Simplify Bash/PowerShell hooks to invoke only this command—no CWD extraction or project-name derivation.

Validate every active core for shared scope, lifecycle, per-memory size, detail declaration, count, and rendered total. Emit all valid cores in canonical order. On corruption, emit only a bounded `<saltmdb-digest><core-bootstrap-error>…` report containing violations, totals, compact inventory, an omitted-count marker if needed, and a rebalancing instruction. Never emit partial core content beside the error.

## 6. Documentation and usability

Update `README.md`, `AGENT_GUIDE.md`, MCP descriptions, hook examples, and hook README with:

- the “must know before natural search” eligibility test;
- valid/invalid examples and temporary lifecycle;
- exact limits/counting semantics and failure examples;
- autonomous rebalance workflow;
- review cadence and overdue blocking boundary;
- minimum independently actionable content;
- when/how to create normal detail memories;
- canonical title + full UUID references and relation direction;
- archived-detail lookup behavior;
- explicit consolidation behavior;
- global shared-only core and absence of background requests/events.

Installed/global SALTMDB skills and templates must also remove obsolete private-core and permanent-law guidance. Because they are outside this repository, update them only as an explicit deployment step with appropriate authorization.

After schemas/descriptions exist, run a cold Luna evaluation on: urgent crash hazard, stable coding preference, capacity rejection/rebalance, retain review, demote review, archived detail evidence, and core-parent consolidation with omitted `is_core`. Luna must choose correct tools/fields without coaching and must not invent queues, private core, relative review durations, or owner-match restrictions. Revise and repeat until no critical misunderstanding remains.

## 7. Verification plan

Tests must cover:

- schema migration, partial index, persistence across raw updates, SCD archive copies, demotion, single/bulk consolidation, fixtures, and derived `#core` synchronization;
- exact boundaries: 2,500/2,501 content, 20/500 reason/exit, 20/1,000 rationale, 3/4 details, 5/6 cores, 15,000/15,001 rendered characters;
- Unicode code-point counting, emoji, CRLF/CR normalization, private-core rejection, malformed placeholders, rejected core-only fields on normal memories, and preservation of omitted update fields;
- capacity rejection precedence, zero side effects, complete inventory, bypass resistance for ID/title/skip paths and consolidation, and concurrent admissions;
- 14-day default, 30-day maximum, canonical timestamps, equality-at-due boundary, ordering, overdue write boundaries, retain/demote/archive, reviewer identity independence, repeat no-ops, invalid retain rejection, and non-injection of rationale;
- active and archived detail memories, missing/short/private/core details, textual-reference validation, atomic multi-edge rollback, duplicate relation no-op, replacement/clearing, and archived exact-ID retrieval;
- consolidation explicitness, prospective parent removal, bulk rollback, Track A bypass prevention, and supersession not freeing capacity;
- core-only bootstrap, absence of project/events/requests, raw and consolidated active cores, archived exclusion, no pagination limit, canonical ordering, admission/render-size identity, bounded fail-closed output, no partial leakage, empty digest, and simplified hooks.

Run the full repository gates:

```bash
uv run python -m unittest discover -s tests
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run bandit -r src
uv run deptry .
```

Manually inspect a representative five-core digest and record characters, UTF-8 bytes, approximate tokenizer count if available, and absence of harness spill/truncation warnings.

## 8. Review, rollout, and acceptance

### Before implementation

1. Have Claude review this plan against current source.
2. Specifically review transaction boundaries, migration compatibility, every write bypass, Track A, relation atomicity, daemon protocol registration, hook behavior, and coverage.
3. Incorporate review findings into the plan.
4. Do not implement until explicitly authorized by the user.

### Live-data transition

The current database is invalid under the proposed rules. Do not automatically infer which memories survive and do not use direct SQL.

1. Back up through the supported mechanism.
2. Start the new version once so schema/tools exist.
3. Expect fail-closed bootstrap inventory for legacy cores.
4. Use SALTMDB tools to demote legacy cores until no malformed active core remains.
5. Agents autonomously curate the most urgent current items.
6. Promote at most five concise cores with complete lifecycle/detail data.
7. Confirm rendered size is at most 15,000 characters.
8. Verify `bootstrap-digest` contains every selected core in full.
9. Start a fresh agent session and verify no spill file or truncation warning.

No normal-product over-limit recovery mode is required because the live set will be tidied before rollout. Any later invalid state is corruption and fails closed.

### Acceptance criteria

Complete only when every mutation path enforces all three limits; every core is shared, actionable, reviewable, and temporary; overdue reviews block only expansion; rejection has zero side effects; bootstrap injects every valid core and nothing else; malformed state never yields a partial set; archived detail IDs remain retrievable; docs/schemas disclose every rule; Luna operates the interface correctly; Claude findings are resolved; full verification passes; and changes are committed descriptively.

## 9. Explicit assumptions and deferred work

- `is_core` remains the writable source of truth; `#core` remains derived.
- Active means `status != "archived"`.
- Existing exact-ID retrieval of archived entities and exact-relation idempotency remain supported.
- No tokenizer dependency is added; 15,000 characters is the conservative proxy.
- Internal audit events may remain for unrelated mechanisms, but bootstrap does not inject them and this feature creates no review/request events.
- Existing 150-character title limit and Track A review-token system remain unchanged.
- The Viewer is not expanded in the first implementation; MCP and bootstrap are authoritative.
- A future explicit `update_memory`/patch API should replace opaque same-title overwrite behavior, especially for weaker models, but is intentionally outside this change.
