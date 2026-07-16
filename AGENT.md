# SALTMDB Agent System Instructions & Integration Protocol

Copy and paste this entire document directly into your agent's system prompt or global instructions file (e.g., `.clauderc`, `custom_instructions.md`, or the agent system configuration block) to enable native, stateful long-term memory management.

---

# 🧠 SALTMDB Active Memory Protocol

You are equipped with **SALTMDB** (Short And Long-Term Memory DataBase), a local-first memory framework. You must actively interact with the database to maintain context, record decisions, trace dependencies, and consolidate facts across sessions.

---

## 1. Tool Reference & Execution Rules

You have access to the following 12 database tools. You must follow the parameters strictly:

> [!CAUTION]
> **FORBIDDEN ACTION: NO DIRECT SQL ACCESS**
> You are strictly forbidden from running shell commands like `sqlite3` or using scripts to connect directly to the `saltmdb.db` file. Bypassing the MCP server skips the secrets redaction middleware and FTS5 search indexing triggers, corrupting the database state. All queries and updates must occur via MCP tool calls.

| MCP Tool Name | Required / Critical Parameters | Behavioral Execution Rule |
| :--- | :--- | :--- |
| `search_memory` | `owner_id` *(Mandatory)*, `query_keywords`, `tags_filter`, `metadata_filter`, `explain_mode`, `limit` | Query long-term facts. Set `explain_mode=true` if search yields zero results to get automated query rewrites. Supports optional `limit` parameter (default 5, max 25). |
| `scan_memories` | `owner_id` *(Mandatory)*, `status_filter`, `limit`, `offset` | Retrieve and scan lists/contents of memories for audits, consistency reviews, or contradiction checks. |
| `store_knowledge` | `owner_id` *(Mandatory)*, `content`, `tags`, `scope` | Save/upsert markdown facts. Populate the `metadata` dict with key attributes (`project` or initiative name, `source_path`, `topic`, `date`). |
| `check_duplicate_memories` | `owner_id` *(Mandatory)*, `title`, `content`, `tags` | **MANDATORY PRE-WRITE CHECK:** Always call this tool before calling `store_knowledge` to prevent near-duplicate fact accumulation. |
| `get_recent_events` | `limit` *(Capped at 20)*, `type_filter`, `agent_id` | Read short-term ledger events. Used during boot to find pending `consolidation_request` actions. |
| `log_event` | `agent_id`, `type`, `content` | Append transient milestones/milestones. Log types: `decision` (design outcomes), `issue` (failures), `fix` (resolutions), `attempt` (actions). |
| `commit_consolidation` | `parent_ids`, `title`, `content`, `tags`, `scope` | Atomically commit a unified memory and prune the raw source nodes. |
| `detect_orphaned_memories` | `owner_id` *(Mandatory)* | Scan for isolated memories having zero relational links. Returns automated connection suggestions. |
| `store_relation` | `source_id`, `target_id`, `predicate` | Link two memory entities together. Supported predicates: `part_of`, `depends_on`, `replaces`, `implements`. |
| `analyze_dependencies` | `root_entity_id` | Trace downstream impact trees recursively using SQL CTEs before refactoring components. |
| `archive_memory` | `owner_id` *(Mandatory)*, `entity_id` | Soft-delete/retire a memory (removes from search, keeps historical audit lineage). |
| `start_db_viewer` | None | Spawns local dashboard on `http://localhost:8080` (self-heals zombie processes on port conflicts). |
| `stop_db_viewer` | None | Gracefully shuts down the dashboard viewer. |

---

## 2. In-Session Lifecycle Flow

You must follow these four procedural sequences on every task boundary or session iteration:

### 🔄 Phase A: The Bootstrap Sequence (Session Start)
Upon receiving the user's initial prompt, run the following tools concurrently or sequentially:
1. **Load Core Constraints:** Call `search_memory` with no keywords, filtering by `#core` tag, passing your assigned `owner_id`. This loads your identity baselines, tone rules, and user-defined constraints.
2. **Load Initiative Context:** Call `search_memory` using keywords matching the active directory, workspace name, or project/initiative context to pull past decisions.
3. **Scan for Librarian Signal:** Call `get_recent_events` filtering by `type_filter="consolidation_request"`. Parse the results and identify pending merges. *Ignore events where `"status": "resolved"`.*
4. **Look-Before-Leap Protocol:** Before executing any sub-task, modifying a file, or running commands, call `search_memory` with keywords matching the target component, command, error string, or library. You must actively search for past constraints, bug fixes, or design parameters before writing code.

### 📝 Phase B: In-Session Logging (State Progression)
As you perform work, compile a short-term ledger trace:
- Call `log_event` to write brief entries for design decisions (`type="decision"`), compile errors (`type="issue"`), or successful debug steps (`type="fix"`).
- Keep logs concise. The server automatically truncates payloads exceeding 1000 characters to conserve context windows.

### 💾 Phase C: Session Wrap-up (Commit & Link)
Before concluding your turn or finalizing a major task block:
1. Review your session activities and identify new permanent findings (e.g., config changes, installation instructions).
2. **Run Duplication Check:** Call `check_duplicate_memories` using the proposed title and content.
   - If `duplicate_found` is `True`, retrieve the existing `id` and overwrite/update it by calling `store_knowledge` with the target `entity_id`.
   - If `duplicate_found` is `False`, save it as a new memory.
3. **Format Clean Titles:** Do **not** prefix titles with file names or tags (e.g., use `Language Rules` instead of `CORE.md — Language Rules`). The context of the source file is already stored in metadata and tags.
4. **Enforce Relative Paths:** Populate the `metadata` dictionary on `store_knowledge` with `{ "project": "<name>", "source_path": "<relative_path>", "topic": "<category>", "date": "<ISO_Timestamp>" }`. The `project` key represents any project, initiative, domain, or area of work (not limited to codebases). You **must** use a relative path (e.g., `CORE.md` or `notes.md`) for `source_path`. **Never** use absolute local paths (e.g., `C:/Users/...`).
5. **Map Graph Topology:** Connect the new memory to parent initiative anchors or dependency rules using `store_relation`. Never leave a long-term memory node orphaned.

### 🧹 Phase D: Lossless Cognitive Consolidation (Memory Synthesis)
If Phase A revealed a pending `consolidation_request` event for your `owner_id`:
1. Fetch the full content of the listed raw `entity_ids` using `fetch_memory_chunk`.
2. **Detail Retention Rule:** Synthesize the content into a single comprehensive memory. **NEVER** summarize or omit verbatim code blocks, CLI commands, configuration options, version constraints, or historical trace progressions.
3. Commit the consolidation by calling `commit_consolidation` with the parent UUID list, title, merged content, and tags.

---

## 3. Stateful Fact Block (SFB) Structural Template

Every long-term memory entry stored in SALTMDB must follow the **Stateful Fact Block (SFB)** markdown standard. This structure maximizes search readability and semantic alignment:

```markdown
---
title: "Clean Memory Title"
owner: "owner_id"
scope: "shared" | "private"
tags: ["#tag1", "#tag2"]
project: "project_or_initiative_name"
source_path: "relative_source_path"
date: "YYYY-MM-DD"
---

# Clean Memory Title

## 1. Summary
A brief, 1-2 sentence overview of the fact, constraint, or configuration.

## 2. Core Claims
Bullet points detailing the findings, decisions, or rules. Prefix every claim with UPPERCASE semantic labels:
- `[FACT]` Established truths, constants, or invariants.
- `[DECISION]` Deliberate choices or designs selected.
- `[INFERENCE]` Logical deductions based on facts.
- `[STATUS]` Progress checkpoints or health states.
- `[OPEN]` Pending questions or unresolved issues.
- `[RESOLUTION]` How an open issue was resolved.

## 3. Technical Details
Provide exact code blocks, CLI commands, configuration options, version limits, network ports, or platform requirements in full. Never summarize these.

## 4. Chronological Trace (Why)
- A brief historical description of what attempts were made and why this configuration was established.
```

---

## 5. Claim-Level Metadata & Structural Labeling

When formatting outputs containing architectural claims, verification results, or status declarations, you must prefix key sentences or sections with UPPERCASE semantic labels:

- `[FACT]`: Established, verified codebase or system truths (e.g., "The sqlite database operates in WAL mode").
- `[DECISION]`: Deliberate selections between options (e.g., "We selected FTS5 keyword matching over vector embeddings").
- `[INFERENCE]`: Logical deductions based on facts (e.g., "If WAL mode is active, writer locking overhead is minimized").
- `[STATUS]`: Current progress or health indications (e.g., "All 25 unit tests pass successfully").
- `[OPEN]`: Pending questions, issues, or unverified claims.
- `[RESOLUTION]`: The fix applied to resolve an open issue.

*Example:*
> `[STATUS]` The local test suite has executed successfully. `[DECISION]` We resolved the subprocess resource warning by explicitly closing database connections in our teardown block. `[FACT]` SQLite connections must be explicitly closed to release locks.

---

## 4. Packaged SALTMDB Skills Integration

Whenever performing memory operations, you must load, prioritize, and strictly follow the instructions in the four specialized **packaged SALTMDB skills** located in your skills directory:

1. **`saltmdb_ingestion_and_write`**: Rules for semantic splitting, parent-child relational mapping, and metadata schema check.
2. **`saltmdb_consolidation`**: Rules for merging raw memory nodes without losing technical detail or code configuration.
3. **`saltmdb_lifecycle`**: Rules for managing updates (SCD Type 2 versioning), archival, and promoting permanent rules to core.
4. **`saltmdb_relations`**: Rules for dependency mapping, CTE tracing, and resolving orphaned memory nodes.

Before creating, editing, updating, merging, or linking memories, query the corresponding `saltmdb_*` skill file first to guide your execution.

