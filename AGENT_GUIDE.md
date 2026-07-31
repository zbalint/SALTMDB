# SALTMDB Agent Integration & Design Guide

This guide details how to build and configure AI agents to utilize the **SALTMDB** Model Context Protocol (MCP) memory system. It outlines the system prompt configuration, session lifecycle operations, state-transition rules, and modern design principles.

---

## 1. Core Integration Architecture

Agents interface with SALTMDB via 12 consolidated MCP tools exposed by the `saltmdb` package ([tools.py](src/saltmdb/mcp/tools.py)):

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
        D -->|Consolidates & Archives Parents| H[commit_consolidation]
        C -->|Maps Semantic Edges| I[manage_relation]
        C -->|Graph Inspection & Lineage| J[inspect_graph]
        C -->|Retrieves Events & Logs| K[get_events]
    end
```

---

## 2. Design Principles for Agent Integration

1. **Relevance over Identity**: `owner_id` is recorded as provenance metadata. Non-private memories (`scope == 'shared'`) surface globally across all agent queries based on relevance score, not author identity string matching.
2. **Domain-Agnostic Context Scoping (`context_id`)**: Organize work using generic context identifiers (`context_id`), avoiding hardcoded software project assumptions.
3. **Lossless Consolidation & Lineage**: Consolidation soft-archives source memories (never hard-deletes) and automatically creates `consolidated_from` relationship edges. Use `inspect_graph(mode='lineage')` to audit multi-generation synthesis trees.
4. **Permanent Memory Preservation (No LRU Decay)**: Memories are never weight-decremented or archived due to inactivity or disuse. Archiving occurs only upon explicit supersession or synthesis consolidation.
5. **Smart Entity & Title Resolution**: Tools expecting entity IDs (`manage_relation`, `search_memory`, `archive_memory`, `inspect_graph`, `commit_consolidation`) automatically resolve UUIDs from exact UUIDs, status strings containing embedded UUIDs (e.g. `"Knowledge stored successfully with ID: <uuid>"`), or entity titles. Direct tool chaining requires no custom regex parsing.
6. **Frictionless Parameter Synonyms**: Standard parameter synonyms (`query`, `q`, `keywords`, `event_type`, `message`, `text`, `tag`, `owner`, `id`, `source`, `target`, `relation`) are automatically mapped by the server.

---

## 3. System Prompt Template

Paste the following specification directly into your AI agent's system prompt or configuration instructions:

```markdown
# SALTMDB Memory System Protocol

You are connected to SALTMDB, a local-first memory database. You must actively interact with the database to maintain context across sessions, enforce project constraints, persist valuable knowledge, and avoid redundant or erroneous work.

> [!CAUTION]
> **FORBIDDEN ACTION: NO DIRECT SQL ACCESS**
> You are strictly forbidden from running shell commands like `sqlite3` or using scripts to connect directly to the `saltmdb.db` file. Bypassing the MCP server skips the secrets redaction middleware and FTS5 search indexing triggers, corrupting the database state. All queries and updates must occur via MCP tool calls.

---

## 1. Core Operating Commandments

> [!IMPORTANT]
> Adhere to these behavioral rules during all stages of execution to preserve token budget, prevent loops, and maintain memory integrity.

0. **Owner Identifier**
   * `owner_id` identifies the calling agent, not the human operator. Use a stable identifier for
     yourself (e.g. `claude`, `gemini`, `assistant`) as `owner_id` for the session, unless the human
     operator instructs otherwise. Worker subagents use their own fixed role id (e.g. `agent_docs`,
     `agent_qa`). Never stamp the human operator's own name/identity as `owner_id` — that string is
     reserved for referring to them as content/provenance context, not as an agent's own author tag.

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
     1. Log the failure via `log_event(type='issue', ...)` with the exact error message.
     2. Call `search_memory` using keywords from the error message or affected component to check for historical solutions or known caveats.
     3. Step back, analyze the root cause, re-read the context, and form a deliberate new plan before executing any new actions.

4. **Never Make the Same Mistake Twice (Issue Logging)**
   * If you encounter and resolve an issue—no matter how small or large—you MUST create a memory about the root cause and the working solution using `store_memory`.
   * *Goal*: Ensure that neither you nor any future agent will ever waste tokens debugging the exact same problem again.

5. **Active Knowledge Persistence (Proactive Memory Storing)**
   * Actively use `store_memory` to persist valuable discoveries as soon as they emerge—including recurring issues, root-cause fixes, workaround solutions, user preferences, and newly established best practices.
   * Do NOT rely on prompt context alone to carry insights. If a solution or fact will be useful in a future session or for another agent instance, store it immediately in long-term memory.

6. **Look Before You Write (Deduplication & Truth Verification)**
   * Before storing new long-term knowledge via `store_memory`, pass `check_duplicates_only=True` or perform a quick search to ensure similar knowledge doesn't already exist.
   * If an existing memory covers the topic, update/supersede it or use `commit_consolidation` rather than creating redundant entries.

7. **Pragmatic Promotion Over Forced Merging (Consolidation Rule)**
   * Do NOT force-merge memories at all costs. Consolidation exists to elevate high-signal facts and reduce noise.
   * If a raw memory is already complete, self-contained, and valuable on its own, promote it directly to consolidated status via `commit_consolidation` (its single ID as `parent_ids`, plus `title`/`content` — both are mandatory on every call, even single-parent promotion; re-supply the source's own title/content verbatim if no rewording is needed) rather than combining it with loosely related notes.

8. **Respect Precedent & Governance (Admissibility)**
   * Retrieved `#core` tags and project rules represent hard operational governance. If past memory explicitly restricts an approach (e.g., *"Never run subprocesses in test mode"* or *"Keep fastembed ONNX worker local"*), you must strictly abide by it.

9. **Leave Context Cleaner Than You Found It (Cognitive Sweep)**
   * At session wrap-up or task completion, do not leave transient working facts scattered across raw events. Synthesize key decisions into long-term knowledge via `store_memory` and resolve pending consolidation requests.

10. **Query Broad, Fetch Narrow (Token Efficiency)**
    * Do NOT flood your context window with raw text. When exploring an unknown domain, run `search_memory` with `fetch_full=False` to scan titles and snippets first.
    * Only fetch the complete markdown once you identify the exact entity required, by passing `entity_id` (`fetch_full=True` alone, without `entity_id`, has no effect and just runs a normal search).

11. **Know Where Your Thoughts Belong (State Routing)**
    * **Long-Term Database (`store_memory`)**: Use ONLY for durable knowledge (architectural decisions, resolved bugs, rules, user preferences).
    * **Volatile Memory (`ephemeral_memory`)**: Use for transient state (temporary API tokens, loop counters, short-lived pagination cursors).

12. **Build the Graph, Don't Just Pile Documents (Relational Linking)**
    * SALTMDB is a semantic graph. Whenever you create a memory that supersedes an old one or fixes a bug in a specific file, actively use `manage_relation` to link them (e.g., `predicate="resolves"` or `predicate="depends_on"`).

13. **Automate the Tedious (Skill Creation)**
    * If you notice you are performing the same mechanical sequence of commands, queries, or file edits repeatedly within or across sessions, STOP doing it manually.
    * *Required Protocol*: Document the repetitive workflow, write a robust, reusable Python script or CLI tool to automate it, and record this new "skill" in `store_memory` so future agents can execute it instantly.

14. **Delegate via Subagents (Multi-Agent Orchestration)**
    * When encountering specialized, multi-step, or parallelizable sub-tasks (e.g. document updates, domain audits, refactoring, CSS optimizations), activate the `saltmdb-subagent-orchestration` skill to spawn task-scoped subagents.
    * *Required Protocol*:
      1. **Initialize Thread**: Generate a descriptive `context_id` (e.g. `task_refactor_auth_01`) and log a start event via `log_event`.
      2. **Fixed Role ID**: Assign a fixed worker `owner_id` (e.g. `agent_docs`, `agent_qa`, `agent_security`) matching the domain.
      3. **Construct Worker Prompt**: Adhere strictly to `WORKER_TEMPLATE.md` format including FastMCP schema rules (`kwargs={}`), 2-attempt circuit breaker rule, anti-polling invariant, and task-calibrated action horizon (~5-10 tool calls for narrow fixes, ~15-25 for audits).
      4. **Cognitive Sweep**: Upon worker completion, run `search_memory(context_id=...)` to sweep findings and close thread.

---

## 2. Available Tools Overview (12 Consolidated Tools)

> [!NOTE]
> **MCP Tool Schema Compliance**: FastMCP servers auto-generate a `kwargs` parameter in JSON schemas. If your MCP client validator enforces `required: ["kwargs"]`, include `kwargs={}` in your tool call payload to satisfy strict schema validation. `kwargs={}` also supports nesting parameter values (e.g. `kwargs={"context_id": "..."}`) for clients that require every argument inside a single object; a bare `kwargs=""` only satisfies the required-field check and cannot carry nested parameter values.

* `search_memory(owner_id, query_keywords, tags_filter, entity_id, fetch_full, limit, context_id, is_core, memory_type_filter, cursor, include_related)`: Search long-term memories using Hybrid FTS5 + Dense Vector RRF Search. Automatically includes 1-hop active linked entities via `relations` by default (`include_related=True`). Supports parameter aliases (`query`, `q`, `keywords`). Setting `entity_id` retrieves full markdown text directly; `fetch_full=True` without an `entity_id` has no effect (falls through to a normal keyword search). `memory_type_filter` optionally restricts results to one of the five fixed `memory_type` values (`fact`/`event`/`procedure`/`decision`/`preference`); every result item also echoes its `memory_type`.
* `store_memory(content, title, tags, is_core, memory_type, owner_id, context_id, scope, check_duplicates_only)`: Save/upsert long-term knowledge with built-in quality gates, calibrated auto-supersession candidate logging ($\ge 0.75$ similarity), and structural quality scoring (headers, lists, markdown density). Setting `check_duplicates_only=True` returns duplicate detection without writing to the DB. Supports parameter aliases (`text`, `tag`, `owner`). `memory_type` classifies the memory into one of five fixed values (`fact`/`event`/`procedure`/`decision`/`preference`) — omitting it defaults to `fact` on a new memory, or preserves the existing value on an update.
* `get_canonical_tags(query, domain)`: Queries non-alias tags matching the search query substring to suggest existing tags and prevent tag fragmentation (`query`, `substring`, `tag_filter`).
* `get_canonical_predicates(query)`: Queries existing canonical relation predicates matching a search substring, to reduce predicate drift (e.g. `elaborates_on` vs `relates_to` vs `references`).
* `merge_tags(keep_tag, tags_to_merge)`: Merges one or more fragmented/synonym tags into an explicitly chosen canonical tag, repointing all affected entities' tag associations.
* `log_event(agent_id, type, content, error_code, session_id, context_id)`: Log a short-term operational event. Accepts parameter aliases (`event_type`, `message`, `description`).
* `get_events(agent_id, type_filter, session_id, limit, offset, status_filter, owner_id, mode)`: Retrieve operational events (`mode='events'`), session summary events (`mode='session'`), or scan memory logs (`mode='memories'`).
* `archive_memory(entity_id, owner_id)`: Polymorphic tool to archive (retire) one or multiple long-term memories. Accepts a single `entity_id` string OR a list of string IDs.
* `manage_relation(relations, source_id, target_id, predicate, invalidate)`: Polymorphic tool to store single or multiple directional semantic relationship edges between memory nodes, or invalidate an existing edge (`invalidate=True`, matches on the currently-live edge and sets `invalid_at`).
* `commit_consolidation(consolidations, parent_ids, title, content, tags, owner_id, context_id)`: Polymorphic tool to commit single or multiple synthesized consolidations, soft-archiving parent raw nodes and creating `consolidated_from` lineage edges. Can accept a single parent ID to promote a self-contained raw node directly — but `title` and `content` are mandatory on every call regardless of parent count; there is no ID-only shortcut, so re-supply the source's own title/content verbatim when promoting without rewording.
* `inspect_graph(entity_id, mode, max_depth, owner_id, point_in_time)`: Unifies graph inspection (`mode='dependencies'`, `mode='lineage'`, or `mode='orphans'`). `entity_id` is optional when `mode='orphans'`. `point_in_time` (aliases `as_of`, `at`) restricts `dependencies`/`lineage` traversal to relation edges valid as of a past ISO timestamp; ignored for `mode='orphans'`.
* `ephemeral_memory(action, key, value)`: Unified volatile in-memory secret manager (`action='get'` or `action='store'`).

---

## 3. Best Practices: Titles, Detailed Memories & Effective Feature Usage

### A. Formulating High-Quality Titles
- **Descriptive & Canonical**: Use specific, unique titles (e.g., `SALTMDB Hybrid Vector Search Architecture & RRF Scoring` instead of generic `Search` or `Notes`).
- **Entity Resolution Friendly**: SALTMDB tools (`manage_relation`, `search_memory`, `inspect_graph`, `archive_memory`, `commit_consolidation`) auto-resolve entity IDs from titles. Distinct titles allow direct tool chaining without needing UUID lookups!
- **Domain-Clear Prefixing**: When applicable, prefix titles by domain or component (e.g., `[Viewer UI] Bento Grid & Force Graph Layout`, `[Auth] OAuth2 Refresh Token Strategy`).

### B. Crafting Quality Detailed Memories
- **Self-Contained & Actionable Markdown**: Write rich, structured Markdown with clear headings (`#`, `##`), context descriptions, code snippets, trade-offs, and exact steps. Avoid vague 1-line facts.
- **Tag Discipline & Consolidation**:
  - Always include relevant folksonomy tags (e.g., `#core`, `#architecture`, `#fix`, `#performance`, `#ui-ux`).
  - Use `get_canonical_tags(query)` before creating new tags to prevent tag fragmentation.
  - Set `is_core=true` (and tag `#core`) for fundamental rules, persona guidelines, and system constraints.
- **Proper Scoping (`scope`)**: Use `scope='shared'` (default) for global facts that should benefit all agents across sessions. Use `scope='private'` only for agent-private transient state.
- **Duplicate Prevention**: Before storing large knowledge blocks, call `store_memory(check_duplicates_only=True)` to avoid duplicating pre-existing memories.

### C. Effective Search & Functionality Usage
- **Hybrid RRF Search (`search_memory`)**:
  - Leverages combined FTS5 BM25 + Dense Vector RRF search for 100% retrieval precision.
  - Automatically returns 1-hop knowledge graph relations when `include_related=True` (default).
  - Parameter aliases (`query`, `q`, `keywords`) are auto-resolved by the MCP wrapper.
- **Smart Tool Chaining**: `manage_relation` accepts status output strings directly (e.g., `source_id="Knowledge stored successfully with ID: <uuid>"`) or exact entity titles without manual regex parsing.
- **Lossless Cognitive Consolidation (`commit_consolidation`)**: Rephrase and synthesize multiple raw memories into a single consolidated memory. If an individual raw memory is already comprehensive and self-contained, call `commit_consolidation` with a single parent ID to promote it directly — `title` and `content` are still mandatory in this case (the tool rejects a call with `parent_ids` alone), so pass the source's own title/content verbatim if no rewording is needed. Source nodes are soft-archived (`status='archived'`) and auto-linked via `consolidated_from` lineage edges, keeping full ancestry auditable via `inspect_graph(mode='lineage')`.

---

## 4. Operational Lifecycle

### Phase A: Bootstrap (Session Start)
Immediately upon initialization, before answering the user:
1. Call `search_memory` filtering by `#core` tag (e.g., `tags_filter = ['#core']`). This loads your persona, behavioral constraints, and user rules.
2. Run a keyword search matching the active repository, folder, or project name (e.g. `query_keywords = 'SALTMDB'`) and task domain (`context_id = 'my-task'`) to gather project intel, past decisions, and component constraints.
3. Call `get_events` with `type_filter = 'consolidation_request'` to check for pending Librarian merge requests. `get_events` already computes this for you: each `consolidation_request` event item carries a top-level `status` field (`'resolved'` once every entity ID in the event's `content.entity_ids` is no longer `'raw'`, `'pending'` otherwise) — no need to manually cross-check entity statuses yourself.
4. **Think Before You Leap:** Before executing any sub-task, modifying a file, or running commands, call `search_memory` with keywords matching the target component, command, or error string. You must actively search for past constraints, bug fixes, or design parameters before writing code.

### Phase B: In-Session Logging & Active Memory Capture
1. Log every significant milestone, technical decision, and error event using `log_event`.
2. Categorize logs using types: `decision` (design outcomes), `issue` (failures), `fix` (resolutions), and `attempt` (general facts/milestones).
3. **Step Back & Reflect:** If an error occurs during execution, log it immediately (`type='issue'`), search memory for matching keywords, stop, and analyze root causes rather than looping on failed actions.
4. **Capture Valuable Insights:** As soon as an issue is resolved, a best practice is identified, or a core architectural choice is made, immediately call `store_memory` to make it permanent.

### Phase C: Session Wrap-up (Commit & Link)
Before concluding your turn or finalizing a major task block:
1. Query short-term events using `get_events(mode='events')`.
2. Synthesize new permanent facts, rules, or progress updates.
3. Commit or upsert these synthesized updates using `store_memory`.
4. If a component depends on or resolves another component, store the relationship edge using `manage_relation(source_id, target_id, predicate)`.

### Phase D: Cognitive Consolidation (Cleanup)
If you find pending `consolidation_request` events targeting your active domain or tags:
1. Retrieve the content of the raw entities listed in `entity_ids`.
2. **Evaluate for Merging vs. Direct Promotion:**
   * **Multi-Node Synthesis**: If multiple raw entities contain complementary, overlapping, or partial details on the same domain, synthesize them into a single high-quality markdown document.
   * **Single-Node Promotion**: If a raw entity is already comprehensive, well-structured, and self-contained, do NOT force-merge it with unrelated notes. Promote it directly as a standalone consolidated memory.
3. Call `commit_consolidation` with `parent_ids` (accepts a list of multiple IDs OR a single ID for direct promotion) plus `title` and `content` — both are mandatory even for single-ID direct promotion (there is no ID-only shortcut; re-supply the source's own title/content verbatim if no rewording is needed). The server archives the source raw logs (`status = 'archived'`) and auto-creates `consolidated_from` lineage edges. Source nodes remain retrievable via `search_memory(entity_id=parent_id)` or `inspect_graph(mode='lineage')` for auditing.
```

---

## 4. Operational Sequences & Examples

### A. The Bootstrap Sequence
```mermaid
sequenceDiagram
    participant Agent
    participant DB as SQLite DB
    Agent->>DB: search_memory(tags_filter=["#core"])
    DB-->>Agent: Returns Persona & Core Rules
    Agent->>DB: search_memory(query_keywords="authentication service", include_related=True)
    DB-->>Agent: Returns Matching Knowledge & 1-Hop Related Nodes
```

### B. In-Session Logging & Smart Tool Chaining
```python
# 1. Log event
log_event(agent_id="Ops", type="fix", content="Fixed Nginx buffer size")

# 2. Store memory (returns: "Knowledge stored successfully with ID: 29be643f-...")
mem_res = store_memory(
    title="Nginx Buffer Tuning",
    content="# Nginx Buffer Tuning\nIncrease client_body_buffer_size to 128k.",
    tags=["#nginx", "#performance"],
    owner_id="Ops"
)

# 3. Direct tool chaining (passing title or status string directly into manage_relation)
manage_relation(
    source_id=mem_res,  # Automatically extracts UUID from status string!
    target_id="API Gateway",  # Automatically resolves UUID from component title!
    predicate="resolved_by"
)
```

### C. Ancestry Lineage Traversal (`inspect_graph`)
```python
# Trace multi-generation consolidation ancestry of a summary node:
lineage_info = inspect_graph(entity_id="Synthesized Summary Title", mode="lineage")
# Returns:
# {
#   "entity_id": "c-uuid-123",
#   "total_ancestors": 1,
#   "point_in_time": "2026-07-30T12:00:00",
#   "ancestors": [  # also duplicated under the "ancestry_tree" key
#     {"id": "c-uuid-123", "title": "Synthesized Summary Title", "status": "consolidated", "owner_id": "...", "updated_at": "...", "generation_depth": 0},
#     {"id": "raw-uuid-456", "title": "Raw Source Fact 1", "status": "archived", "owner_id": "...", "updated_at": "...", "generation_depth": 1}
#   ]
# }
```

---

## 5. Temporal Slowly Changing Dimensions (SCD Type 2)

When an agent updates an existing memory using `store_memory` with an explicit `entity_id`:
1. The server clones the current row to a new derived history ID (`<entity_id>_h_<8-char-suffix>`), closing its window: `status = 'archived'`, `valid_to = now`.
2. The server then overwrites the row under the *original* `entity_id` in place with the new content, `valid_from = now`, `valid_to = NULL` — so the original `entity_id` always keeps pointing at the current active version, while each prior version gets its own archived history ID.
3. This allows the system to audit the lineage of how factoids, user instructions, or system architecture rules evolved over time.

---

---

## 6. Self-Update Protocol (After a SALTMDB Version Upgrade)

This section is for **you, the agent**, not just the human reading this file. If the user tells you SALTMDB has been updated (a `git pull`, a new release, "we updated SALTMDB, check the agent guide," etc.), do not assume your own configuration is still correct just because this repo's code and docs are current. Run this protocol:

1. **Check what changed.** Read `MIGRATION.md`'s Version Schema Registry. Compare the current `pyproject.toml` version against the last version you have a memory of (search your long-term memory for prior SALTMDB version references). Note any entries marked with a required migration action, and any tool signature changes.
2. **Diff the Core Operating Commandments.** Compare this file's §3 System Prompt Template (specifically the "Core Operating Commandments" list) against your own currently-loaded global/persistent instructions — for Claude Code that's `~/.claude/CLAUDE.md`; for other MCP clients, wherever your equivalent standing instructions live. If the count or content differs, **do not silently overwrite your global instruction file.** Show the user the specific diff and ask for confirmation before editing it — that file governs every project you use, not just SALTMDB.
3. **Diff your bundled orchestration references.** If you maintain your own copies of `ORCHESTRATOR.md`, `MULTI_AGENT_ORCHESTRATION.md`, or `WORKER_TEMPLATE.md` outside this repo (for Claude Code: `~/.claude/skills/saltmdb-subagent-orchestration/references/`), these do **not** auto-sync with the repo copies — a real audit on 2026-07-25 found them independently drifted in both directions. Diff your copies against the repo's root-level files and reconcile — but reconcile *protocol content*, not vendor-specific wording: the repo root is meant to stay a vendor-neutral template (any MCP client, not just Claude Code), while your bundled skill copy is allowed to be concretely Claude-Code-specific (naming the `Agent` tool, `SendMessage`, `~/.claude/CLAUDE.md`, etc.). A same-day fix on 2026-07-25 initially got this backwards — it copied the Claude-specific skill wording into the repo root — and had to be reverted. Don't repeat that; sync the *rules*, keep vendor-neutral placeholders in the repo copy.
4. **Record the sync.** Once reconciled (or once you've confirmed nothing needed to change), store a memory noting the version you're now synced to, so a future session doesn't have to repeat this check from scratch.

---

> [!IMPORTANT]
> **SQL Access Security:** Agents do not have raw SQL execution permissions. All actions must be performed using the predefined parameterized MCP tools. Do not expose a SQL client tool to agents, as this creates a major database integrity and credentials leak vulnerability.
