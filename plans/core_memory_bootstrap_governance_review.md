# Review: Core-Memory Bootstrap Governance Plan (against current `rework` source)

Reviewer: Claude. Target: `plans/core_memory_bootstrap_governance_detailed.md`.
Scope: transaction boundaries, schema compatibility, Track A interactions, daemon protocol
registration, test coverage — per the plan's own §8 review request.

**Verdict:** The plan's factual evidence checks out against current source (verified the 20 live
core memories, the `--core-limit 20`, the store_memory bypass paths, the consolidation
is_core-inheritance bug, the disposition/relation idempotency claims — all confirmed by reading
the actual code, not taking the plan's word for it). The architecture is sound and buildable with
the existing patterns (`_add_column_if_missing`, `write_transaction_retrying`,
`WRITE_TOOLS`/`MUTATING_TOOLS`/`DISPATCH_TABLE`). Six concrete gaps need a decision before
implementation; two items in the plan are already done/moot and can be dropped to save effort.

---

## Must-resolve gaps

### 1. Track A's "elaborate" disposition is an ungoverned back door into core detail-linking

`disposition_service.py`: `_CORE_SAFE_DISPOSITIONS = ["distinct", "supersede", "elaborate"]`.
When any ordinary `store_memory` call gets flagged against a candidate that happens to be an
active core, and the caller picks `elaborate`, `commit_disposed_write` creates
`output_entity_id --elaborates_on--> target_id` (relation_service.py `commit_disposed_write`,
elaborate branch) completely independently of `core_detail_memory_ids`, the 3-detail cap, and the
"core content must mention the detail's title/UUID" rule (plan §2 items 34–39). Any agent storing
an unrelated memory that fuzzy-matches a core memory can attach itself as a "detail" of that core
without the core ever declaring it, without the 3-max cap applying, and without updating
`core_detail_memory_ids`.

This is a real, already-shipped code path (not hypothetical) that the plan's detail-linking model
doesn't mention at all. Needs an explicit decision: either (a) exclude core targets from the
`elaborate` disposition entirely (drop it from `_CORE_SAFE_DISPOSITIONS`, leaving only
`distinct`/`supersede` for core candidates — the plan's model already routes deliberate
detail-linking through `store_memory`'s new `detail_memory_ids` param, so Track A's own
`elaborate` path has no remaining legitimate reason to target a core), or (b) explicitly declare
these edges out-of-band and out of the 3-cap's scope, documented as such. (a) is simpler and
matches "core declares its own details" as the sole source of truth.

### 2. Consolidation's is_core-parent check has a TOCTOU gap the plan's own transaction-order rule should close but doesn't call out

`relation_service.py:809-817` computes `is_core_val` (today: silent inheritance; under the new
plan: the reject-if-omitted check) by querying `conn` **before** `_do_commit`'s transaction opens
— it is not part of the existing TOCTOU revalidation block at `_do_commit` (lines ~892-905) that
re-checks parent `content_hash`/`status` against `observed_state` right before the destructive
writes. Plan §4 step 7 ("repeat authoritative lifecycle, detail, overdue, and capacity validation
inside the write transaction before mutation") states the right principle for `store_memory`, but
`commit_consolidation` needs the identical fix threaded through its *existing* revalidation
mechanism — the is_core-parent detection and the capacity precheck must move into (or be
re-verified inside) `_do_commit`, using the same observed-state pattern the cohesion gate already
uses, not just re-run once before `write_transaction_retrying` the way the current buggy
inheritance check does today. Worth naming explicitly in §4's Consolidation subsection so the
implementer doesn't reproduce the existing TOCTOU shape while fixing the inheritance bug.

### 3. `review_core_memory`'s `archive` outcome collides with `archive_memory`'s existing owner check

`lifecycle.py archive_memory`: `if owner_id and existing_owner and existing_owner != owner_id:
return "Error: ... owner mismatch."` The plan explicitly requires `review_core_memory`'s
`owner_id` to identify the *reviewing* agent and states it "need not match the entity owner" (§2
item 63, §4 review_core_memory section) — but then says `archive` "uses existing archival
semantics." Calling `archive_memory(entity_id, owner_id=reviewer_id)` as-is will incorrectly
reject in the ordinary case where the reviewer isn't the original author. The plan needs to pick
one explicitly: (a) the review tool's archive path never forwards `owner_id` to the underlying
archive call (pass `None`), (b) add a bypass parameter to `archive_memory`, or (c) a small
internal helper that shares `_do_archive`'s body without the ownership gate. Recommend (a) — it's
the smallest change and consistent with decision #4/#5 (no ownership veto over maintenance).

### 4. `core_detail_memory_ids` cap vs. actual graph edges can diverge

Because of #1, and because relations are the graph's ground truth while `core_detail_memory_ids`
is a separate JSON declaration, the plan should state explicitly that the "≤3 details" cap and all
detail-validation rules (§2 items 34–41, §4 Detail relations) apply only to the declared JSON
list, and that this is the sole channel the governance service manages/validates — not "however
many `elaborates_on` edges happen to point at a core in the graph." Once #1 is fixed this mostly
resolves itself, but it should still be one explicit sentence in the plan so there's no ambiguity
about which representation is authoritative.

### 5. New free-text lifecycle fields aren't covered by the existing redaction/normalization pipeline

`write.py`'s `store_memory` runs `redact_secrets` → `validate_memory_input` →
`auto_format_markdown` (which also does the CRLF→LF normalization the plan relies on) only over
`content`/`title`. The plan's new `core_reason`, `core_exit_condition`, and
`core_review_rationale` fields are free text an agent will type in a hurry during an incident —
exactly the kind of field a pasted token/credential ends up in. The plan should state explicitly
whether these three fields go through `redact_secrets` (recommended) and LF-normalization before
their 20/500/1000-char boundaries are measured and before persistence.

### 6. Loose boolean coercion on `is_core` doesn't match the plan's own "don't silently swallow ambiguity" philosophy

`relation_service.py:817`: `is_core_val = 1 if is_core in (True, 1, "true", "1", "True") else 0`
(same pattern in `write.py`). Any other value — `"yes"`, `"False"` (the string), a stray int —
silently becomes `False`. The plan is explicit elsewhere (decision requiring core-only fields on a
non-core write to be rejected rather than ignored) that ambiguous input should error, not be
guessed at. Recommend the plan note that the governance service's `is_core` parsing should reject
unrecognized values with a clear error instead of reusing this existing loose-coercion helper
as-is, or explicitly declare that out of scope and leave the existing coercion behavior alone.

---

## Already true / drop from the work list

- **Plan §2 item 43 / §5 ("remove event injection, pending-request material" from bootstrap):**
  read `cli.py`'s `cmd_bootstrap_digest` in full — it only calls `search_memory` twice (core +
  project keywords) plus a thread-pooled `fetch_full`. There is no event/request-queue code path
  in the current bootstrap CLI to remove. Only `--project-keywords`/`--project-limit`/
  `--core-limit`, the project search, and the N+1 `_fetch_full` thread pool are real deletions.
  Drop the phantom event/request cleanup from the task list.

- **Plan §7 ("fixture schemas and test DDL" need updating):** all 47 test files that touch the DB
  call `schema.init_db()`; grep found zero test files with their own duplicated
  `CREATE TABLE entities` DDL. New nullable columns propagate to every test DB automatically via
  `_add_column_if_missing`. Soften or drop this action item — there's no duplicate DDL to hunt
  down.

---

## Confirmed accurate, no change needed (spot-checked against source)

- `review_core_memory` registration touch points are exactly: `daemon/protocol.py WRITE_TOOLS`,
  `daemon/dispatch.py DISPATCH_TABLE` + `MUTATING_TOOLS`, and a new `@mcp.tool()` in
  `mcp/tools.py` forwarding through `_backend_or_raise().call(...)` — matches the existing
  7-tool pattern exactly (verified against `store_memory`/`archive_memory`/`commit_consolidation`
  wiring).
- The internal read-only bootstrap-digest RPC the plan wants (§5) is realizable exactly as
  described: add to `dispatch.DISPATCH_TABLE` (not `MUTATING_TOOLS`) and to `protocol.READ_TOOLS`;
  no `mcp/tools.py` entry is required since `cli.py` already calls the daemon directly via
  `daemon_client.call(db_path, tool_name, kwargs)`, bypassing the MCP adapter layer entirely.
- `bulk_commit_consolidation` is genuinely sequential-within-one-transaction, all-or-nothing
  (confirmed by reading `_write`'s loop and the shared `write_transaction_retrying` call) — the
  plan's capacity governance for bulk consolidation needs to load/recompute active-core state from
  `conn_arg` on each loop iteration (SQLite sees its own uncommitted writes within one connection),
  not from a Python-side count taken once before the loop.
- Track A already refuses `consolidate` against a core candidate (`_NON_CORE_DISPOSITIONS` is the
  only list containing `"consolidate"`) — the plan's "keep Track A's ordinary consolidate
  restriction against core candidates" (§2 item 55/59) is already true today and needs no new
  code, just preservation.
- `_add_column_if_missing` and the entities `INSERT ... ON CONFLICT DO UPDATE SET x = COALESCE(?,
  entities.x)` pattern already used for `is_core`/`owner_id`/`context_id`/`memory_type` is directly
  reusable for the new lifecycle columns — no new persistence pattern needs inventing.
- Live DB currently has exactly 20 active core memories (re-verified live via `search_memory(is_
  core=True)` during this review), matching the plan's cited evidence.

---

## Minor note

The plan's proposed capacity-rejection shape `{"status": "REJECTED", "error_code": "CORE_CAPACITY_
EXCEEDED", ...}` sits next to two existing but different `status` conventions on the same
`store_memory` return type: the internal-only quality-gate dict uses `"REJECT"` (one character
off, never surfaces to callers today), and Track A's own dict returns use `"REVIEW_REQUIRED"`/
`"REVIEW_STALE"`. Not a defect, but worth the plan/docs spelling out the full enumerated set of
`store_memory`'s possible top-level `status` values in one place so agents parsing results don't
conflate a terminal capacity rejection with a token-bearing review state at a glance.
