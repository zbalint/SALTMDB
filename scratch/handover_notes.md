# SALTMDB Handover Notes & Session Context

This file preserves the complete context of recent developments for the next agent session to resume work smoothly.

## 1. Summary of Completed Releases

Since the last major checkpoint, we successfully shipped versions **`v0.1.0-alpha.16`** through **`v0.1.0-alpha.20`**:

* **v0.1.0-alpha.16:** 
  * Schema migration added `metadata` TEXT column to `entities` database table.
  * Added `metadata_filter` and `explain_mode` to `search_memory`.
  * Implemented `sanitize_fts_query` to prevent FTS5 parser syntax crashes, with dynamic fallback.
  * Implemented duplicate checking (`check_duplicate_memories`) and orphan detection (`detect_orphaned_memories`).
* **v0.1.0-alpha.17:**
  * Fixed event log context overload error by capping the default `limit` to `20` in `get_recent_events` and truncating log payloads > 1000 characters (excluding `consolidation_request` payloads).
* **v0.1.0-alpha.18:**
  * Implemented self-healing port clearance in `start_db_viewer` using active TCP socket probing to kill zombie processes holding port 8080 before spawning the viewer.
* **v0.1.0-alpha.19:**
  * Shipped four specialized Google Antigravity Skills in the `skills/` directory (`ingestion_and_write`, `consolidation`, `lifecycle`, `relations`) to prevent details compression and govern memory promotion to `#core`.
* **v0.1.0-alpha.20:**
  * Renamed skills folders to include prefix (`saltmdb_`) to prevent file path namespace collisions in global CLI skills paths.

---

## 2. Packaged Skills References

The following skills are packaged inside the repository for agent use:

1. **[saltmdb_ingestion_and_write](file:///C:/Users/zbalint/Workspace/SALTMDB/skills/saltmdb_ingestion_and_write/SKILL.md)**: Rules for detail retention, semantic granularity, parent-child linking, and metadata.
2. **[saltmdb_consolidation](file:///C:/Users/zbalint/Workspace/SALTMDB/skills/saltmdb_consolidation/SKILL.md)**: Guidelines for lossless memory merges and resolving pending events.
3. **[saltmdb_lifecycle](file:///C:/Users/zbalint/Workspace/SALTMDB/skills/saltmdb_lifecycle/SKILL.md)**: Logic for temporal updates, archiving, and promoting rules to `#core`.
4. **[saltmdb_relations](file:///C:/Users/zbalint/Workspace/SALTMDB/skills/saltmdb_relations/SKILL.md)**: Guidelines for dependency mapping, CTE tracing, and orphan cleanup.

---

## 3. Current State & Next Steps

* **Current Build Status:** All 25 unit tests pass successfully. Working directory is clean and staged.
* **DB Viewer:** Stopped successfully.
* **Next Steps:**
  * The user has copied the skills to their global path.
  * In the next session, verify that the active agent loads the new `saltmdb_*` skills and successfully utilizes them to ingest files and run lossless consolidations.
