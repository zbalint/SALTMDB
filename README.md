# SALTMDB: Local-First MCP Memory Server

**SALTMDB** (Short And Long-Term Memory DataBase) is a centralized, local-first memory framework designed for AI CLI tools and agents (such as Antigravity, Copilot, and Claude Code). It acts as a shared memory layer, allowing multiple concurrent agents to read, write, and consolidate contextual facts without heavy ML dependencies, vector databases, or high API overhead.

> [!TIP]
> * **Installation:** To install and register the MCP server, see the **[Installation Guide](INSTALL.md)**.
> * **Developer Guide:** To learn how to configure your AI agents to utilize this memory system, read the **[Agent Integration & Design Guide](AGENT_GUIDE.md)**.

---

## 🏛️ System Architecture

SALTMDB is built using standard Python libraries and SQLite, prioritizing concurrency safety, security, and low memory overhead.

```mermaid
graph TD
    subgraph Active Agents
        A[Antigravity CLI]
        B[Copilot / Claude Code]
    end

    subgraph MCP Server Layer
        A -->|Stdio / MCP| Server[saltmdb_server.py]
        B -->|Stdio / MCP| Server
        Server -->|timeout=10.0 / WAL| MainDB[(sqlite3: saltmdb.db)]
        Server -->|check_same_thread=False| EphemDB[(sqlite3: :memory:)]
    end

    subgraph Background Process
        Server -->|Asynchronous detached spawn| Lib[Librarian gc worker]
        Lib -->|Atomic Leader Election Lock| Lock[_system_locks]
        Lib -->|Consolidate & Archive| MainDB
    end
```

## Features

- **Local-First & Lightweight:** Zero cloud dependencies. Uses SQLite (WAL mode) with `fastembed` (`onnxruntime`) and `sqlite-vec`.
- **Hybrid Search (FTS5 + Vector RRF):** Parallel FTS5/BM25 keyword search and `BAAI/bge-small-en-v1.5` dense vector search combined via Reciprocal Rank Fusion (RRF). Feature-gated via `SALTMDB_ENABLE_SEMANTIC=true`.
- **Secrets Redaction:** Built-in pattern matching to automatically redact API keys, secrets, and private paths before storage.
- **Folksonomy & Canonical Tags:** Flexible tagging system supporting tag aliases and canonical resolution.

### 1. Database Schema
The SQLite database operates in **Write-Ahead Logging (WAL)** mode for safe concurrent readers. It includes the following tables:
* **`events`**: An immutable, append-only ledger tracking agent operations (issues, attempts, decisions, fixes).
* **`entities`**: The long-term knowledge base storing facts, markdown content, weights, and status fields (`raw`, `consolidated`, `archived`).
* **`tags`**: A folksonomy table allowing tags, categorizations, and canonical redirects.
* **`entity_tags`**: A mapping table linking knowledge entities to folksonomy tags.
* **`entities_fts`**: A virtual table using **SQLite FTS5** to index entity titles and full contents for fast, weighted keyword searches.
* **`_system_locks`**: A system table facilitating leader election mutex locks for concurrent processes.

---

## 🚀 Core Features

### 1. Weighted Keyword FTS5 Search
SALTMDB bypasses expensive vector embedding models in favor of standard keyword search. It leverages SQLite FTS5's built-in `bm25` auxiliary function configured with a **10:1 title-to-content weight ratio**:
* Matches found in the entity's **Title** are prioritized 10x higher than matches in the **Body Content**.
* The final rank score merges BM25 ranking and the entity's priority `weight` multiplier.

### 2. Hybrid Title Extraction
When storing new knowledge, agents can optionally specify a custom `title`. If omitted, the server automatically extracts the first markdown heading (`# Heading`) as the title, falling back to a snippet of the first line if no heading is present.

### 3. Security & Redaction Middleware
Before any database writes occur, the text is evaluated by a regex-based scrubbing pipeline:
* **Core Redactions:** Automatically censors standard credentials (GitHub tokens, Anthropic API keys, OpenAI API keys, AWS credentials, and Discord tokens).
* **Custom Developer Rules:** On startup, the server reads `.saltmdb_redact` from the current working directory. You can add one custom regex pattern per line (e.g. internal staging domains, proprietary IDs) to strip out company-specific secrets.

### 4. Ephemeral State Layer
For temporary data (like short-lived session tokens, OTPs, or process variables), the server maintains an isolated `:memory:` SQLite database. These variables are never written to disk and disappear completely when the server stops.

### 5. Atomic Leader Election Mutex
To prevent multiple parent processes from launching redundant garbage collection tasks simultaneously, the server uses an **Atomic SQLite lock** in the `_system_locks` table.
* The lock uses a **10-minute expiry safety net**. If a terminal session crashes mid-run, the lock automatically expires, preventing permanent deadlocks.

---

## 🧹 The Librarian Process (Garbage Collection)

Whenever the database is modified, the server asynchronously spawns a detached background instance of the server in Librarian mode (`python saltmdb_server.py --librarian`):
* **Windows Detachment:** Spawns with `0x08000000` (`CREATE_NO_WINDOW`) to prevent distracting terminal window popups.
* **Unix Detachment:** Spawns with `start_new_session=True` so it survives parent process termination.

Once the background Librarian acquires the atomic lock, it runs the following tasks:
1. **Tag Merging:** Merges case-insensitive tag aliases (e.g. `#Auth-Error` and `#auth_error`) into a canonical tag to prevent folksonomy fragmentation.
2. **Lossless Memory Preservation (No LRU Decay):** Unaccessed memories are never archived or weight-decremented based purely on access recency. Archiving occurs only upon explicit supersession or synthesis consolidation, preserving rare-but-important root cause knowledge indefinitely.
3. **Clutter Tag Consolidation (Request-based):** Identifies tags accumulating $\ge 5$ raw entries and logs a JSON-formatted `consolidation_request` event to the short-term `events` ledger.
4. **General Consolidation (Request-based):** Identifies overall raw accumulation ($\ge 5$ items sharing owner/scope) and logs a `consolidation_request` event. The cognitive task of merging and rephrasing markdown is offloaded to the active client agent, ensuring the server runs fully offline without independent API requirements.

---

## 🛠️ API & MCP Tools Reference

The server exposes 18 tools over standard I/O:

| Tool Name | Parameters | Description |
| :--- | :--- | :--- |
| `log_event` | `agent_id`, `type`, `content`, `error_code`, `session_id`, `context_id` | Appends a scrubbed entry to the immutable short-term ledger. |
| `get_recent_events` | `agent_id` (optional), `type_filter` (optional), `limit` | Retrieves events logged to the short-term ledger, allowing agents to read consolidation requests. |
| `get_canonical_tags` | `domain` (optional) | Queries non-alias tags matching the search filter (or alias parameters `query`, `substring`, `tag_filter`). |
| `store_memory` | `content`, `tags`, `owner_id`, `scope`, `weight`, `is_core`, `title`, `entity_id`, `metadata`, `context_id` | Stores/upserts facts in raw markdown. Validates mandatory `content` and `title`. |
| `search_memory` | `query_keywords`, `tags_filter`, `owner_id`, `metadata_filter`, `explain_mode`, `include_related`, `context_id` | Searches knowledge using FTS5 with natural language stop-word normalization, tag filtering, metadata filters, and optional 1-hop related entity fetching (`include_related`). Demotes `owner_id` to provenance metadata for shared memories (relevance over identity). |
| `fetch_memory_chunk` | `entity_id` | Returns the complete markdown text of a specific entity. Accepts exact UUID, status string containing UUID, or entity title. |
| `archive_memory` | `entity_id`, `owner_id` | Explicitly archives (retires) a long-term memory, marking it as inactive. |
| `detect_orphaned_memories`| `owner_id` | Identifies active memories with no relationship links and suggests candidate links based on tag overlap. |
| `check_duplicate_memories`| `title`, `content`, `owner_id`, `tags` | Checks the database for potential near-duplicates of a proposed memory using stemming and stop-word similarity. |
| `store_ephemeral_memory`| `key`, `value` | Saves a volatile secret to the in-memory database. |
| `get_ephemeral_memory` | `key` | Retrieves a volatile secret. |
| `commit_consolidation` | `parent_ids`, `title`, `content`, `tags`, `scope`, `weight` | Atomically commits a consolidated memory, archives parent raw nodes (never deletes), and auto-links `consolidated_from` lineage edges. |
| `store_relation` | `source_id`, `target_id`, `predicate` | Stores a directional semantic relationship edge between two entity nodes. Auto-resolves UUIDs from titles or status strings. |
| `analyze_dependencies` | `root_entity_id`, `max_depth` | Traverses relationship trees using recursive SQL CTEs to map downstream components. Returns `graph_exhausted` signal. |
| `analyze_lineage` | `entity_id` | Traverses full multi-generation consolidation and derivation ancestry (`consolidated_from` / `derived_from`). |
| `create_snapshot` | None | Safely creates a timestamped database backup in `backups/` using SQLite's backup API. |
| `start_db_viewer` | `port` (optional, default 8080) | Launches the zero-dependency database dashboard viewer locally on specified port. |
| `stop_db_viewer` | None | Terminates the database dashboard viewer running on port 8080 or specified port. |

---

## ⚙️ Configuration & Installation

### 1. Configuration Path
By default, the server initializes the database under `~/.saltmdb/saltmdb.db`. You can override this behavior by setting the `SALTMDB_DB_PATH` environment variable:
```bash
$env:SALTMDB_DB_PATH = "C:\custom_path\memory.db"
```

### 2. Registering with MCP Clients
To connect SALTMDB to Claude Desktop or Claude Code, add the following to your configuration file:
```json
"mcpServers": {
  "saltmdb": {
    "command": "python",
    "args": ["/path/to/SALTMDB/saltmdb_server.py"],
    "env": {
      "GEMINI_API_KEY": "YOUR_GEMINI_API_KEY"
    }
  }
}
```

### 3. Database Dashboard Viewer
SALTMDB includes a sleek, zero-dependency dark-mode dashboard to inspect events, memories, tags, and system lock states:
1. Run the viewer script locally:
   ```bash
   python saltmdb_viewer.py
   ```
2. Open your web browser and navigate to:
   [http://localhost:8080](http://localhost:8080)

### 4. Running Unit Tests
You can run the test suite to verify database schemas, triggers, and lock rules:
   ```bash
$env:PYTHONPATH="C:\path\to\SALTMDB"
python scratch/test_db.py
```

---

## 🧠 Google Antigravity Skills

SALTMDB ships with four specialized **Google Antigravity Skills** to guide agents in executing high-quality memory management tasks:

* **`saltmdb_ingestion_and_write`** (`skills/saltmdb_ingestion_and_write`): Rules for semantic document splitting (granularity rule), parent-child graph linking, and metadata tagging to prevent monolithic content blocks.
* **`saltmdb_consolidation`** (`skills/saltmdb_consolidation`): Guidelines for lossless cognitive consolidation, ensuring code blocks, parameter listings, and version histories are fully preserved during memory merges.
* **`saltmdb_lifecycle`** (`skills/saltmdb_lifecycle`): Best practices for SCD Type 2 updates, archiving obsolete records, and promoting system rules and constraints to core memories.
* **`saltmdb_relations`** (`skills/saltmdb_relations`): Graph topology management, standard relationship predicates, recursive CTE impact tracing, and resolving orphaned memory nodes.

### Skill Installation
To make these skills available to your active `copilot-cli` or `agy` CLI/Antigravity agents, copy the folders from `skills/` directly to your local skills directory:
```bash
Copy-Item -Path "skills/*" -Destination "$HOME/.gemini/antigravity-cli/builtin/skills/" -Recurse -Force
```

---

## 📄 License & Community

* **License:** Distributed under the **[GNU Affero General Public License v3 (AGPLv3)](LICENSE)**.
* **Contributing:** Read the **[Contributing Guidelines](CONTRIBUTING.md)** for details on testing and branch setups.
* **Conduct:** We adhere to the **[Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md)**.