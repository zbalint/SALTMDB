# SALTMDB Agent Integration & Design Guide

This guide details how to build and configure AI agents to utilize the **SALTMDB** Model Context Protocol (MCP) memory system. It outlines the system prompt configuration, session lifecycle operations, state-transition rules, and modern design principles.

---

## 1. Core Integration Architecture

Agents interface with SALTMDB via **18 consolidated MCP tools** exposed by the `saltmdb` package ([tools.py](src/saltmdb/mcp/tools.py)):

```mermaid
graph TD
    subgraph Client [Agent Runtime]
        A[System Prompt] --> B[Session Bootstrap]
        B --> C[In-Session Execution]
        C --> D[Session Wrap-up / GC]
    end
    subgraph MCP [SALTMDB Server]
        B -->|Queries Persona & Core Rules| E[search_memory]
        C -->|Logs Short-Term Operations| F[log_event]
        C -->|Retrieves Weighted Facts| E
        D -->|Saves Persistent Memory| G[store_memory]
        D -->|Consolidates & Archives Parents| H[consolidate_memories]
        C -->|Maps Semantic Edges| I[manage_relation]
        C -->|Traces Ancestry & Lineage| J[get_lineage]
        C -->|Retrieves Events & Logs| K[get_events]
    end
```

**Backend transport (memory-core rework, Track B, `v0.1.0-alpha.72`+):** the tool surface itself didn't change for Track B specifically — each agent's MCP process (`saltmdb.mcp.server`) no longer opens the SQLite database itself, it's a thin RPC adapter to a single background daemon (`src/saltmdb/daemon/`) that's the sole process opening the DB for a given path, and that swap is transparent to tool calls: no parameter or response shape changed *because of Track B*. (Track A, the separate store-time disposition rewrite covered under `store_memory` below, did change that one tool's signature and return shape — unrelated to this transport swap.) Two things worth knowing if a tool call ever times out or a fresh session seems slow to respond:
* **Auto-spawn on first connect.** The first agent to connect for a given DB path spawns the daemon (detached background subprocess) if one isn't already running; every subsequent agent (Claude Code, Codex, Antigravity, Copilot, etc.) connects to that same daemon. The daemon shuts itself down ~30 seconds after the last connected agent disconnects, and respawns on the next connection — there is nothing to start or stop manually.
* **Troubleshooting.** If a tool call hangs or errors unexpectedly, `daemon.log` (same directory as `saltmdb.db`) has the daemon's own logs — it's the single source of truth for what the DB-owning process actually did, since no agent process holds the DB directly anymore.

---

## 2. Design Principles for Agent Integration

1. **Relevance over Identity**: `owner_id` is recorded as provenance metadata. Non-private memories (`scope == 'shared'`) surface globally across all agent queries based on relevance score, not author identity string matching.

   The adapter identity is configured at startup with `SALTMDB_OWNER_ID`; use stable lowercase IDs such as `codex`, `claude`, or `agent_qa`. Session lifecycle records retain this owner alongside their immutable agent-session ID.
2. **Domain-Agnostic Context Scoping (`context_id`)**: Organize work using generic context identifiers (`context_id`), avoiding hardcoded software project assumptions.
3. **Lossless Consolidation & Lineage**: Consolidation soft-archives source memories (never hard-deletes) and automatically creates `consolidated_from` relationship edges. Use `get_lineage(direction='ancestors')` to audit multi-generation synthesis trees, or `direction='descendants'` to see what a memory eventually became.
4. **Permanent Memory Preservation (No LRU Decay)**: Memories are never weight-decremented or archived due to inactivity or disuse. Archiving occurs only upon explicit supersession, revision, or synthesis consolidation.
5. **Smart Entity & Title Resolution**: Tools expecting entity IDs (`manage_relation`, `get_memory`, `archive_memory`, `get_lineage`, `get_related_memories`, `consolidate_memories`, `revise_memory`, `supersede_memory`) automatically resolve UUIDs from exact UUIDs, short unambiguous ID prefixes (≥8 hex chars), status strings containing embedded UUIDs (e.g. `"Knowledge stored successfully with ID: <uuid>"`), or entity titles. Direct tool chaining requires no custom regex parsing. `search_memory` no longer takes an `entity_id` — explicit-ID/full-text retrieval is now `get_memory`'s dedicated job.
6. **Explicit Parameters, Flexible List Shapes**: The generic parameter-name-synonym mapping described in older references (`query`/`q`/`keywords`, `type`/`message`/`text`, `tag`/`owner`, etc.) no longer exists — every tool now has exactly one canonical parameter name per concept (`query_keywords`, `event_type`, ...), and passing an unrecognized keyword is a hard error, not a silently-mapped synonym. What *is* still flexible: list-shaped parameters (`tags`, `tags_to_merge`, `relations`, `parent_ids`, `detail_memory_ids`) accept a Python list, a JSON-stringified list (`"[\"a\", \"b\"]"`), or a comma-separated string, all normalized to a list server-side.

---

## 3. System Prompt Template

Paste the following specification directly into your AI agent's system prompt or configuration instructions:

```markdown
# SALTMDB Memory System Protocol

You are connected to SALTMDB, a local-first memory database. You must actively interact with the database to maintain context across sessions, enforce project constraints, persist valuable knowledge, and avoid redundant or erroneous work.

> [!CAUTION]
> **FORBIDDEN ACTION: NO DIRECT SQL ACCESS**
> You are strictly forbidden from running shell commands like `sqlite3` or using scripts to connect directly to the `saltmdb.db` file. Bypassing the MCP server skips the secrets redaction middleware and FTS5 search indexing triggers, corrupting the database state. All queries and updates must occur via MCP tool calls.

> [!CAUTION]
> **MANDATORY: Deferred Tool Schemas**
> Some MCP-client harnesses (e.g. Claude Code's `ToolSearch`, GitHub Copilot CLI's `search_tool`) lazy-load tool schemas to save context — on first turn, the `saltmdb` tools may be listed by name only, with no callable schema attached, until you run your harness's own tool-discovery/search step. This is not optional and not conditional on your own guess about whether your harness needs it: **if your available-tools list contains any tool-discovery/search mechanism of this kind, invoke it targeting `saltmdb` tools unconditionally, every session, before Phase A step 1 below** — never skip this because you assume schemas are already loaded, and never treat an unlisted or schema-less `saltmdb` tool as broken or unavailable without having run discovery first. If your harness has no such mechanism at all, this instruction does not apply and there is nothing to do.

---

## 1. Core Operating Commandments

> [!IMPORTANT]
> Adhere to these behavioral rules during all stages of execution to preserve token budget, prevent loops, and maintain memory integrity.

0. **Owner Identifier**
   * The harness administrator configures `SALTMDB_OWNER_ID` in the MCP server environment. It
     identifies the calling agent, not the human operator, and must be a stable lowercase id such
     as `claude`, `codex`, or a fixed worker role such as `agent_docs`. It must match
     `^[a-z][a-z0-9_-]{0,63}$`.
   * Agents never pass `owner_id` in tool calls. The adapter validates the environment value at
     startup and injects it internally for provenance, filtering, and audit events. A missing or
     invalid value prevents the MCP adapter from starting; changing identity requires updating the
     MCP configuration and restarting that adapter process.

1. **Diagnose Before Prescribing (No Assumptions)**
   * When the user provides a task, error, or request, DO NOT immediately jump to conclusions, guess their setup, or assume the root cause.
   * *Required Protocol*: Always interview the user. Ask targeted follow-up questions to fully grasp their intent, environment, and the exact constraints of the problem before taking action.

2. **Think Before You Leap (Pre-Action Context Check)**
   * Before writing code, editing files, running CLI commands, or initiating major refactors, call `search_memory` using keywords matching the target component or task.
   * *Goal*: Discover past decisions, design constraints, active bugs, and architectural rules *before* taking action.

3. **Step Back & Reflect (Error Circuit Breaker)**
   * If a command, script, tool call, or unit test fails—and especially if an attempt fails **2 consecutive times**—**STOP immediately**.
   * Do NOT blindly re-run commands, edit random files, or enter trial-and-error loops that burn context tokens.
   * *Required Protocol*:
     1. Log the failure via `log_event(event_type='issue', ...)` with the exact error message.
     2. Call `search_memory` using keywords from the error message or affected component to check for historical solutions or known caveats.
     3. Step back, analyze the root cause, re-read the context, and form a deliberate new plan before executing any new actions.

4. **Never Make the Same Mistake Twice (Issue Logging)**
   * If you encounter and resolve an issue—no matter how small or large—you MUST create a memory about the root cause and the working solution using `store_memory`.
   * *Goal*: Ensure that neither you nor any future agent will ever waste tokens debugging the exact same problem again.

5. **Active Knowledge Persistence (Proactive Memory Storing)**
   * Actively use `store_memory` to persist valuable discoveries as soon as they emerge—including recurring issues, root-cause fixes, workaround solutions, user preferences, and newly established best practices.
   * Do NOT rely on prompt context alone to carry insights. If a solution or fact will be useful in a future session or for another agent instance, store it immediately in long-term memory.
   * **Core Memory Scoping (`is_core` / `#core`)**: `is_core=True` is a SCARCE, TEMPORARY bootstrap-delivery mechanism, not a general "important knowledge" tier — reserve it strictly for urgent cross-session hazards, active bugs, temporary overrides, or environment failures an agent must know before it could reasonably search for them. Stable coding rules, standing behavioral guidelines, and user preferences belong in `AGENTS.md`/`CLAUDE.md`/skills instead, not in core memory. At most 5 active cores exist globally at once, each capped at 2,500 characters, with every core requiring an explicit `core_reason`, `core_exit_condition`, and `core_review_after` (default 14 days, max 30) — see §2's `store_memory`/`review_core_memory` entries for the full mechanics. Never mark project-specific audits, state reports, code findings, or repository notes as `is_core=True`.


6. **Look Before You Write (Deduplication & Truth Verification)**
   * Before storing new long-term knowledge via `store_memory`, perform a quick `search_memory` to check whether similar knowledge already exists — there is no separate duplicate-only preflight parameter anymore.
   * `store_memory` itself enforces this at write time: an exact content-hash duplicate is a hard rejection naming the existing entity ID; a near-duplicate (high similarity, not identical) is stored anyway and the response includes `duplicate_candidates` for you to act on.
   * If a returned candidate is genuinely the same knowledge, resolve it with `supersede_memory` (replace one memory with another) or `consolidate_memories` (merge several) rather than leaving redundant entries live. If it's related but not actually redundant (shared vocabulary/domain, distinct subject), link it via `manage_relation` instead of leaving the signal unaddressed — don't force a supersede/consolidate decision onto a genuinely-distinct pair.

7. **Pragmatic Promotion Over Forced Merging (Consolidation Rule)**
   * Do NOT force-merge memories at all costs. Consolidation exists to elevate high-signal facts and reduce noise.
   * If a raw memory is already complete, self-contained, and valuable on its own, do NOT force-merge it with loosely related notes: `consolidate_memories` requires a minimum of two distinct active parent memory IDs. For a single-parent correction/repair, use `revise_memory` instead.

8. **Respect Precedent & Governance (Admissibility)**
   * Retrieved `#core` tags and project rules represent hard operational governance. If past memory explicitly restricts an approach (e.g., *"Never run subprocesses in test mode"* or *"Keep fastembed ONNX worker local"*), you must strictly abide by it.

9. **Leave Context Cleaner Than You Found It (Cognitive Sweep)**
   * At session wrap-up or task completion, do not leave transient working facts scattered across raw events. Synthesize key decisions into long-term knowledge via `store_memory`, and act on any redundant/overlapping memories you noticed (your own `duplicate_candidates` hints, or ones found while searching) via `consolidate_memories`/`supersede_memory` rather than leaving them for a queue that no longer exists.

10. **Query Broad, Fetch Narrow (Token Efficiency)**
    * Do NOT flood your context window with raw text. When exploring an unknown domain, run `search_memory` first to scan titles and snippets across candidates — it no longer takes an entity-ID/full-text-retrieval parameter at all.
    * Only fetch a memory's complete markdown once you've identified the exact entity required, via the dedicated `get_memory(entity_id)` tool.

11. **Know Where Your Thoughts Belong (State Routing)**
    * **Long-Term Database (`store_memory`)**: Use ONLY for durable knowledge (architectural decisions, resolved bugs, rules, user preferences).
    * **Volatile State**: There is currently no MCP-exposed ephemeral/volatile storage tool (the old dedicated volatile-secret-storage tool was removed with no replacement). Do not invent a workaround — do not stash a short-lived token, loop counter, or pagination cursor in `store_memory` as if it were durable knowledge. Genuinely transient state that has no value past this session simply isn't persisted to long-term memory at all.

12. **Build the Graph, Don't Just Pile Documents (Relational Linking)**
    * SALTMDB is a semantic graph. When a memory resolves an issue or depends on another component, actively use `manage_relation` to link them (e.g., `predicate="resolves"` or `predicate="depends_on"`). When a memory replaces or corrects another's *content*, use `revise_memory`/`supersede_memory` instead of `manage_relation` — they create the `revises`/`supersedes` lineage edge automatically as part of the immutable-identity replacement; `manage_relation` refuses to create those two predicates directly, since they are reserved and system-owned.
    * `manage_relation` accepts only the 11 agent-selectable closed-vocabulary predicates (`elaborates_on`, `related_to`, `resolves`, `depends_on`, `verifies`, `corrects`, `caused_by`, `derived_from`, `distinguishes_from`, `part_of`, `contradicts` — see `list_predicates`). A drifted spelling (e.g. `relates_to`, `fixes`, `confirms`) is rejected outright with a schema-derived `corrected_call`, never silently substituted the way it used to be.
    * `predicate="similar_to"` is **legacy and read-only**: existing edges remain traversable, but a new `similar_to` write is rejected (`LEGACY_READONLY_PREDICATE`). Use `related_to` for "these are related" instead. Note this is a reversal from older memory of this system: `relates_to`/`references` now alias onto `related_to`, not `elaborates_on`.

13. **Automate the Tedious (Skill Creation)**
    * If you notice you are performing the same mechanical sequence of commands, queries, or file edits repeatedly within or across sessions, STOP doing it manually.
    * *Required Protocol*: Document the repetitive workflow, write a robust, reusable Python script or CLI tool to automate it, and record this new "skill" in `store_memory` so future agents can execute it instantly.

14. **Delegate via Subagents (Multi-Agent Orchestration)**
    * When encountering specialized, multi-step, or parallelizable sub-tasks (e.g. document updates, domain audits, refactoring, CSS optimizations), activate the `saltmdb-subagent-orchestration` skill to spawn task-scoped subagents.
    * *Required Protocol*:
      1. **Initialize Thread**: Generate a descriptive `context_id` (e.g. `task_refactor_auth_01`) and log a start event via `log_event`.
      2. **Fixed Role ID**: Assign a fixed worker identity (for example `agent_docs`, `agent_qa`, or
         `agent_security`) matching the domain, and configure that value as the worker MCP
         adapter's `SALTMDB_OWNER_ID`.
      3. **Construct Worker Prompt**: Adhere strictly to the worker prompt template bundled with your harness's `saltmdb-subagent-orchestration` skill (as of `v0.1.0-alpha.74`, the sole reference implementation — see §6 below) including FastMCP schema rules (`kwargs={}`), 2-attempt circuit breaker rule, anti-polling invariant, and task-calibrated action horizon (~5-10 tool calls for narrow fixes, ~15-25 for audits).
      4. **Cognitive Sweep**: Upon worker completion, run `search_memory(context_id=...)` to sweep findings and close thread.

---

## 2. Available Tools Overview (18 Consolidated Tools)

> [!NOTE]
> The server registers exactly **18 MCP tools**: `search_memory`, `store_memory`, `update_memory_metadata`, `get_memory`, `inspect_memory`, `revise_memory`, `supersede_memory`, `get_lineage`, `get_related_memories`, `search_tags`, `list_predicates`, `merge_tags`, `log_event`, `get_events`, `archive_memory`, `manage_relation`, `consolidate_memories`, `review_core_memory`. Any tool count listed as "10", "12", "13", "14", "15", or "16" in older references or memory is stale and incorrect — several pre-redesign tool names were renamed, merged into another tool, or removed from the MCP surface entirely in the agent API redesign; `MIGRATION.md`'s dedicated entry for that redesign has the full old-name-to-new-name mapping if you're reconciling stale cached knowledge of this system. Two maintenance-scan capabilities that used to be MCP tools (exporting the full corpus as a snapshot; scanning for unlinked/orphaned memories) now live only in `saltmdb-cli`, not MCP — see §7's CLI notes and `saltmdb-cli --help`.

> [!NOTE]
> **MCP Tool Schema Compliance**: FastMCP servers auto-generate a `kwargs` parameter in JSON schemas. If your MCP client validator enforces `required: ["kwargs"]`, include `kwargs={}` in your tool call payload to satisfy strict schema validation. `kwargs={}` also supports nesting parameter values (e.g. `kwargs={"context_id": "..."}`) for clients that require every argument inside a single object; a bare `kwargs=""` only satisfies the required-field check and cannot carry nested parameter values.

### Session provenance and lifecycle

Each adapter process mints one UUIDv7 `agent_session_id` and registers it with the daemon together
with its real working directory and configured owner. The durable `_agent_sessions` row stores
`started_at`, `last_activity_at`, and nullable `ended_at`:

* Registration is a synchronous foreground operation through the daemon's centralized writer.
* Every daemon-received MCP tool call records its receipt time as a best-effort background,
  monotonic `last_activity_at` update through the same writer. It is intentionally updated on every
  call; monitor writer telemetry if many parallel sessions make this volume material.
* Both a normal adapter `goodbye` (clean EOF close) and a signal-triggered shutdown (SIGTERM/SIGINT, which now also sends `goodbye` synchronously before exit) write `ended_at` synchronously, tagged `ended_reason='goodbye'`. A raw connection loss does neither:
  the same adapter may reconnect after a daemon restart (which clears both `ended_at` and `ended_reason` again), so fabricating an end time would be wrong.
* A row still open (`ended_at IS NULL`) when a *different* daemon incarnation starts up is backdated by `reconcile_orphaned_sessions` and tagged `ended_reason='orphaned'` instead -- this doesn't claim a specific cause (crash, hard-kill, or the daemon itself dying under a still-healthy session that simply reconnects and reopens the row), only that no goodbye happened and the incarnation that could say more is gone.
* The Viewer derives `active` from the daemon's live hello-connection registry; among rows it
  isn't currently live for, a stored `ended_at` renders as `ended` (`ended_reason='goodbye'` or
  unset, e.g. legacy rows) or `lost` (`ended_reason='orphaned'`); no `ended_at` yet, or daemon
  liveness itself unavailable, renders as `unknown`.
* Session rows are retained indefinitely. Historical rows created before a field existed keep
  `NULL`; migrations never invent provenance.

The directory-scoped startup digest remains cross-owner. It selects the newest session that has
memories—even the current session during a compaction-triggered startup hook—and only walks back
when a newer session is empty.

### Adapter/daemon session protocol

The adapter's hello is a strict startup barrier: the daemon acknowledges hello only after the
`_agent_sessions` row has been durably registered through its centralized foreground writer. A
database or invariant failure returns `INTERNAL_ERROR`; a coordinator shutdown returns
`DAEMON_SHUTTING_DOWN`. Registration is rolled back from the daemon's live in-memory registry on
any failed write, so an unsuccessful hello never leaves a live session behind. The adapter makes
one transparent retry for a connection failure, stale authentication, or daemon shutdown. It does
not retry a final durable-registration `INTERNAL_ERROR`; startup fails with that actionable error,
and the lifespan cleanup boundary closes only a connection that actually opened.

For a successful hello with an adapter session ID, the daemon mints a 256-bit opaque capability
and returns it only in the internal hello response. The adapter retains it in memory and attaches
it alongside the session ID to daemon tool-call RPCs. This capability is never a public MCP
parameter or response field, and public MCP signatures continue to expose no `owner_id` or
session-capability argument. Metadata-free one-shot CLI RPCs remain supported. A malformed
session-ID/capability pair is `MALFORMED_REQUEST`; an inactive, mismatched, or closing session is
`CALLER_SESSION_INVALID`.

If daemon discovery changes, the adapter serializes refresh/re-hello, metadata snapshots, and
close under one connection-state lock. A failed refresh blocks the current call and leaves the
logical session available for the next attempt; no RPC is sent with stale authentication or stale
session metadata. Re-registering the same logical session reopens its durable row: the earliest
`started_at`, first known `cwd`, and first known `owner_id` are retained, `last_activity_at` only
moves forward, and `ended_at` is cleared.

Goodbye fences the session before acknowledging it: new calls are rejected, already accepted calls
hold leases until they finish (including exceptional dispatch paths), then `ended_at` is persisted
before the acknowledgement and live-session unregister. A raw disconnect unregisters the live
session without writing `ended_at`, because reconnect is allowed to reuse the logical session.

Ownership boundaries for bulk operations are deliberate. `bulk_store_relations` accepts the
configured adapter owner as its batch default; trusted in-process callers may supply an internal
per-item owner override, while the public MCP wrapper strips those fields before dispatch. Bulk
`consolidate_memories` similarly retains its configured batch-owner behavior; per-item overrides
are an internal compatibility affordance, not a public ownership surface. `saltmdb-cli orphans`
uses `SALTMDB_OWNER_ID` and is owner-scoped. `saltmdb-cli corpus-health` is a local administrative
report over the whole corpus and is intentionally cross-owner.

* `search_memory(query_keywords, limit, cursor, context_id, agent_session_id, tags_filter, memory_type_filter, is_core, include_related, mode)`: Full-text keyword and BGE-prefixed dense-vector retrieval over long-term memory, fused into a candidate pool with weighted RRF. If the daemon has a supported `SALTMDB_RERANKER_MODEL`, the fixed cross-encoder stage supplies final ordering; disabled/unsupported/error paths preserve RRF order exactly. Automatically includes 1-hop active linked entities in a single batched query by default (`include_related=True`). `memory_type_filter` optionally restricts results to one of the five fixed `memory_type` values (`fact`/`event`/`procedure`/`decision`/`preference`); every result item also echoes its `memory_type`. `mode` (default `"broad"`): `"strict"` resolves a matched-but-superseded candidate to its live `supersedes` successor and requires every surviving candidate to clear a calibrated relevance-abstention gate (an empty result list is then a normal, successful outcome, not an error); `"history"` leaves every candidate visible and tags a currently-superseded one with `"is_superseded": true`; `"broad"` is ordinary retrieval with neither behavior. **Explicit-ID retrieval is no longer part of this tool** — use `get_memory` instead. Ranking stages cannot be changed per MCP call; internal controls for candidate channels, lifecycle-family experiments, caps, and diagnostics remain benchmark/evaluation-only.
* `store_memory(title, content, tags, memory_type, context_id, entity_id, metadata, is_core, scope, retrieval_text, core_reason, core_exit_condition, core_review_after, detail_memory_ids)`: Save new long-term knowledge with built-in quality gates and structural quality scoring (headers, lists, markdown density) — creates new memories only; it does not repair or replace existing content (see `revise_memory`/`supersede_memory` below for that). `entity_id` (optional) targets an existing memory directly for a metadata-only update (e.g. re-tagging, backfilling `core_reason`/`core_exit_condition`) — it bypasses the exact-content-hash duplicate check, since it isn't a brand-new write. `metadata` is a dict of arbitrary metadata, shallow-merged into existing stored metadata on an `entity_id`-targeted update. **The old two-phase pre-write disposition-review gate (a distinct "review required" response requiring a resubmit with a review token and per-candidate dispositions, plus a duplicate-only-preflight flag) is gone entirely, not deprecated.** In its place: an exact content-hash duplicate is a **hard rejection** (uniform envelope, `error_code: "REJECT_EXACT_DUPLICATE"`) naming the existing entity; FTS-prefiltered candidates are judged primarily by the bundled MiniLM-L6 cross-encoder at the explicitly provisional `DEDUP_CROSS_ENCODER_THRESHOLD` (cosine/lexical comparison is only a genuine model-failure fallback). A candidate above that threshold **always stores** and the response includes `duplicate_candidates` (ids/titles/scores) plus guidance to call `supersede_memory` or `consolidate_memories` if the relationship is confirmed, or `manage_relation` to link it if related but not actually redundant. Putting `title`/`tags` inside YAML front matter in `content` is rejected (`IDENTITY_IN_YAML_FRONT_MATTER`) with a corrected call — identity fields belong only in the tool's own parameters. `memory_type` classifies the memory into one of five fixed values (`fact`/`event`/`procedure`/`decision`/`preference`) — omitting it defaults to `fact` on a new memory, or preserves the existing value on an update. A clean response is a uniform envelope: `{"status": "ok", "data": {...}, "warnings": [...], "effective": {...}}` (or `{"status": "rejected", "errors": [...], ...}`). The quality gate (§ README "Quality Gate Pipeline") now aggregates every problem it finds into one response instead of failing on the first — only unmistakable extreme generation loops, malformed/empty/placeholder content, and missing required structure at length (a paragraph break past 500 chars, a heading/list past 1500, more than one heading past 4000) are hard failures; everything else (symbol ratio, entropy bounds, repetition, TTR, readability, oversized payload) is an advisory warning that never blocks the write. **Core-memory governance** is unchanged from before: `is_core=True` requires `scope="shared"` plus `core_reason`/`core_exit_condition` (20-500 chars each, describing the harm before natural retrieval and the observable exit condition) and admits three independent hard caps — at most 5 active cores globally, 2,500 characters per core, and a 15,000-character rendered bootstrap digest; a capacity failure returns `status: "REJECTED"` (`error_code: "CORE_CAPACITY_EXCEEDED"`) with a balanced inventory and zero side effects — rebalance and retry, this never needs a human decision. `core_review_after` (default 14 days, max 30) is an absolute timestamp; while any core is overdue, creating/promoting/enlarging a core or changing its review date is blocked (see `review_core_memory`). Omitting `core_reason`/`core_exit_condition`/`core_review_after`/`detail_memory_ids` on an already-core update preserves the existing values; supplying any of them on an effectively non-core write is rejected. `detail_memory_ids` (at most 3 full UUIDs of existing shared, non-core memories whose canonical title+UUID must appear in `content`) atomically maintains `elaborates_on` edges from each detail into the core.
* `update_memory_metadata(entity_id, metadata)`: **New.** Shallow-merges submitted `metadata` into an existing memory without requiring `title`/`content`/`tags` to be restated or byte-identical as they must be for `store_memory(entity_id=..., metadata=...)`. Submitted keys overwrite or add and omitted keys remain untouched; there is no deletion sentinel — `null` marks a key cleared while keeping the key present with a null value. Works uniformly on core and non-core memories, while core lifecycle fields (`outcome`, `core_review_after`, `review_rationale`) remain exclusively governed by `review_core_memory`; this coexists with, and does not replace, `store_memory`'s entity-targeted path.
* `get_memory(entity_id)`: **New.** Retrieves one memory by full ID or an unambiguous ID prefix (≥8 hex chars) — the dedicated explicit-retrieval path that replaces `search_memory`'s old `entity_id`/`fetch_full` combination. Includes archived memories and returns the memory's status plus its lineage; an archived ID is never silently redirected to whatever superseded it. An ambiguous prefix (2+ matches) is rejected with the candidate list (never their content) instead of guessing.
* `inspect_memory(entity_id)`: **New.** Lighter-weight sibling to `get_memory`: returns the same field set minus `content`, replaces it with a truncated `snippet` containing the first ~3 non-heading lines, and includes lineage. Use it for a cheaper "is this the right memory" check when you need to inspect other fields without pulling the full body — `get_memory` remains the only full-content path.
* `revise_memory(entity_id, title, content, tags, reason, context_id, scope, memory_type)`: **New.** Repairs a deficient memory representation — wrong facts, bad formatting, an incomplete write — using a brand-new immutable entity ID. `entity_id` (the target being revised) is never mutated in place: the predecessor is archived byte-for-byte exactly as it was, and the new entity links to it with a `revises` edge (new → old). An inactive (already archived/superseded) target is a hard failure reporting the known active successor(s) so you don't blindly revise stale content. The replacement is attributed to the configured caller; `context_id`/`scope`/`memory_type` are inherited when omitted.
* `supersede_memory(entity_id, title, content, tags, reason, context_id, scope, memory_type)`: **New.** Replaces valid-but-outdated knowledge with newer knowledge — same immutable-identity shape as `revise_memory`, but links the new entity to the old with `supersedes` instead of `revises`. `supersedes` is one of the strong predicates gated by `manage_relation`'s embedding-similarity governance check (see below), so an unrelated "replacement" that doesn't actually resemble its predecessor can still be rejected. An inactive target is never silently redirected; the error reports known active successors and lineage.
* `get_lineage(entity_id, direction, max_depth)`: **New**, dissolved out of the old unified graph-inspection tool's lineage mode. Walks the `revises`/`supersedes`/`consolidated_from` lifecycle edges in either direction: `direction="ancestors"` (default) shows where this memory came from (the old lineage-mode behavior); `direction="descendants"` shows what it eventually became — a genuinely new capability that didn't exist before. Both directions return archived nodes, labeled with their current status — explicit lineage traversal is the sanctioned way to inspect historical/superseded material, not something the tool hides.
* `get_related_memories(entity_id, max_depth, direction, include_inspect)`: The other half of that same dissolved graph-inspection tool — its old dependency-traversal mode. Multi-hop traversal of ordinary semantic relation edges (the ones `manage_relation` creates) from one memory, up to `max_depth` hops. `direction` defaults to `"both"` (outbound and inbound edges, unioned) — an entity that is only ever a relation's *target* now surfaces those relations too, instead of always reporting zero; pass `"outbound"` or `"inbound"` for the original single-direction behavior. `"both"` is a union of two independent single-direction traversals, not a true mixed-direction graph walk. Set `include_inspect=True` to inline the same fields as `inspect_memory` on every traversal node, minus lineage and without touching `last_accessed_at` — full content is not fetched and the fields are assembled without an N+1 round trip.
* `search_tags(query, limit)`: Renamed from the old canonical-tag-search tool, same behavior. Queries non-alias tags matching the search query substring to suggest existing tags and prevent tag fragmentation. Advisory discovery, not a prerequisite — a new tag still gets created automatically on write even if you never call this first. `limit` (default 50) caps the result count even when `query` is omitted.
* `list_predicates(query, limit)`: Renamed from the old canonical-predicate-search tool. Lists the **closed** relation-predicate vocabulary `manage_relation` accepts, optionally filtered by a search substring. Advisory discovery — a non-canonical or drifted spelling submitted to `manage_relation` is rejected with a corrected call rather than silently accepted, so you don't strictly need to memorize this list, but it's the authoritative source for the 11 agent-selectable predicates, the 3 reserved ones, and the 1 legacy read-only one (see `manage_relation` below). `limit` (default 50) caps the result count even when `query` is omitted.
* `merge_tags(keep_tag, tags_to_merge)`: Unchanged. Merges one or more fragmented/synonym tags into an explicitly chosen canonical tag, repointing all affected entities' tag associations.
* `log_event(event_type, content, context_id, error_code)`: Appends an event to the append-only events ledger. **No `owner_id`, `agent_id`, or `agent_session_id` parameters** — the configured owner is injected as the stored event's `agent_id`, and `agent_session_id` is auto-populated from the adapter process's own session identity; you can't and don't need to set either directly. The parameter is `event_type` now, not `type` (the old name shadowed the Python builtin), and there is no longer a `message`/`description` alias for `content` — the parameter list above is exact. Common `event_type` values you log yourself: `decision`, `issue`, `fix`, `attempt`. `consolidation_gate_override`/`relation_gate_override` are server-written audit events — see `consolidate_memories`/`manage_relation` below. The old `supersession_candidate`/`consolidation_request` Librarian-scanner event types and the tool that used to review/dismiss them are both gone entirely — nothing generates or resolves them anymore.
* `get_events(context_id, agent_id, event_type, agent_session_id, order, limit, offset)`: Retrieves events from the append-only ledger, for multi-agent coordination and wrap-up thread review. **`context_id` is the headline capability here** — previously stored on every event row but completely unreachable from any agent-facing call, now a genuine filter: read back every event logged under a shared thread handle, survivable and re-readable across a session or even a power cut. `agent_id` filters to one agent's events (for "what did the other agent just decide" in a multi-agent DB); `agent_session_id` filters to one SALTMDB adapter session. `order`: `"newest_first"` (default, for discovery) or `"oldest_first"` (for chronological wrap-up synthesis) — always explicit, never inferred from which filter was passed. The old `mode` parameter (`'events'`/`'session'`/`'memories'`) and `status_filter` are both gone — returned event dicts no longer carry a computed `"status"` (`pending`/`resolved`/`dismissed`) field at all, since the mechanism that computed it (the Librarian scanner backlog) no longer exists. Like every MCP tool, it has no `owner_id` input; `agent_id` remains a cross-agent read filter.
* `archive_memory(entity_id)`: Unchanged. Polymorphic tool to archive (retire) one or multiple long-term memories. Accepts a single `entity_id` string OR a list of string IDs.
* `manage_relation(relations, source_id, target_id, predicate, invalidate, valid_at, invalid_at, override_justification)`: Stores single or multiple directional semantic relationship edges between memory nodes, or invalidates an existing edge (`invalidate=True`, sets `invalid_at` on the event/world-time axis; never touches `valid_to`, which is reserved for consolidation). **The predicate contract changed completely.** `predicate` must be one of the **11 agent-selectable closed-vocabulary predicates**: `elaborates_on`, `related_to`, `resolves`, `depends_on`, `verifies`, `corrects`, `caused_by`, `derived_from`, `distinguishes_from`, `part_of`, `contradicts` (see `list_predicates`). **3 reserved/system-owned predicates** — `supersedes`, `consolidated_from`, `revises` — are refused if an agent submits them directly, naming the correct lifecycle tool to use instead (`supersede_memory`/`consolidate_memories`/`revise_memory`). **`similar_to` is legacy and read-only**: existing edges stay traversable, a new write is rejected (`LEGACY_READONLY_PREDICATE`). A drifted/legacy spelling is **rejected outright, never silently substituted** — this is a behavior change from older releases, which used to canonicalize silently and append a `[canonicalized: ...]` note. The rejection includes a schema-derived `corrected_call` you can resubmit verbatim: for a same-direction rename (e.g. `relates_to` → `related_to`) it's just the corrected `predicate`; for one of 6 direction-reversed aliases (`resolved_by`/`remediated_by`→`resolves`, `verified_by`→`verifies`, `affects`→`caused_by`, `summarizes`/`expanded_by`→`elaborates_on`) the `corrected_call` also has `source_id`/`target_id` swapped. **Note the reversal**: `relates_to`/`references` now alias onto `related_to`, NOT `elaborates_on` as in older releases — this is the single most consequential rename in the redesign, so don't rely on old memory of the opposite mapping. A genuinely unrecognized string is rejected too, listing the 11 valid predicates, with no `corrected_call` to offer. None of this closed-vocabulary gating applies to `invalidate=True` calls. Bulk `relations` items now support per-item `invalidate=True` with optional `invalid_at`, or inherit top-level `invalidate` as a batch-wide default; previously bulk calls silently ignored `invalidate` and only ever attempted to create edges. For a bulk `relations` list, all items are validated in one pass; if ANY item has a predicate problem, the WHOLE call is rejected with zero side effects, and `corrected_call["relations"]` is offered only when every flagged item was mechanically correctable (an alias) — if any flagged item was reserved/legacy/unknown, no `corrected_call` is offered for the whole batch. **Governance gate** (unchanged from before): for the strong predicates `elaborates_on`/`resolves`/`supersedes`, the call is rejected (`REJECT_LOW_RELATION_SIMILARITY`) if source/target embedding similarity is too low to plausibly support that claim; independently, any predicate is rejected (`REJECT_CONTRADICTORY_PREDICATE`) if it and an existing predicate on the same directional edge are a known-contradictory pair. Only pass `override_justification` (≥20 characters) when you have a specific, verifiable reason the gate is wrong for this edge. Every override is logged to a `relation_gate_override` audit event, atomic with the relation write. Bulk calls via the `relations` list thread `override_justification` per item; item-level owner overrides are ignored. **Core-memory governance**: a NEW `elaborates_on` edge targeting an active core memory is rejected (`REJECT_CORE_ELABORATES_ON`) — only that core's own `detail_memory_ids` declaration governs it; re-submitting an already-existing edge stays an idempotent no-op.
* `consolidate_memories(consolidations, parent_ids, title, content, tags, context_id, scope, weight, is_core, override_justification, core_reason, core_exit_condition, core_review_after, detail_memory_ids)`: Renamed from the old consolidation-commit tool, same core behavior. Polymorphic tool to commit single or multiple synthesized consolidations from two or more explicit parents, archiving the parents unchanged and linking them with `consolidated_from`. Semantic relations on the parents are never repointed automatically anymore; the response may include an optional orphaned-edge worklist of safe, optional follow-up declarations. **Cohesion gate**: for 2+ parents, the call is rejected (`REJECT_LOW_COHESION`) when the parents aren't actually a cohesive group — their minimum pairwise centroid similarity falls below threshold (an unresolved/unembeddable parent forces rejection). Only pass `override_justification` (≥20 characters) after you've actually verified the parents belong together despite the low score — the text is appended verbatim into the committed entity's `content` as a permanent `[Consolidation Override]` block, so a rubber-stamp justification becomes part of the permanent record. Every override also atomically logs a `consolidation_gate_override` audit event. For the bulk `consolidations` shape, `override_justification` goes on each individual item that needs it, never at the top level. **Core-memory governance**: `is_core` is NEVER inherited from parents — if any resolved parent is currently an active core and `is_core` is omitted, the call is rejected with an actionable error; pass explicit `is_core=True` (with `core_reason`/`core_exit_condition`, optionally `core_review_after`/`detail_memory_ids`) to keep the result core, or `is_core=False` to let it become an ordinary memory. Same capacity caps as `store_memory`.
* `review_core_memory(entity_id, outcome, review_rationale, core_review_after)`: Unchanged. Reviews an active core memory — `outcome='retain'` extends its next review date (requires a future `core_review_after`, at most 30 days out, defaulting to 14 days if omitted), `outcome='demote'` turns it back into an ordinary searchable memory, `outcome='archive'` retires it. A direct, synchronous operation, never a request/queue/event. The configured `SALTMDB_OWNER_ID` identifies the REVIEWING agent — it need not match the entity's own stored owner and never transfers ownership. `review_rationale` (20-1,000 chars) is stored for provenance but never injected into the bootstrap digest. Meaningful content revision is now a `revise_memory`/`supersede_memory` call, not `store_memory`; this tool changes lifecycle state only. Repeating `demote`/`archive` on an already-non-core/already-archived memory is a no-op; `retain` against a non-core or archived memory is rejected.

> [!NOTE]
> **Tools removed from MCP entirely, not deprecated**: the old volatile in-memory secret-storage tool (the underlying store still exists internally but has no MCP-facing tool anymore — do not tell an agent it can call it), the old pending-signal-dismissal tool (its underlying service function still exists only for a one-time legacy DB-init migration sweep, not for agent use), and the old full-corpus-export tool (moved to `saltmdb-cli export-corpus-snapshot`) are all gone with no replacement tool. The old canonical-tag/canonical-predicate lookup tools, the old consolidation-commit tool, and the old unified graph-inspection tool were not removed outright — see the two bullets above and the `search_tags`/`list_predicates`/`consolidate_memories` bullets for what they became. The unified graph-inspection tool's orphan-detection mode specifically moved to `saltmdb-cli orphans`, with no MCP equivalent.

---

## 3. Best Practices: Titles, Detailed Memories & Effective Feature Usage

> [!TIP]
> This section (plus §4's Operational Lifecycle) is also shipped as a portable, harness-agnostic
> skill — [`skills/saltmdb-usage/`](skills/saltmdb-usage/) — for agents/harnesses
> that load skills on demand instead of being told to read this whole file. If the two drift,
> this file is authoritative.

### A. Formulating High-Quality Titles
- **Descriptive & Canonical**: Use specific, unique titles (e.g., `SALTMDB Hybrid Search Candidate Fusion and Final Ranking` instead of generic `Search` or `Notes`).
- **Entity Resolution Friendly**: SALTMDB tools (`manage_relation`, `get_memory`, `archive_memory`, `get_lineage`, `get_related_memories`, `consolidate_memories`, `revise_memory`, `supersede_memory`) auto-resolve entity IDs from titles. Distinct titles allow direct tool chaining without needing UUID lookups! (`search_memory` itself no longer takes an `entity_id` — use `get_memory` for direct lookups.)
- **Domain-Clear Prefixing**: When applicable, prefix titles by domain or component (e.g., `[Viewer UI] Bento Grid & Force Graph Layout`, `[Auth] OAuth2 Refresh Token Strategy`).

### B. Crafting Quality Detailed Memories
- **Self-Contained & Actionable Markdown**: Write rich, structured Markdown with clear headings (`#`, `##`), context descriptions, code snippets, trade-offs, and exact steps. Avoid vague 1-line facts. Following this on a genuinely comprehensive memory can trip the advisory-only `OVERSIZED_PAYLOAD` warning (never blocks the write, see § 2's `store_memory` entry) — that's expected and safe to ignore for legitimately comprehensive content, not a cue to trim.
- **Tag Discipline & Consolidation**:
  - Always include relevant folksonomy tags (e.g., `#core`, `#architecture`, `#fix`, `#performance`, `#ui-ux`).
  - Use `search_tags(query)` before creating new tags to prevent tag fragmentation.
  - Write-time tag validation (not `search_tags`, which is a read-only lookup) now collapses any single character outside `[a-z0-9]` between alphanumeric runs to `-` and rejects 2+ adjacent disallowed/separator characters pre-transaction rather than silently deleting them; it is wired into `store_memory`, `revise_memory`, `supersede_memory`, and `consolidate_memories`, the last of which previously had no tag validation at all.
  - Set `is_core=true` only for urgent, TEMPORARY cross-session hazards an agent must know before it could reasonably search for them — active bugs, environment failures, migrations in progress, temporary overrides. It is not a permanent-law or "important knowledge" tier: stable coding rules, standing behavioral guidelines, and user preferences belong in `AGENTS.md`/`CLAUDE.md`/skills. Every core requires `scope='shared'`, `core_reason`, `core_exit_condition` (both 20-500 chars), and an absolute `core_review_after` (default 14 days, max 30); at most 5 may be active globally, each capped at 2,500 characters. Once the urgency ends, call `review_core_memory(outcome='demote')` (keeps the content searchable) or `outcome='archive'` (retires it) — core status is meant to be temporary, not a one-way promotion. Never set `is_core=true` on project-specific state reports, repository audits, or component facts. (Note: the server automatically keeps the `#core` tag in sync with `is_core`; do not set `#core` directly via the `tags` list, it will be silently overridden).
- **Proper Scoping (`scope`)**: Use `scope='shared'` (default) for global facts that should benefit all agents across sessions. Use `scope='private'` only for agent-private transient state.
- **Duplicate Prevention**: Before storing large knowledge blocks, run a quick `search_memory` first — there is no separate duplicate-only preflight parameter anymore. `store_memory` itself still catches an exact content-hash duplicate (hard rejection) and returns `duplicate_candidates` on a near-duplicate (stores anyway, flags it for you to act on).

### C. Effective Search & Functionality Usage
- **Hybrid Retrieval and Final Ranking (`search_memory`)**:
  - Combines FTS5 BM25 and BGE-prefixed dense-vector candidates with weighted RRF, then uses the deployment-configured cross-encoder for final ordering when enabled. Retrieval remains probabilistic; no accuracy percentage is guaranteed across arbitrary corpora, languages, or usage patterns.
  - Automatically returns 1-hop knowledge graph relations when `include_related=True` (default).
  - The agent-facing parameter surface is intentionally small: `query_keywords`, `limit`, `cursor`, `context_id`, `agent_session_id`, `tags_filter`, `memory_type_filter`, `is_core`, `include_related`, `mode` — no parameter-name aliases, no reranking flags, no explicit-ID retrieval (that's `get_memory`'s job now).
  - Use `mode="strict"` when you specifically need superseded matches resolved to their live successor and low-confidence results dropped rather than returned; use `mode="history"` to see superseded candidates explicitly tagged (`is_superseded: true`) instead of hidden; default `mode="broad"` for ordinary retrieval.
- **Smart Tool Chaining**: `manage_relation` accepts status output strings directly (e.g., `source_id="Knowledge stored successfully with ID: <uuid>"`) or exact entity titles without manual regex parsing.
- **Lossless Cognitive Consolidation (`consolidate_memories`)**: Rephrase and synthesize multiple raw memories into a single consolidated memory. If an individual raw memory is already comprehensive and self-contained, do NOT force-merge it with unrelated notes: `consolidate_memories` requires a minimum of two distinct active parent memory IDs; use `revise_memory` for a single-parent correction/repair. Source nodes are soft-archived (`status='archived'`) and auto-linked via `consolidated_from` lineage edges, keeping full ancestry auditable via `get_lineage(direction='ancestors')`.

### D. Retrieval-Outcome Telemetry

There is no built-in mechanism today that measures whether `search_memory` results actually
helped in real sessions — only individual observation, which isn't verifiable or aggregable. The
convention: after acting on a `search_memory` result, call `log_event(event_type="retrieval_outcome",
content="<memory_id>: used|irrelevant|insufficient -- <why>")`. This needs no new tool or schema
— `event_type` is a free-form string and `get_events` already filters by it. It stays **pure
observation**: it must never automatically feed ranking, decay, or memory authority — a popular
memory can still be wrong, and a rarely-used one can still be essential. If a
`saltmdb-stop-retrieval-outcome-gate.py`-style hook (see §7) is installed, it nudges this
automatically after a turn that called `search_memory`; without one, it's a manual habit like
`log_event` itself. Skip it when no search happened that turn — don't skip it because the result
was negative; "irrelevant"/"insufficient" is exactly the useful signal.

---

## 4. Operational Lifecycle

### Phase A: Bootstrap (Session Start)
Immediately upon initialization, before answering the user:
0. **MANDATORY, unconditional, every session — no self-judgment call**: check whether your available-tools list includes a tool-discovery/search mechanism (e.g. Claude Code's `ToolSearch`, Copilot CLI's `search_tool`). If it does, invoke it now targeting the `saltmdb` tools to load their full schemas, before proceeding to step 1 — do this even if you believe schemas are already loaded, since that belief is exactly what causes this step to get silently skipped. Only skip this step if no such discovery mechanism exists in your tool list at all.
1. Call `search_memory` filtering by `is_core=True` (e.g., `search_memory(is_core=True)`). This loads whatever urgent, temporary cross-session hazards are currently active — NOT a general persona/preference dump (those live in `AGENTS.md`/`CLAUDE.md`/skills). On harnesses with lifecycle hooks configured (see Section 7 below), this bootstrap step normally already ran automatically before the session started via a `SessionStart` hook, which prints the canonical core-memory digest (`saltmdb-cli bootstrap-digest`, global, no project-keyword search); this manual step is a fallback for when no such hook exists or fired. If the hook's output is a `<core-bootstrap-error>` report instead of `<core-rules>`, the active core set is malformed or over capacity — rebalance it yourself (demote/archive/shorten/consolidate) before relying on any core content; the hook deliberately fails closed rather than injecting a partial or oversized set.
2. Run a keyword search matching the active repository, folder, or project name (e.g. `query_keywords = 'SALTMDB'`) and task domain (`context_id = 'my-task'`) to gather project intel, past decisions, and component constraints.
3. If you're resuming a specific thread of work (your own or another agent's), call `get_events(context_id=<thread handle>, order='oldest_first')` to read back everything logged under that handle in order — `context_id` is a genuine filter now (it wasn't reachable from any agent-facing call before the agent API redesign), so a `context_id` thread survives a session restart or even a power cut. There is no "pending merge request queue" to check anymore — the old pending-status-filter and dismissal mechanism for `consolidation_request`/`supersession_candidate` events was removed along with the Librarian scanners that generated those event types; `get_events` no longer computes or exposes any `status` field at all.
4. **Think Before You Leap:** Before executing any sub-task, modifying a file, or running commands, call `search_memory` with keywords matching the target component, command, or error string. You must actively search for past constraints, bug fixes, or design parameters before writing code.

### Phase B: In-Session Logging & Active Memory Capture
1. Log every significant milestone, technical decision, and error event using `log_event`.
2. Categorize logs using types: `decision` (design outcomes), `issue` (failures), `fix` (resolutions), and `attempt` (general facts/milestones).
3. **Step Back & Reflect:** If an error occurs during execution, log it immediately (`event_type='issue'`), search memory for matching keywords, stop, and analyze root causes rather than looping on failed actions.
4. **Capture Valuable Insights:** As soon as an issue is resolved, a best practice is identified, or a core architectural choice is made, immediately call `store_memory` to make it permanent.

### Phase C: Session Wrap-up (Commit & Link)
Before concluding your turn or finalizing a major task block:
1. Query short-term events using `get_events(context_id=<your thread handle>)` (or
   `agent_id=<configured SALTMDB_OWNER_ID>` for everything you personally logged this session,
   independent of thread).
2. Synthesize new permanent facts, rules, or progress updates.
3. Commit or upsert these synthesized updates using `store_memory`.
4. If a component depends on or resolves another component, store the relationship edge using `manage_relation(source_id, target_id, predicate)`.

### Phase D: Cognitive Consolidation (Cleanup)
> [!NOTE]
> **No background merge queue, no store-time review gate — consolidation is purely agent-initiated.** There is no Librarian scanner emitting `consolidation_request` events, and (unlike an earlier interim design) `store_memory` does not hand back a two-phase review gate to route through either — a near-duplicate write just stores and returns `duplicate_candidates` inline. Consolidation happens when you decide to do it, triggered by one of two things: (1) `store_memory` returning `duplicate_candidates` on a near-duplicate write — treating that as your cue to call `consolidate_memories` (or `supersede_memory`, if one candidate should simply replace the other) is exactly this workflow, inline; or (2) noticing redundant/overlapping raw memories yourself while searching, with no gate prompting you to look.

When you decide to consolidate (from a `store_memory` `duplicate_candidates` hint, or on your own initiative):
1. Retrieve the content of the candidate raw entities (e.g. via `get_memory`).
2. **Evaluate for Merging vs. Direct Promotion vs. Replacement:**
   * **Multi-Node Synthesis**: If multiple raw entities contain complementary, overlapping, or partial details on the same domain, synthesize them into a single high-quality markdown document via `consolidate_memories`.
   * **Single-Node Promotion**: If a raw entity is already comprehensive, well-structured, and self-contained, do NOT force-merge it with unrelated notes. `consolidate_memories` requires a minimum of two distinct active parent memory IDs; use `revise_memory` for a single-parent correction/repair.
   * **Straight Replacement**: If one candidate should simply replace the other with newer/corrected knowledge (not a synthesis of both), use `supersede_memory` instead — it's the more direct tool for a one-to-one replacement and preserves immutable-identity lineage (`new --supersedes--> old`).
   * **Related, Not Redundant**: If the candidates are genuinely distinct subjects that only share domain vocabulary or a title-template convention (near-duplicate flagging has a known false-positive rate here), neither merge nor replace — link them via `manage_relation` (e.g. `predicate="related_to"`) and leave both standing.
3. Call `consolidate_memories` with `parent_ids` (a list of at least two distinct active parent memory IDs) plus `title` and `content` — both are mandatory (there is no ID-only shortcut). For a single-parent correction/repair, use `revise_memory` instead. The server archives the source raw logs (`status = 'archived'`) and auto-creates `consolidated_from` lineage edges. Source nodes remain retrievable via `get_memory` or `get_lineage(direction='ancestors')` for auditing.
```

---

## 4. Operational Sequences & Examples

### A. The Bootstrap Sequence
```mermaid
sequenceDiagram
    participant Agent
    participant DB as SQLite DB
    Agent->>DB: search_memory(is_core=True)
    DB-->>Agent: Returns Persona & Core Rules
    Agent->>DB: search_memory(query_keywords="authentication service", include_related=True)
    DB-->>Agent: Returns Matching Knowledge & 1-Hop Related Nodes
```

### B. In-Session Logging & Smart Tool Chaining
```python
# 1. Log event (identity comes from SALTMDB_OWNER_ID; there is no agent_id parameter)
log_event(event_type="fix", content="Fixed Nginx buffer size")

# 2. Store memory (returns: "Knowledge stored successfully with ID: 29be643f-...", or the
#    uniform {"status": "ok", "data": {"id": "29be643f-...", ...}, ...} envelope)
mem_res = store_memory(
    title="Nginx Buffer Tuning",
    content="# Nginx Buffer Tuning\nIncrease client_body_buffer_size to 128k.",
    tags=["#nginx", "#performance"],
)

# 3. Direct tool chaining (passing title or status string directly into manage_relation).
#    predicate must be one of the 11 agent-selectable closed-vocabulary predicates -- a
#    drifted spelling like the pre-redesign "resolved_by" is now REJECTED with a
#    corrected_call, never silently substituted, so use the canonical spelling directly.
manage_relation(
    source_id=mem_res,  # Automatically extracts UUID from status string!
    target_id="API Gateway",  # Automatically resolves UUID from component title!
    predicate="resolves",
)
```

### C. Ancestry Lineage Traversal (`get_lineage`)
```python
# Trace multi-generation consolidation/replacement ancestry of a summary node:
lineage_info = get_lineage(entity_id="Synthesized Summary Title", direction="ancestors")
# Returns a nodes/edges graph shape (not the old flat "ancestors" list):
# {
#   "entity_id": "c-uuid-123",
#   "direction": "ancestors",
#   "root": {"id": "c-uuid-123", "title": "Synthesized Summary Title", "status": "consolidated", "owner_id": "...", "updated_at": "...", "depth": 0, "generation_depth": 0},
#   "nodes": [
#     {"id": "c-uuid-123", "title": "Synthesized Summary Title", "status": "consolidated", "owner_id": "...", "updated_at": "...", "depth": 0, "generation_depth": 0},
#     {"id": "raw-uuid-456", "title": "Raw Source Fact 1", "status": "archived", "owner_id": "...", "updated_at": "...", "depth": 1, "generation_depth": 1}
#   ],
#   "edges": [
#     {"relation_id": "...", "source_id": "c-uuid-123", "target_id": "raw-uuid-456", "predicate": "consolidated_from", "depth": 1, "source_title": "...", "source_status": "consolidated", "target_title": "...", "target_status": "archived"}
#   ],
#   "total": 1, "total_nodes": 2, "graph_exhausted": true, "point_in_time": "2026-08-19T...", "max_depth": 5
# }

# direction="descendants" instead shows what this entity eventually became -- a capability
# that didn't exist under the old graph-inspection tool's lineage mode, which only walked backwards.
```

### D. Session Recall — From One Memory to Its Whole Session (`agent_session_id`)
Every memory and event carries the id of the SALTMDB adapter session that produced it
(`agent_session_id`, minted once per adapter process — see `mcp/identity.py`). This lets you pivot
from a single memory you found to everything else that same session did, which is often more
useful than the one memory alone: it recovers the *narrative* around a fact (why it was decided,
what else happened in that pass), not just the fact itself.

```python
# 1. Find or recall a memory. get_memory / search_memory results both include
#    agent_session_id (who created it, immutable) and last_touched_session_id
#    (who last touched it via an in-place administrative update, e.g. a core-flag change).
hit = get_memory(entity_id="Nginx Buffer Tuning")
session = hit["data"]["agent_session_id"]

# 2. Pull every OTHER memory that same session created.
sibling_memories = search_memory(agent_session_id=session, limit=50)

# 3. Pull that session's own event log -- often the richer half, since it's the actual
#    decision/issue/fix narrative, not just the durable facts that got promoted to memory.
sibling_events = get_events(agent_session_id=session, order="oldest_first")
```
**Caveat — this covers *creation*, not every *touch*.** `search_memory`'s `agent_session_id`
filter matches `entities.agent_session_id` only (`WHERE e.agent_session_id = ?` in
`memory_service/orchestrator.py`) — it will not surface a memory that session merely touched
in place (`last_touched_session_id`, e.g. an administrative metadata update via
`store_memory(entity_id=...)`) but didn't originally create. There is currently no MCP-level
filter with the "created OR touched" OR-semantics — that combined filter exists only on the
SALTMDB Viewer's HTTP route (`GET /api/entities?session_id=`, viewer-internal, not an MCP tool).
If you need every memory a session touched at all, not just what it created, you don't have a
single-call way to get that from MCP tools today.

---

## 5. Immutable Identity: `revise_memory` & `supersede_memory`

**Every memory's `entity_id` is now permanent.** Once a memory is created, `store_memory` can never change its actual content (`title`/`content`/`tags`/`owner_id`/`scope`/`memory_type`/`context_id`) — this is a deliberate change from older releases, where `store_memory` with an explicit `entity_id` would clone the current row to a `<entity_id>_h_<suffix>` history snapshot and overwrite the original ID in place (SCD Type 2). That in-place-content-mutation behavior is **gone**. A genuine content change now always goes through one of two dedicated lifecycle tools, each of which creates a **brand-new immutable entity** and links it to its predecessor rather than mutating the old one:

1. **`revise_memory(entity_id, title, content, tags, reason, ...)`** — repairs a deficient representation of the same knowledge (wrong facts, bad formatting, an incomplete write). The predecessor (`entity_id`) is archived byte-for-byte, unchanged, and the new entity links to it with a `revises` edge (`new --revises--> old`).
2. **`supersede_memory(entity_id, title, content, tags, reason, ...)`** — replaces valid-but-outdated knowledge with newer knowledge. Same shape, but the new entity links to the old with `supersedes` instead.

In both cases: the target must be active (an already-archived/superseded `entity_id` is a hard failure reporting its known successors, never silently redirected), the replacement is attributed to the configured caller and `context_id`/`scope`/`memory_type` are inherited when omitted, and the predecessor's content is never mutated or deleted — full ancestry is auditable forever via `get_lineage(entity_id, direction='ancestors')` (what this came from) or `direction='descendants'` (what it eventually became).

### The narrow legacy in-place path that remains

`store_memory` still accepts an explicit `entity_id` — but only for a genuinely **administrative-only** update, never a content change. Internally (`_legacy_update_guard` in `src/saltmdb/domain/services/memory_service/write.py`), any attempt to change a frozen field (`title`, `content`, `tags`, `owner_id`, `scope`, `memory_type`, `context_id`) through this path is rejected outright (`IMMUTABLE_MEMORY`, naming `revise_memory`/`supersede_memory` as the correct tool) — this applies whether the target is resolved via an explicit `entity_id` or an implicit same-title/owner/scope match, so there is no way to sneak a content change through the old upsert path. What genuinely remains updatable in place, on the *same* `entity_id`, with no new entity created: `is_core`, `core_reason`/`core_exit_condition`/`core_review_after`/`detail_memory_ids`, `weight`, `retrieval_text`, and non-identity `metadata`. Because these administrative-only fields are governance/lifecycle metadata rather than the memory's actual knowledge content, this path does **not** create an `<entity_id>_h_<suffix>` history snapshot anymore either — there is nothing content-wise to snapshot, since the guard already guaranteed content is unchanged. (Pre-existing `_h_` rows from before this redesign still exist in the database and remain fully readable; see `MIGRATION.md` for how they were reconciled into the lineage graph.)

### Correcting an inactive (consolidated/archived) memory

`revise_memory`/`supersede_memory`/`consolidate_memories` all require an active (`status='raw'`)
target/parent — attempting any of them against a `consolidated` or `archived` memory is a hard
`INACTIVE_TARGET`/`INACTIVE_PARENT` rejection with zero side effects. For a single stale fact on
an otherwise-fine consolidated memory, do not try to revive it through one of those tools. Instead:
1. `store_memory` a small, self-contained new memory carrying just the corrected information.
2. `manage_relation(predicate="corrects", source_id=<new memory>, target_id=<stale memory>)` —
   `corrects` is an ordinary agent-selectable predicate (not gated by the embedding-similarity
   governance check that applies to `elaborates_on`/`resolves`/`supersedes`), so this works even
   when the new memory's content differs substantially from the stale one's.

No `consolidate_memories` call is needed for a single correction. `search_memory(mode="strict")`
already demotes a memory that is the target of a currently-valid `corrects` edge below its
corrector automatically — no extra step is needed to make the correction take retrieval priority.

---

---

## 6. Self-Update Protocol (After a SALTMDB Version Upgrade)

This section is for **you, the agent**, not just the human reading this file. If the user tells you SALTMDB has been updated (a `git pull`, a new release, "we updated SALTMDB, check the agent guide," etc.), do not assume your own configuration is still correct just because this repo's code and docs are current. Run this protocol:

1. **Check what changed.** Read `MIGRATION.md`'s Version Schema Registry. Compare the current `pyproject.toml` version against the last version you have a memory of (search your long-term memory for prior SALTMDB version references). Note any entries marked with a required migration action, and any tool signature changes.
2. **Diff the Core Operating Commandments.** Compare this file's §3 System Prompt Template (specifically the "Core Operating Commandments" list) against your own currently-loaded global/persistent instructions — for Claude Code that's `~/.claude/CLAUDE.md`; for other MCP clients, wherever your equivalent standing instructions live. If the count or content differs, **do not silently overwrite your global instruction file.** Show the user the specific diff and ask for confirmation before editing it — that file governs every project you use, not just SALTMDB.
3. **Bundled orchestration references no longer have a repo-root counterpart.** As of `v0.1.0-alpha.74`, `ORCHESTRATOR.md`, `MULTI_AGENT_ORCHESTRATION.md`, and `WORKER_TEMPLATE.md` were retired from this repo's root — maintaining them as an independent vendor-neutral copy alongside your harness's own bundled skill copy (for Claude Code: `~/.claude/skills/saltmdb-subagent-orchestration/references/`) repeatedly drifted out of sync (a 2026-07-25 audit found them independently drifted in both directions; see `MIGRATION.md` alpha.49–.51 for the history) and wasn't worth the maintenance cost. There is nothing left to diff against here — your harness-bundled skill copy is now the sole reference implementation of the orchestration protocol. If you're upgrading from a pre-alpha.74 memory of this repo and still have a locally-cached copy of the old repo-root files, discard it; don't treat it as authoritative.
4. **Record the sync.** Once reconciled (or once you've confirmed nothing needed to change), store a memory noting the version you're now synced to, so a future session doesn't have to repeat this check from scratch.

---

## 7. Session Automation via Lifecycle Hooks

Session lifecycle hooks allow your AI host harness (such as **Claude Code**, **Antigravity CLI (`agy`)**, or **GitHub Copilot CLI**) to automatically trigger SALTMDB operations at specific session lifecycle events—such as session start, pre-tool execution, post-tool response inspection, context compaction, turn completion, and session end.

Using hooks eliminates manual prompt bootstrapping, enforces pre-action memory searches ("Think Before You Leap"), reacts to tool *responses* (not just tool names) for duplicate/unlinked-memory/empty-result nudges and a repeated-failure circuit breaker, prevents context loss during transcript compaction, ensures post-action quality self-critique with a reflection-to-memory closer, enforces the retrieval-outcome telemetry convention, and reminds about session wrap-up.

Every `saltmdb-*.py` script body is fully agent-agnostic — the same script is registered from all three harnesses' configs; only the registration snippet differs. See [`hooks/README.md`](hooks/README.md) for the full inventory, naming convention, and design principle. All shipped, ready-to-use hook scripts and harness configuration templates live in the [`hooks/`](hooks/) directory.

---

### A. Claude Code Hooks Configuration

Claude Code supports lifecycle hooks configured in your global settings file (`~/.claude/settings.json` or `%USERPROFILE%\.claude\settings.json`).

#### Reference Files:
- **Configuration Template**: [`hooks/claude-settings-example.json`](hooks/claude-settings-example.json)
- **Session Start Script**: [`hooks/saltmdb-session-start-bootstrap.py`](hooks/saltmdb-session-start-bootstrap.py)
- **Pre-Tool Search Gate Script**: [`hooks/saltmdb-pre-tool-search-gate.py`](hooks/saltmdb-pre-tool-search-gate.py)
- **Post-Tool Response Nudges Script**: [`hooks/saltmdb-post-tool-response-nudges.py`](hooks/saltmdb-post-tool-response-nudges.py)
- **Post-Tool Failure Circuit-Breaker Script**: [`hooks/saltmdb-post-tool-failure-circuit-breaker.py`](hooks/saltmdb-post-tool-failure-circuit-breaker.py)
- **Stop Self-Critique Gate Script**: [`hooks/saltmdb-stop-critique-gate.py`](hooks/saltmdb-stop-critique-gate.py)
- **Stop Retrieval-Outcome Gate Script**: [`hooks/saltmdb-stop-retrieval-outcome-gate.py`](hooks/saltmdb-stop-retrieval-outcome-gate.py)
- **Session-End Wrap-Up Reminder Script**: [`hooks/saltmdb-session-end-wrapup-reminder.py`](hooks/saltmdb-session-end-wrapup-reminder.py)
- **Pre-Compact Sweep Script (standalone)**: [`hooks/saltmdb-pre-compact-sweep.py`](hooks/saltmdb-pre-compact-sweep.py)

#### Overview of Hooks:
1. **`SessionStart`**: Triggers [`saltmdb-session-start-bootstrap.py`](hooks/saltmdb-session-start-bootstrap.py), which invokes `saltmdb-cli bootstrap-digest` (no arguments) to auto-inject the canonical core-memory digest into context — global and core-only, no project-keyword search — plus a nudge if any core memory is overdue for review (`saltmdb-cli corpus-health`), and `saltmdb-cli session-digest` to inject the directory-scoped last-session digest when one exists for the current cwd.
2. **`PreToolUse`**: Triggers [`saltmdb-pre-tool-search-gate.py`](hooks/saltmdb-pre-tool-search-gate.py) for edit/bash tool executions, enforcing Rule 1 ("Think Before You Leap") by denying action until at least one `search_memory` call has been recorded this session. Primary signal is a structured per-session flag file set by the `PostToolUse` nudge script on every `search_memory` call (not a transcript-only check — see §C below for why); a transcript scan remains as a fallback for harnesses that supply `transcript_path`.
3. **`PostToolUse`**: Triggers [`saltmdb-post-tool-response-nudges.py`](hooks/saltmdb-post-tool-response-nudges.py) on `store_memory`/`search_memory` (unacted duplicate candidates, unlinked memories, empty strict results; also sets the retrieval-outcome-pending flag consumed at `Stop`) and [`saltmdb-post-tool-failure-circuit-breaker.py`](hooks/saltmdb-post-tool-failure-circuit-breaker.py) on `log_event` (repeated-failure fingerprinting per CLAUDE.md rule 2; also clears that pending flag on a `retrieval_outcome` event).
4. **`PreCompact`**: Inlines a background agent prompt to sweep and persist unrecorded decisions, bug fixes, or rules before conversation transcript compaction (a standalone script version also exists for manual/cron use — see above).
5. **`Stop`**: Triggers [`saltmdb-stop-critique-gate.py`](hooks/saltmdb-stop-critique-gate.py) (2-question self-reflection, then requires it become a `store_memory` call or an explicit opt-out) and [`saltmdb-stop-retrieval-outcome-gate.py`](hooks/saltmdb-stop-retrieval-outcome-gate.py) (requires a `retrieval_outcome` event after any `search_memory` call this turn).
6. **`SessionEnd`**: Triggers [`saltmdb-session-end-wrapup-reminder.py`](hooks/saltmdb-session-end-wrapup-reminder.py), a one-shot reminder to check `get_events` for anything durable still only in the ephemeral event ledger.

---

### B. Google Antigravity CLI (`agy`) Hooks Integration

Antigravity CLI supports execution lifecycle hooks configured in workspace or global settings (`~/.gemini/antigravity-cli/settings.json`). Antigravity's own hook event set today only covers `PreInvocation`/`PreToolUse` (no confirmed `PostToolUse`/`Stop`/`SessionEnd`/`PreCompact` equivalent) — the config below only wires what's confirmed to exist, per this project's own agent-agnostic design principle ("a harness just doesn't register a hook it has no event for").

#### Reference Files:
- **Configuration Template**: [`hooks/antigravity-settings-example.json`](hooks/antigravity-settings-example.json)
- **Session Start Script**: [`hooks/saltmdb-session-start-bootstrap.py`](hooks/saltmdb-session-start-bootstrap.py)
- **Pre-Tool Search Gate**: [`hooks/saltmdb-pre-tool-search-gate.py`](hooks/saltmdb-pre-tool-search-gate.py)

#### Overview of Hooks:
- **`PreInvocation`**: Invokes [`saltmdb-session-start-bootstrap.py`](hooks/saltmdb-session-start-bootstrap.py) to pre-load the canonical core-memory digest prior to initial prompt processing, and injects the directory-scoped last-session digest via `saltmdb-cli session-digest` when one exists for the current cwd.
- **`PreToolUse`**: Intercepts file modification tools (`replace_file_content`, `write_to_file`, `run_command`) using [`saltmdb-pre-tool-search-gate.py`](hooks/saltmdb-pre-tool-search-gate.py) to ensure prior memory searches.

---

### C. GitHub Copilot CLI Hooks Integration

GitHub Copilot CLI supports custom hooks defined in `.github/hooks/*.json` in your project repository or globally in `~/.copilot/hooks/hooks.json` (Unix) / `%USERPROFILE%\.copilot\hooks\hooks.json` (Windows).

#### Reference Files:
- **Configuration Specification**: [`hooks/copilot-hooks-example.json`](hooks/copilot-hooks-example.json)
- **Session Start Script**: [`hooks/saltmdb-session-start-bootstrap.py`](hooks/saltmdb-session-start-bootstrap.py)
- **Pre-Tool Search Gate Script**: [`hooks/saltmdb-pre-tool-search-gate.py`](hooks/saltmdb-pre-tool-search-gate.py)
- **Stop Self-Critique Gate Script**: [`hooks/saltmdb-stop-critique-gate.py`](hooks/saltmdb-stop-critique-gate.py)

#### Overview & Permission Decision Protocol:
- **`sessionStart`**: Runs [`saltmdb-session-start-bootstrap.py`](hooks/saltmdb-session-start-bootstrap.py) to output the canonical core-memory digest on session init, plus the directory-scoped last-session digest via `saltmdb-cli session-digest` when one exists for the current cwd.
- **`preToolUse`**: Runs [`saltmdb-pre-tool-search-gate.py`](hooks/saltmdb-pre-tool-search-gate.py) — the same script Claude Code and Antigravity use. Reads tool context on `stdin`, does its own read-only-tool check internally (Copilot's `preToolUse` fires unfiltered, unlike the other two harnesses' matcher-scoped registration), and writes JSON permission decisions on `stdout`:
  - Allowed: `{"permissionDecision": "allow", ...}`
  - Denied: `{"permissionDecision": "deny", "permissionDecisionReason": "...", ...}`
  - This replaces the former separate `saltmdb-copilot-pre-tool.py`, which reimplemented the same decision logic independently (drift risk) instead of sharing it.
  - **Four confirmed-live Windows bugs, fixed**: (1) `RISKY_TOOL_NAMES` (the Stop-time critique gate's risky-call detection, below) was case-sensitive, but Copilot lowercases tool names in its transcript (`"bash"`/`"edit"`) — now case-insensitive. (2) Copilot's `preToolUse` payload has no flat `tool_name`/`toolName` field, only a nested `toolCalls[0].name` — the read-only-tool fast path never engaged; `get_tool_name()` now falls back to this nested shape. (3) Copilot's `preToolUse` carries **no `transcript_path` field at all** (unlike its own `agentStop`) — the gate's only signal was a transcript scan, which always saw an empty segment and permanently fail-opened, defeating Rule-1 enforcement entirely on Copilot. Fixed with a structured per-session flag file set on every `search_memory` call, checked before the transcript-scan fallback. (4) That flag-file fix initially shipped broken: the `PostToolUse` scripts that need to *write* the flag (`saltmdb-post-tool-response-nudges.py`, `saltmdb-post-tool-failure-circuit-breaker.py`) still used the old flat-alias-only tool-name lookup, not `get_tool_name()` — so on Copilot's `postToolUse` (same nested shape as `preToolUse`), the flag was never actually written, silently defeating fix (3) in practice. Both scripts now use `get_tool_name()` too — see [`hooks/README.md`](hooks/README.md) "Windows notes" for full detail, verification, and the one residual known gap (the very first risky call of a session, before any `search_memory` flag can exist, still fails open).
- **`agentStop`**: Triggers [`saltmdb-stop-critique-gate.py`](hooks/saltmdb-stop-critique-gate.py) for post-turn reflection. **Fixed bug**: this was previously wired to a script that only emitted Claude Code's `{"decision":"block"}` schema, which `agentStop` has no documented support for — likely non-functional as shipped. The script now emits `permissionDecision`/`permissionDecisionReason` redundantly alongside `decision`/`reason`, so it should work here too (still worth confirming empirically against a real Copilot CLI session).



---

> [!IMPORTANT]
> **SQL Access Security:** Agents do not have raw SQL execution permissions. All actions must be performed using the predefined parameterized MCP tools. Do not expose a SQL client tool to agents, as this creates a major database integrity and credentials leak vulnerability.
