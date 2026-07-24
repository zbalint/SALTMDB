# SALTMDB Database Migration Log & Guide

This document tracks schema modifications across alpha versions and provides instructions and SQL statements for migrating production databases.

---

## Version Schema Registry

| Package Version | Schema Version | Modifications | Migration Action |
| :--- | :---: | :--- | :--- |
| `v0.1.0-alpha.6` | 1 | Initial FTS5 virtual tables and events schema | Baseline schema creation |
| `v0.1.0-alpha.7` | 2 | Added temporal SCD columns (`valid_from`, `valid_to`) to `entities`; added `relations` edge table | Column migrations on `entities`; DDL creation of `relations` |
| `v0.1.0-alpha.8` | 2 | No schema changes. Updated Python tool signatures, deduplication logic, tag normalization, and event-read tools | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.9` | 2 | No schema changes. Added Relations Topology graph visualizer and modal click-through links to database viewer | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.10` | 2 | No schema changes. Fixed string escaping syntax error on Outfit font-family definition in server JS block | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.11` | 2 | No schema changes. Added stop_db_viewer MCP tool for programmatic viewer lifecycle control | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.12` | 2 | No schema changes. Fixed path leak in MIGRATION.md and build_review_report.py by resolving paths dynamically | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.13` | 2 | No schema changes. Mocked viewer subprocess inside unit tests to prevent test environment variable pollution | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.14` | 2 | No schema changes. Added archive_memory MCP tool and implemented dynamic pending/resolved event status mapping | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.15` | 2 | No schema changes. Redirected database viewer background startup logs to viewer.log and added startup integrity check | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.16` | 3 | Added `metadata` TEXT column to `entities` for JSON structured filtering; added safe FTS5 query parser fallback, search explain mode, orphan detection, and duplicate checking | **Column Migration on `entities`** (handled automatically or manually via ALTER TABLE) |
| `v0.1.0-alpha.17` | 3 | No schema changes. Capped default get_recent_events limit to 20 and truncated large operational log payloads | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.18` | 3 | No schema changes. Added self-healing zombie port clearance check to start_db_viewer | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.19` | 3 | No schema changes. Shipped specialized Google Antigravity Skills for memory ingestion, consolidation, lifecycle, and relations | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.20` | 3 | No schema changes. Renamed skills folders to include prefix to prevent name collisions in global CLI skills paths | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.21` | 4 | Configured FTS5 Porter tokenizer, added search_aliases metadata column; migrated project_id to entities and session_id to events; renamed store_knowledge to store_memory; changed consolidation to soft-archiving | **Automatic Migration** (init_db runs column changes, Drops/Recreates FTS5 index automatically) |
| `v0.1.0-alpha.22` | 4 | Added E2E test suite; fixed title secret redaction, tag search canonicalization, duplicate check null safety, self-referential relations, bulk consolidation relation re-pointing, viewer CORS OPTIONS preflight, pagination bounds, and connection leaks | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.23` | 4 | Usability refactoring following subagent blind testing; implemented smart UUID/title entity resolution, tool parameter aliasing (query, event_type, text, tag, owner, etc.), init_db default arguments, flexible bulk string archiving, and configurable viewer port | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.24` | 5 | REVIEW_1.md architectural overhaul; added context_id domain-agnostic scoping; demoted owner_id to provenance metadata for shared memories; removed LRU decay entirely; added query stop-words normalization, include_related search, graph_exhausted signal, analyze_lineage tool, and auto-linked consolidated_from lineage edges | **Automatic Migration** (init_db auto-adds context_id columns to entities & events tables) |
| `v0.1.0-alpha.25` | 5 | Viewer & Server comprehensive audit fixes; fixed bulk_commit_consolidation predicate direction to `consolidated_from`, context_id column persistence in store_memory & log_event, start_db_viewer port check; overhaul of database viewer SPA with Stats Dashboard, FTS Search UI, server-side filter dropdowns, Lineage tab, complete entity detail modal, and interactive graph zoom/pan & predicate legend | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.26` | 5 | Fixed critical frontend JavaScript regex syntax crash in `saltmdb_viewer.py` by converting SPA HTML templates to raw string literals (`r"""..."""`); fixed `start_db_viewer` environment and `--port` parameter passing; updated `get_entities` empty string filter logic; updated `get_lineage` entity resolution by ID or title | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.27` | 5 | Codebase refactored from single `saltmdb_server.py` monolith into `src/saltmdb` package layout (`db/`, `domain/services/`, `mcp/`, `viewer/`, `utils/`); entry point changed from `saltmdb_server.py` to `python -m saltmdb` | **No Action Required** (no schema change; update MCP client config to use `python -m saltmdb` instead of `saltmdb_server.py`) |
| `v0.1.0-alpha.28` | 5 | No schema changes. Internal service-layer cleanups and stabilization on refactor branch | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.29` | 6 | Added Hybrid FTS5 + Semantic Vector RRF Search: new `sqlite-vec` `entity_embeddings` vec0 virtual table; `embedding_status` column on `entities`; `embedding_service.py` for lazy `fastembed` ONNX inference; parallel `ThreadPoolExecutor` search with RRF merge; `SALTMDB_ENABLE_SEMANTIC` env-var flag (read-path only) | **Automatic Migration** (`init_db` auto-adds `embedding_status` column and creates `entity_embeddings` virtual table; run `scratch/backfill_embeddings.py` once for pre-existing rows) |
| `v0.1.0-alpha.30` | 6 | Major audit release: added 11 SQL performance indexes; bounded `ThreadPoolExecutor` for background embeddings; security hardening (CORS restricted to localhost, raw exception leakage suppressed, redaction fast-path bypass fix); viewer SPA enhancements (server-side tag filtering, dedicated `/api/relations/graph` endpoint, PID tracking for clean stop); MCP API standardization (`commit_consolidation` `owner_id`/`context_id` params, `get_canonical_tags` `query` rename); complete documentation sync across README, INSTALL, AGENT_GUIDE, and pyproject.toml | **Automatic Migration** (`init_db` creates secondary SQL indexes automatically) |
| `v0.1.0-alpha.31` | 6 | Hotfix release: fixed thread-safety race condition in hybrid search by giving `semantic_search` its own dedicated SQLite connection; fixed `embedding_status` showing as `pending` for archived memories in web UI/API (added SCD2 history status copying, viewer detail API field fix, and automatic DB migration/trigger) | **Automatic Migration** (`init_db` auto-backfills `embedding_status = 'archived'` for existing archived memories) |
| `v0.1.0-alpha.32` | 6 | Production release: enabled Hybrid FTS5 + Dense Vector RRF search by default across all clients (`SALTMDB_ENABLE_SEMANTIC` defaults to `true`); fixed `store_memory` background embedding parameter resolution (`target_db`); fixed `sqlite-vec` virtual table upsert syntax (`DELETE` + `INSERT`); added automatic server startup embedding backfill queue; preserved score calculation precision (6 decimal places); verified 100% blind retrieval accuracy in multi-agent stress benchmark | **No Action Required** (fully backward-compatible) |
| `v0.1.0-alpha.33` | 6 | Production release: fixed MCP tool parameter alias resolution (`query`, `q`, `keywords`) in `search_memory` wrapper; made `include_related=True` default in `search_memory` so agents automatically receive 1-hop knowledge graph relations without explicit parameters; expanded test suite to 22 unit, integration, and E2E tests | **No Action Required** (fully backward-compatible) |

---


## DDL Migrations (v0.1.0-alpha.6 ➔ v0.1.0-alpha.7)

If you are upgrading an existing production `saltmdb.db` database from `v0.1.0-alpha.6` manually (rather than allowing `init_db` to run migrations automatically), run the following SQL statements:

```sql
-- 1. Enable temporal columns on entities table
ALTER TABLE entities ADD COLUMN valid_from TEXT;
ALTER TABLE entities ADD COLUMN valid_to TEXT;

-- 2. Create the typed relationship edges table
CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    created_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
);

-- 3. Create index for relation lookups to accelerate CTE recursive traversals
CREATE INDEX IF NOT EXISTS idx_relations_source_target ON relations (source_id, target_id);
```

---

## DDL Migrations (v0.1.0-alpha.15 ➔ v0.1.0-alpha.16)

If you are upgrading an existing production `saltmdb.db` database from `v0.1.0-alpha.15` manually, run the following SQL statement:

```sql
-- Add metadata JSON column to entities
ALTER TABLE entities ADD COLUMN metadata TEXT;
```

---

## DDL Migrations (v0.1.0-alpha.28 ➔ v0.1.0-alpha.29)

If you are upgrading an existing production `saltmdb.db` manually (rather than allowing `init_db` to run migrations automatically), run the following steps:

```sql
-- 1. Add embedding_status tracking column
ALTER TABLE entities ADD COLUMN embedding_status TEXT DEFAULT 'pending';

-- 2. Mark existing archived memories as 'archived' in embedding_status
UPDATE entities SET embedding_status = 'archived' WHERE status = 'archived';
```

> [!IMPORTANT]
> **Order of Operations for Backfill:**
> Before running `backfill_embeddings.py`, the `entity_embeddings` virtual `vec0` table **must** be created. 
> 
> You can create it by either:
> 1. Launching the server once (`python -m saltmdb`), or
> 2. Running `python -c "from saltmdb.db.schema import init_db; init_db()"`
> 
> If `backfill_embeddings.py` is executed before initializing `init_db()`, embedding generation will fail with `no such table: entity_embeddings` and mark entity statuses as `failed`. If this happens, run `init_db()`, reset failed records (`UPDATE entities SET embedding_status = 'pending' WHERE embedding_status = 'failed'`), and re-run `backfill_embeddings.py`.

Finally, run the one-time backfill script to generate embeddings for existing rows:
```bash
python scratch/backfill_embeddings.py
```

---

## Upgrade Verification

To verify your database schema compatibility, run the hybrid search test suite:

```bash
python -m pytest scratch/test_hybrid_search.py -v
```
If all tests execute and pass cleanly, your database schema is correctly aligned.
