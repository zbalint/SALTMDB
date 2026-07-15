# SALTMDB
An Sqlite backed memory system for AI agents

SALTMDB is a perfect choice. It sounds exactly like a robust, low-level infrastructure tool built for developers, fitting right in alongside names like SQLite and MongoDB. It clearly communicates exactly what it does: Short And Long-Term Memory DataBase.

Here is the final, updated implementation plan with the SALTMDB branding incorporated. You can copy this exactly, save it as `saltmdb_architecture.md`, and feed it straight to Antigravity CLI.

---

# SALTMDB: Local-First MCP Memory Server Implementation Plan

## 1. Project Overview & Constraints

You are tasked with building **SALTMDB** (Short And Long-Term Memory DataBase), a robust, centralized memory framework for AI CLI tools.

**Hard Constraints:**

* **No Vector Embeddings / ML Models:** The target machine has low compute and zero budget for API costs. Do not use external vector databases (Pinecone, etc.) or local ML models.
* **Standard Library Only:** The implementation must be in Python using only the standard library (specifically `sqlite3`) and the official MCP SDK. No massive frameworks like LangChain or LlamaIndex.
* **Concurrency:** The system must safely support multiple AI agents (e.g., Antigravity CLI, Copilot) reading and writing simultaneously.
* **Zero Secret Leakage:** Strict redaction middleware must prevent credentials from entering the permanent database.

**Core Architectural Inspirations:**

* *PROJECTMEM Architecture:* Append-only event-sourcing and deterministic pre-action gating.
* *SQLite-backed MCP Memory Patterns:* SQLite WAL mode for concurrency, FTS5 for 1,100x faster keyword search.
* *MemX / CortexaDB Principles:* Local-first deployment, strict metadata filtering, and structural retrieval over fuzzy semantic search.

---

## 2. Database Design (SQLite)

The database must be initialized with **Write-Ahead Logging (WAL)** enabled (`PRAGMA journal_mode=WAL;`).

### Table 1: `events` (Short-Term / Append-Only Ledger)

An immutable log of all actions. No `UPDATE` or `DELETE` allowed.

* `id`: TEXT (UUID) PRIMARY KEY
* `timestamp`: DATETIME DEFAULT CURRENT_TIMESTAMP
* `agent_id`: TEXT (Which agent created it)
* `type`: TEXT (e.g., 'issue', 'attempt', 'fix', 'decision')
* `content`: TEXT (What happened)
* `error_code`: TEXT (Optional)

### Table 2: `entities` (Long-Term Knowledge Base)

Stores consolidated Markdown facts. Uses Soft-Deletes.

* `id`: TEXT PRIMARY KEY
* `created_at`: DATETIME
* `updated_at`: DATETIME
* `owner_id`: TEXT
* `scope`: TEXT ('private' or 'shared')
* `is_core`: BOOLEAN (If TRUE, bypassed search and injected into system prompt)
* `weight`: INTEGER (Priority multiplier for search ranking, default 1)
* `status`: TEXT ('raw', 'consolidated', 'archived')
* `parent_ids`: TEXT (JSON array of original IDs this chunk was merged from)
* `full_content`: TEXT (Markdown text)

### Table 3: `entities_fts` (Virtual Table for Search)

Must use SQLite FTS5 for fast full-text search.

* Uses the `MATCH` operator.
* *CRITICAL TRIGGER:* Create an SQLite trigger named `archive_memory_fts`. `AFTER UPDATE ON entities WHEN NEW.status = 'archived'`, it must automatically delete the corresponding row from `entities_fts` so agents don't search stale data.

### Table 4: `tags` (Folksonomy)

* `id`: TEXT PRIMARY KEY
* `name`: TEXT UNIQUE (e.g., '#auth-error')
* `canonical_id`: TEXT (Self-referential foreign key. If an alias, points to the master tag).

---

## 3. The Security & Ephemeral State Layer

* **The Filter Middleware:** Before executing any `INSERT` into `events` or `entities`, run a regex scrubber. Replace strings matching standard token formats (e.g., `ghp_...`, `sk-ant-...`) with `[REDACTED_SECRET]`.
* **The Ephemeral Database:** On startup, the MCP server must spin up a secondary SQLite connection using the `:memory:` path. Expose a tool for the agent to save temporary variables (like short-lived OTPs or session tokens) here. This data is purposely destroyed when the server stops.

---

## 4. MCP Server Implementation (`saltmdb_server.py`)

Implement an MCP server exposing the following tools to the LLM:

1. `log_event(agent_id, type, content, error_code=None)`: Appends to the `events` table.
2. `get_canonical_tags(domain)`: Queries the `tags` table to suggest existing tags to prevent tag fragmentation.
3. `store_knowledge(content, tags, scope, weight=1, is_core=False)`: Redacts secrets, stores the Markdown chunk in `entities` (status = 'raw'), and updates FTS5.
4. `search_memory(query_keywords, tags_filter)`:
* Executes an FTS5 `MATCH` query.
* Ranks results using a combined score of BM25 + the `weight` column.
* Limits output to top 5 results to save context tokens.
* Returns a JSON object of `id`, `title/snippet`.


5. `fetch_memory_chunk(entity_id)`: Fetches the exact full markdown of a specific ID identified via search.
6. `store_ephemeral_memory(key, value)`: Stores a short-lived string in the `:memory:` database.

---

## 5. The Librarian Process (Garbage Collection)

Include a separate script or CLI flag (e.g., `python saltmdb_server.py --librarian`) designed to run as a background CRON job.

* **Tag Merging:** Queries for recently created tags and merges semantic duplicates into canonical tags via LLM prompt.
* **Consolidation:** Selects rows where `status = 'raw'`, asks the LLM to resolve conflicts and merge them into a single clean Markdown chunk.
* **Lineage Updates:** Inserts the newly consolidated chunk, updates original rows to `status = 'archived'` (which triggers FTS5 cleanup), and tracks the merged IDs in `parent_ids`.

---

## 6. Execution Tasks for the AI Agent

**Agent Instructions: Read this entire document and execute the following steps in order:**

1. **Step 1: Setup:** Create the project directory and initialize `saltmdb_server.py`. Import `sqlite3` and set up the MCP Python standard SDK.
2. **Step 2: Database Initialization:** Write a `init_db(db_path)` function that configures `PRAGMA journal_mode=WAL;` and creates the standard tables (`events`, `entities`, `tags`) and the virtual FTS5 table (`entities_fts`).
3. **Step 3: Triggers:** Write the specific SQL execution commands to attach the `archive_memory_fts` trigger.
4. **Step 4: Security:** Implement a `redact_secrets(text)` helper function using Python's `re` module with common credential patterns.
5. **Step 5: MCP Tools:** Decorate and implement the core MCP functions defined in Section 4 using standard SQL executions. Ensure `redact_secrets()` wraps all inserts.
6. **Step 6: Ephemeral DB:** Implement the `:memory:` database connection and the `store_ephemeral_memory` logic.
7. **Step 7 (Optional/Phase 2):** Draft the structural skeleton for the `--librarian` background script.

---

*Sources & Architectural References embedded in this design:*

* *PROJECTMEM (arXiv:2602.20478)*: Event-Sourced Memory and Judgment Layer.
* *MemX (arXiv:2410.10813)*: Local-First Long-Term Memory System (BM25, FTS5 latency reduction, structural keyword matching).
* *SQLite as MCP Context Saver (DEV Community)*: Guarding LLM context size via localized SQL execution.
* *SQLite-backed MCP Memory Server (GitHub)*: WAL concurrent safety and session tracking patterns.