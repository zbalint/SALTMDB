# SALTMDB Exhaustive Audit — 2026-08-23

## Executive conclusion

The SQLite file is structurally sound: `PRAGMA integrity_check` and `quick_check` returned
`ok`, `foreign_key_check` returned no rows, and an exhaustive cross-table scan found no missing
declared foreign-key endpoints, invalid temporal intervals, content-hash drift, retrieval-hash
drift, orphan vectors, stale chunk hashes, or active embedding jobs aimed at missing/archived
entities. The corpus is usable, but deployment is still running schema version 2 and an older
writer reproduced a small lifecycle defect after the prior repair: archived entity
`72307b0c-7076-4691-b1d0-44533ec396a2` has one entity vector and two chunk vectors.

The file also contains 11,161 free pages out of 22,485 (45,715,456 bytes, 49.64% of the
92,098,560-byte database). An offline `VACUUM` is justified. It was deliberately not run during
this audit: all clients and the daemon must first be stopped and a fresh backup must be created,
verified, and retained.

## Scope and method

- Read the requested handover memory `0901825c-04a5-4db3-92a4-8a06c0d38e14`, its linked prior
  invariant audit, and the later correction memory.
- Inventoried every live `sqlite_schema` object and compared it with a database initialized from
  current source.
- Iterated and cross-checked 53,381 rows across all 40 logical, virtual, and virtual-table support
  tables. This included every one of the 1,414 memory/entity rows and every one of the 28,882
  event rows present at scan time.
- Audited entities, events, relations, predicates, tags, entity-tag joins, embedding/retrieval
  jobs, three vector stores, FTS stores/support tables, locks, viewer sessions, telemetry, and
  schema metadata.
- Traced the full unit suite with the Python standard tracer: 57 of 59 package modules were
  imported. The two non-imported modules were the process launcher (`__main__`) and standalone
  snapshot helper (`db.backup`); their code paths were manually inspected. All 16 MCP wrappers
  were exercised by the test run.
- Searched every production entity lifecycle write site. There are no production hard deletes
  of authoritative entities; archive, replacement, consolidation, job cancellation, FTS, and
  vector cleanup paths were reviewed together.

Audit artifacts used during the review are in `/tmp`:
`saltmdb-schema-inventory.json`, `saltmdb-exhaustive-row-audit.json`,
`saltmdb-semantic-inventory.json`, `saltmdb-function-calls.json`, and
`saltmdb-final-review.log`. They are ephemeral supporting evidence, not repository deliverables.

## Live database inventory

At the principal row scan the database held 1,414 entities (774 archived, 492 raw, 148
consolidated; 640 active), 28,882 events, 2,468 relations, 923 tags, 5,002 entity-tag links,
1,677 entity embedding jobs, 10 retrieval embedding jobs, 641 entity vectors, 2,473 chunk
vectors, 9 retrieval vectors, and 1,079 tool telemetry rows. Audit logging and the durable audit
memory can increase append-only counts after these measurements.

All 640 active entities were embedding-ready and had entity and chunk vectors. No active exact
content-hash duplicate groups exist. One active exact-title pair exists: the consolidated
`ea16d3d9-...` and raw corrector `a827ef73-...`, both titled `[Antigravity CLI] Quota Pools,
Model Catalog & Exhaustion Error Format`; their explicit `corrects` relationship makes this a
known correction/history case rather than a silent content duplicate.

The event ledger contained 13,547 historical backlog events (13,046
`consolidation_request`, 501 `supersession_candidate`) and 13,533 matching dismissal records.
The remaining 14 undismissed rows are legacy `supersession_candidate` events from August 7–8.
There are no duplicate dismissal targets, missing dismissal targets, or invalid dismissal JSON.
Fourteen legacy event rows have empty content, which the schema permits. Missing context/session
IDs are widespread legacy/advisory metadata, not relational corruption.

## Findings requiring action

1. **Archived vector recurrence (live deployment):** one archived entity vector and two archived
   chunk vectors remain. Current source now deletes entity, chunk, and retrieval vectors on
   explicit archive, revise/supersede replacement, and consolidation-parent retirement. Schema
   v3 also removes historical archived/orphan vectors atomically.
2. **Schema version lag:** live `PRAGMA user_version` is 2; current source is 3. Deployment of the
   reviewed code is required for automatic cleanup and prevention.
3. **Vacuum warranted:** freelist ratio is 49.64%; `auto_vacuum=0`. Run an offline full `VACUUM`
   after stopping all SALTMDB clients/daemon and verifying a new SQLite backup. Re-run integrity,
   FK, vector, FTS, and count checks afterward.
4. **Stale upgraded index:** live `idx_entities_context` is `(context_id, project_id)`, while a
   fresh database uses `(context_id)`. SQLite's `CREATE INDEX IF NOT EXISTS` silently preserved
   the old definition. Schema v3 now drops/recreates this index inside its savepoint.
5. **Lineage exceptions:** 85 entities have `parent_ids` counts that differ from
   `consolidated_from` edges; one historical memory (`d4a0...`) refers to two absent parent IDs.
   These are known historical provenance gaps. They were not guessed or rewritten.
6. **Ambiguous supersedes graphs:** five current supersedes targets remain active, including
   consolidated `3e3c92bd-...`; they occur in ambiguous graph components. V3 archives only
   connected, acyclic linear chains and leaves branches/cycles for explicit review.

## Historical states that are not corruption

- 172 `_h_` entity IDs are legacy immutable SCD snapshots; relations to them are valid.
- 136 archived former cores retain `is_core=1` for provenance. There are zero active cores. CLI
  corpus health now counts only non-archived cores.
- Thirty self-relations remain as historical rows, but all are invalidated; current self-edges
  are zero.
- 257 current semantic relation rows touch archived nodes. Relations are historical claims by
  design and are not automatically copied, closed, or repointed during lifecycle transitions.
- 199 legacy tags have null stored `normalized_name`; runtime normalization supplies the
  fallback and there are no computed normalized-name duplicate groups.
- 81 active records resemble handovers, 51 active records are unlinked, 29 active records have
  no tags, and 3 active records look test-related. These are corpus-quality worklists, not
  database integrity failures.

## Defects corrected in source

- Added centralized entity/chunk vector deletion and invoked it from every archival lifecycle.
- Added schema-v3 repair of active self-relations, deterministic linear supersedes chains,
  archived temporal bounds, orphan/archived vectors, and jobs.
- Prevented v3 from leapfrogging a failed v2 migration (`user_version == 2`, not a broad gate).
- Made v3 atomic and retryable with a savepoint, including failure-injection coverage.
- Rejected directed cycles as ambiguous; degree checks alone incorrectly classified two-node
  cycles as safe.
- Rebuilt the stale context index during v3.
- Corrected CLI active-core reporting so archived former cores are excluded.
- Expanded regression coverage for explicit archive, revise/supersede predecessor cleanup,
  consolidation-parent cleanup, retrieval cleanup, branching/cyclic graphs, migration rollback,
  prerequisite ordering, stale-index rebuild, and CLI reporting.

## Verification

- Focused lifecycle/schema/CLI suite: **40 tests passed**.
- Full suite outside the socket-restricted sandbox: **1,071 tests passed** in 41.390 seconds.
- The same suite inside the sandbox reached all tests but had three socket `PermissionError`s and
  one daemon startup failure because socket creation is prohibited there; unrestricted rerun
  proves these were environmental.
- Ruff lint: passed.
- Changed-file Ruff formatting: passed after formatting the new test.
- `git diff --check`: passed.
- Repository-wide format checking still identifies pre-existing drift in untouched
  `tests/test_lifecycle_replacements.py`; this audit did not rewrite unrelated user work.
- The optional `coverage` command is not installed. Stdlib trace/profiling was used instead;
  profiler runs inside the sandbox share the socket limitation described above.

## Safe deployment and maintenance order

1. Stop the daemon, adapters, viewer, and all other database clients.
2. Create a fresh backup with SQLite's backup API; run `quick_check` on the backup and record its
   SHA-256 digest.
3. Deploy the reviewed source and start once so schema v3 runs. Confirm `user_version=3`, the
   context index has only `context_id`, archived/orphan vector counts are zero, and ambiguous
   supersedes components were only reported—not guessed.
4. Stop clients again and run full `VACUUM` on the live database.
5. Re-run `integrity_check`, `foreign_key_check`, row counts, FTS/entity parity, embedding/job
   invariants, lineage exceptions, and file/page statistics. Retain the pre-maintenance backup
   until the post-maintenance audit is accepted.

The audit intentionally did not mutate the live database, deploy the code, resolve ambiguous
lineage, or run `VACUUM`. Those are operational actions that require a controlled client outage;
performing them against a live daemon would weaken rather than strengthen the evidence.

## Maintenance addendum — VACUUM completed

At the user's explicit request, the offline maintenance procedure was completed later on
2026-08-23. Daemon PID 95777 was the only live database holder; it received `SIGTERM`, exited
cleanly, removed its discovery file, and `lsof` then showed zero holders.

A pre-VACUUM SQLite backup was created at
`/home/zbalint/.saltmdb/backup/saltmdb.db.bak_20260823_142623_123461_pre_vacuum_508ee641`.
It is 92,098,560 bytes, passed `integrity_check`, matched the live entity/event/relation counts,
and has SHA-256 `94f455f801e6dc6397162b1a30451593cad86559b8e8ff58d25204a351ae1a49`.

`VACUUM` completed in 2.763 seconds. The live file shrank from 92,098,560 to 45,305,856
bytes, reclaiming 46,792,704 bytes (50.8%). Page count fell from 22,485 to 11,061 and freelist
count from 11,164 to zero. Post-maintenance `integrity_check` and `quick_check` returned `ok`,
`foreign_key_check` returned no rows, all compared logical table counts matched the backup, and
the exhaustive 53,436-row scan completed. The known pre-deployment archived-vector residue
(one entity vector and two chunk vectors for `72307b0c-7076-4691-b1d0-44533ec396a2`) remains;
VACUUM compacts storage but does not alter logical rows. The daemon remains stopped for the
pending deployment, preventing the old writer from recreating further lifecycle drift.
