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

## Upgrade Verification

To verify your database schema compatibility, execute **[examples/query_db.py](file:///C:/Users/zbalint/Workspace/SALTMDB/examples/query_db.py)** or run unit tests:

```bash
$env:PYTHONPATH="C:\Users\zbalint\Workspace\SALTMDB"
python scratch/test_db.py
```
If all unit tests execute and pass cleanly, your database schema is correctly aligned.
