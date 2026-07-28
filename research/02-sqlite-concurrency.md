# SQLite Multi-Process Concurrency Research — SALTMDB

> **Status: Implemented in v0.1.0-alpha.53** (2026-07-28). The Quick Wins and Medium-Term items
> below (BEGIN IMMEDIATE via `write_transaction_retrying`, retry/backoff, `PRAGMA optimize` +
> WAL-checkpoint logging on close, the Librarian's checkpoint/optimize maintenance duty, and a
> bonus fix for previously-non-atomic bulk operations) were implemented, tested (95/95 passing,
> including a real two-connection contention test proving `busy_timeout` now actually engages on
> write-write lock contention), and landed via a three-role Architect → Developer → Tester
> subagent chain. See `MIGRATION.md`'s `v0.1.0-alpha.53` entry for the exact shipped changes and
> SALTMDB's own memory (context_id `task_sqlite_concurrency_impl_20260727`) for the full decision
> trail. This file is kept as the original historical research record — it is intentionally not
> edited further below this notice.

Scope: real concurrency shape under research is **multiple independent OS processes** (separate
Claude Code / Antigravity / Copilot / Codex / Cursor CLI subprocesses, each potentially spawning
its own SALTMDB MCP server) all opening connections against the **same local SQLite file**
(`~/.saltmdb/saltmdb.db`) on Windows. This is explicitly *not* the "one process, many threads"
case most SQLite tuning content targets.

---

## Research Summary

- **[Write-Ahead Logging (official)](https://www.sqlite.org/wal.html)** — WAL allows unlimited
  concurrent readers alongside exactly one writer; automatic checkpoints default to a 1000-page
  (~4MB) WAL threshold and run in `PASSIVE` mode, which **skips silently** if any reader/writer is
  active — and if there is *always* at least one overlapping reader with no gap, "no checkpoints
  will be able to complete and hence the WAL file will grow without bound." WAL also requires all
  processes to share memory via `-shm`, so it explicitly does not work over a network filesystem.

- **[File Locking And Concurrency In SQLite Version 3 (official)](https://sqlite.org/lockingv3.html)** —
  the actual cross-process serialization primitive is a five-state file lock
  (`UNLOCKED → SHARED → RESERVED → PENDING → EXCLUSIVE`), implemented on Windows via native
  `LockFile()`/`LockFileEx()`/`UnlockFile()` calls. `busy_timeout` is layered on top of this lock
  state machine as a "keep retrying instead of failing immediately" policy — it does not change
  who wins the lock or in what order.

- **[PRAGMA optimize (official docs)](https://www.sqlite.org/pragma.html#pragma_optimize)** — for
  apps with short-lived connections (SALTMDB's exact pattern), the documented recommendation is to
  run `PRAGMA optimize;` once right before closing each connection; long-lived connections should
  instead run `PRAGMA optimize=0x10002` at open and `PRAGMA optimize` periodically. It's normally a
  fast no-op, only running `ANALYZE` when the query planner would actually benefit.

- **[SQLite Over a Network, Caveats and Considerations (official)](https://sqlite.org/useovernet.html)** —
  confirms POSIX/network-filesystem advisory locking is unreliable and has caused real corruption;
  not directly applicable to SALTMDB's local-disk deployment, but worth ruling out explicitly (see
  Open Questions — cloud-synced home directories effectively *are* a network filesystem).

- **[Bert Hubert — "What to do about SQLITE_BUSY errors despite setting a timeout"](https://berthub.eu/articles/posts/a-brief-post-on-sqlite3-database-locked-despite-timeout/)** —
  the sharpest, least-known gotcha found in this research: `busy_timeout` is **not honored** when a
  `DEFERRED` transaction (SQLite/Python's default `BEGIN`) tries to *upgrade* an already-acquired
  read lock to a write lock while another connection holds a write lock — that specific case fails
  immediately with `SQLITE_BUSY` regardless of timeout. Fix: use `BEGIN IMMEDIATE` for any
  transaction that will write, so the write lock is requested up front (where `busy_timeout` *does*
  apply).

- **[SkyPilot — "Abusing SQLite to Handle Concurrency"](https://skypilot.ai/blog/abusing-sqlite-to-handle-concurrency/)** —
  real production war story at much higher scale (1000+ concurrent writer processes) than SALTMDB
  will ever see, but the mechanism is directly relevant: SQLite's busy-retry backoff uses
  hardcoded sleep steps (1, 2, 5, 10, 15, 20, 25ms, ...) with **no FIFO fairness** — arrival order
  does not predict lock-acquisition order, producing a geometric/long-tailed latency distribution
  under contention rather than a bounded queue. Their fix was simply a much larger timeout (60s+)
  plus accepting the residual tail-latency risk; they explicitly did not build a writer-queue.

- **[Hynek Schlawack — "TIL: SQLite WAL Mode Can Lock Short-Lived Readers"](https://hynek.me/til/sqlite-read-only-wal-locked/)** —
  the single most directly-relevant finding for SALTMDB's *specific* architecture: WAL mode's own
  connection open/close coordination (via the `-shm` file), not application data writes, can
  transiently require exclusive coordination locks. This specifically bites systems that open a
  brand-new connection per operation and close it immediately — exactly SALTMDB's
  `get_connection()`-per-request pattern — rather than systems with a small number of long-lived
  pooled connections. Recommended mitigations: non-zero `busy_timeout` (already done), or move
  toward longer-lived/pooled connections.

- **[phiresky — "SQLite performance tuning"](https://phiresky.github.io/blog/2020/sqlite-performance-tuning/)** —
  concrete, widely-cited PRAGMA recipe (`journal_mode=WAL`, `synchronous=NORMAL`,
  `temp_store=MEMORY`, `mmap_size=...`) that SALTMDB's `connection.py` already substantially
  matches; also flags that WAL files can grow unexpectedly large under sustained write load and
  recommends periodic manual `wal_checkpoint(TRUNCATE)`.

- **[Fly.io — "I'm All-In on Server-Side SQLite"](https://fly.io/blog/all-in-on-sqlite-litestream/)**
  and **[Introducing LiteFS](https://fly.io/blog/introducing-litefs/)** — Litestream/LiteFS treat
  WAL + continuous replication as what makes SQLite production-viable; the "unlock" was solving
  backup/disaster-recovery, not concurrent-write throughput — SQLite is still fundamentally
  single-writer underneath, replication just protects against losing that one writer's data.

- **[Expensify — "Scaling SQLite to 4M QPS"](https://use.expensify.com/blog/scaling-sqlite-to-4m-qps-on-a-single-server)**
  — Bedrock's hyperscale numbers come from a custom-built networking/replication layer *on top of*
  a forked SQLite, running one conceptual writer thread serving requests off a queue, on very large
  bare-metal hardware. It's evidence that SQLite's core engine scales far beyond typical
  assumptions — but the actual mechanism (a bespoke replicated write-serialization service) is not
  something to casually replicate for a local dev tool; it solves a fundamentally different
  problem (many networked clients) than SALTMDB has (a handful of local sibling processes).

- **[rqlite design docs](https://rqlite.io/docs/design/) / [Queued Writes](https://rqlite.io/docs/api/queued-writes/)**
  — rqlite's "queued writes" feature batches client writes into a single node-local queue that
  then commits through Raft consensus, trading durability guarantees for throughput. Conceptually
  it's the "single writer + queue" pattern in its purest form — but it exists to amortize
  **network + consensus** round-trips, not local single-file lock contention. Not the same problem
  SALTMDB has.

- **[Turso — "Beyond the Single-Writer Limitation with Turso's Concurrent Writes"](https://turso.tech/blog/beyond-the-single-writer-limitation-with-tursos-concurrent-writes)**
  — Turso/libSQL's `BEGIN CONCURRENT` / MVCC engine is a from-scratch storage-engine rewrite that
  lets multiple writers commit without blocking each other (conflict-checked at commit time). This
  is the "real" fix to SQLite's single-writer limitation, but it requires swapping the entire
  storage engine (adopting libSQL/Turso) — a large, invasive dependency change, not a tuning knob.

- **[Anton Zhiyanov — "SQLite is not a toy database"](https://antonz.org/sqlite-is-not-a-toy-database/)**
  — influential myth-busting essay: the popular belief that SQLite "doesn't support concurrent
  access" is wrong in WAL mode — unlimited concurrent readers plus one writer is enough for the
  overwhelming majority of real applications, and reaching for a client-server DB purely for
  concurrency is usually premature.

- **[oven-sh/bun issue #25964](https://github.com/oven-sh/bun/issues/25964)** — concrete, current
  bug report of SQLite in WAL mode holding a lock on the main `.db` file on Windows past
  `close()` (can't be deleted immediately after), confirming Windows' native `LockFile()` locking
  has real, currently-open edge cases distinct from POSIX behavior.

- **[SQLite Forum — "Can -wal -shm files be deleted if -wal file is empty?"](https://sqlite.org/forum/forumpost/2a8c51e0b8?t=h)**
  — practical operational guidance: `-wal`/`-shm` files are safe to delete only when **no** process
  has the database open; SQLite will transparently rebuild them on next connect. Relevant to any
  manual recovery runbook after a crashed multi-agent session leaves stale WAL/SHM files.

---

## Current SALTMDB State

As grounded by the task (not re-derived from code in this research pass):

- `connection.py` calls `sqlite3.connect()` **fresh, per request/operation** — no connection pool,
  no persistent/long-lived connection held by the MCP server process. Every PRAGMA
  (`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=20000`, `cache_size=-64000`,
  `mmap_size=268435456`, `temp_store=MEMORY`, `foreign_keys=ON`) is re-applied on every single new
  connection.
- `busy_timeout` was just raised 5000ms → 20000ms (commit `548d170`) specifically to survive
  multi-agent *startup* contention.
- A separate always-resident in-memory SQLite connection (`:memory:`, `check_same_thread=False`,
  `timeout=10.0`) handles ephemeral/scratch state as a module-level singleton — this one *is*
  long-lived, just not persisted to disk.
- `locks.py` implements a `_system_locks`-table-based leader-election lock so only one process at a
  time runs the background "Librarian" GC/consolidation worker, with a 10-minute crash-safety
  expiry.
- FTS5 (full-text) and `sqlite-vec` `vec0` (embeddings) virtual tables are both kept in sync via
  triggers/explicit writes on every entity insert/update — every logical write fans out into
  multiple physical writes (base table + FTS5 shadow tables + vec0 shadow tables), amplifying WAL
  traffic per logical operation.
- No migration framework; schema evolves via idempotent `ALTER TABLE ... ADD COLUMN` guards run at
  every process startup.
- Deployment is exclusively Windows 10 local machines, one file, no network filesystem involved.

---

## Gaps / Problems

1. **Per-request-connection pattern directly matches the exact anti-pattern flagged by
   [hynek.me](https://hynek.me/til/sqlite-read-only-wal-locked/).** WAL's own connection
   open/close coordination overhead (via `-shm`) is paid on *every single MCP tool call*, not once
   per server lifetime. Under multiple concurrent agent processes, this multiplies the number of
   lock-coordination windows far beyond what a pooled/long-lived-connection design would incur —
   each of those windows is a fresh opportunity for `SQLITE_BUSY`, even for pure reads.

2. **`busy_timeout=20000` may be silently ineffective for exactly the write-write contention it's
   meant to protect against, if writes use Python's default `DEFERRED` transaction behavior.** Per
   [berthub.eu](https://berthub.eu/articles/posts/a-brief-post-on-sqlite3-database-locked-despite-timeout/),
   a deferred transaction that reads then tries to write, while another writer already holds the
   write lock, gets an *immediate* `SQLITE_BUSY` regardless of the timeout value — the 20s ceiling
   would never even be exercised in that failure mode. This is worth confirming in the actual write
   path (flagged in Open Questions since this research pass is web-only).

3. **20s is a real, user-visible stall risk, not a free insurance policy.** Per
   [SkyPilot's findings](https://skypilot.ai/blog/abusing-sqlite-to-handle-concurrency/), SQLite's
   busy-retry backoff has no FIFO fairness — a process can lose the lock race to a newer arrival
   repeatedly. Raising the ceiling reduces *failure rate* but does nothing about *tail latency*;
   for an interactive coding-agent tool, a single MCP call blocking for up to 20 seconds reads to
   the calling agent (and the human waiting on it) as "hung," not "resilient." The 5s→20s bump is a
   reasonable stopgap but is masking, not fixing, underlying lock contention if it's being hit
   routinely rather than only in pathological startup races.

4. **WAL checkpoint starvation is a real, documented risk given SALTMDB's usage pattern**, per the
   [official WAL docs](https://www.sqlite.org/wal.html): default `PASSIVE` autocheckpointing at
   1000 pages silently no-ops whenever a reader/writer overlaps the checkpoint's target range, and
   with multiple independent agent processes plausibly issuing overlapping reads across a session,
   there may rarely be a clean "gap" for a checkpoint to fully complete. Nothing in the current
   design (per the grounding given) actively schedules or verifies checkpoint completion — WAL
   file size is currently unmonitored.

5. **FTS5 + vec0 write amplification compounds both of the above.** Every logical write is
   actually N physical writes (base row + FTS5 index rows + vec0 shadow rows), meaning more WAL
   frames per operation, faster growth toward the 1000-page autocheckpoint threshold, and a larger
   in-flight WAL for any checkpoint attempt to work through.

6. **No `PRAGMA optimize` call anywhere in the described lifecycle.** Given the official guidance
   is specifically written for "short-lived connection" apps — SALTMDB's exact shape — this is a
   free, low-risk, officially-recommended win currently left on the table.

7. **Windows-specific lock behavior is a real, live risk category, not a theoretical one.** The
   [oven-sh/bun bug](https://github.com/oven-sh/bun/issues/25964) shows WAL-mode Windows locks
   currently outliving `close()` in real software; combined with Windows Defender real-time
   scanning or (very commonly, on corporate-managed Windows machines) OneDrive Known Folder
   redirection silently relocating `%USERPROFILE%` under a cloud-sync path, there's a believable
   path to transient file-handle contention on `~/.saltmdb/saltmdb.db` that has nothing to do with
   SQLite's own concurrency model at all — see Open Questions.

---

## Proposed Improvement Plan

### Quick Wins (low risk, do these regardless of anything else)

- **Add `PRAGMA optimize;` immediately before closing every connection in `connection.py`.** This
  is the official recommendation for exactly SALTMDB's "short-lived connection" shape and is
  documented as a fast no-op in the common case.
- **Verify (and if needed, fix) that write transactions use `BEGIN IMMEDIATE`**, not Python's
  default deferred `BEGIN`, in whatever layer issues transactions on top of `get_connection()`.
  This is the single highest-leverage *correctness* fix identified in this research — it's the
  difference between `busy_timeout` actually working for write-write contention versus being
  silently bypassed on the exact lock-upgrade path it's meant to cover.
- **Add a bounded retry/backoff wrapper** around the small number of call sites that execute
  writes, catching `sqlite3.OperationalError` matching "database is locked" and retrying with
  jittered backoff up to a small fixed budget (e.g., 2-3 retries) *in addition to* — not instead of
  — the existing `busy_timeout`. This directly targets the no-FIFO-fairness failure mode SkyPilot
  documented, where a single busy_timeout window can still lose the race to a newer arrival.
- **Explicitly set and comment `wal_autocheckpoint`** in `connection.py` even if keeping the
  SQLite default (1000 pages) — right now it's implicit/undocumented, and it's the primary lever
  for the checkpoint-starvation risk described above.
- **Log a WAL page count on connection close** (e.g., `PRAGMA wal_checkpoint;` return values
  include pages-in-wal and pages-checkpointed) at a debug/trace level, cheaply turning the
  currently-theoretical "is WAL growing unbounded?" question into an observable metric.

### Medium-Term (moderate effort, needs a small design decision)

- **Give the Librarian a checkpoint-maintenance duty.** Since `locks.py` already guarantees at
  most one process holds the Librarian leader lock at a time, that's a natural, already-existing
  place to periodically issue `PRAGMA wal_checkpoint(TRUNCATE)` (or the less-aggressive `RESTART`)
  without introducing any new coordination primitive. This directly answers the "who runs
  checkpoint maintenance in a per-request-connection design with no long-lived writer" question —
  the Librarian *is* the closest thing SALTMDB has to a long-lived process, so it should own this.
- **Give the Librarian the "long-lived connection" `PRAGMA optimize` duty too**: run
  `PRAGMA optimize=0x10002` once at the start of its run, and plain `PRAGMA optimize` on subsequent
  periodic passes — matching official guidance for the one process in the system that isn't
  per-request-connection-shaped.
- **Instrument before re-tuning `wal_autocheckpoint` further.** Only lower the threshold (trading
  more frequent, cheaper checkpoints for tighter WAL size bounds) once the page-count logging above
  shows it's actually needed under real multi-agent load — don't tune blind.
- **Confirm the Windows/AV/cloud-sync environment question** (see Open Questions) and, if
  `~/.saltmdb` risks landing under OneDrive/AV-scanned paths on some installs, add a startup check
  that warns (or better, relocates/documents an override) — this is cheap insurance against an
  entire class of spurious lock errors that have nothing to do with SQLite tuning at all.

### Larger Bets — and why most are NOT justified here

- **A dedicated single-writer thread/process + queue (rqlite-style) is over-engineering at this
  scale.** rqlite's writer-queue exists to amortize network + Raft-consensus round-trips across a
  distributed cluster; Expensify's Bedrock exists to serve millions of QPS across networked
  clients via a custom replicated engine. SALTMDB's actual write volume — a handful of independent
  local coding-agent processes issuing occasional MCP tool calls — is nowhere near the point where
  SQLite's built-in single-writer file lock becomes the bottleneck. The OS file lock *is already*
  the queue; WAL + `busy_timeout` + (fixed) `BEGIN IMMEDIATE` is sufficient for this scale. Building
  a Python-level `queue.Queue` + dedicated writer thread would only help intra-process contention
  (multiple concurrent async handlers inside *one* MCP server), and even that should only be
  pursued if profiling ever shows a single server process genuinely issuing overlapping writes
  internally — not as a blanket fix for the cross-process scenario this research was scoped to.
- **Turso/libSQL's MVCC (`BEGIN CONCURRENT`) is real, interesting technology — but it means
  adopting a different storage engine entirely**, not a config change to stock `sqlite3`. Given
  SALTMDB's contention profile doesn't require it, this is not recommended now; worth revisiting
  only if the Medium-Term instrumentation someday shows sustained write-write contention that
  `BEGIN IMMEDIATE` + retry/backoff can't absorb — which, at "a handful of local CLI processes,"
  is not expected to happen.
- **Connection pooling (a small persistent pool instead of per-request `sqlite3.connect()`)** is
  the one "larger bet" that's arguably worth it, but it cuts against the grain of Python's
  `sqlite3` thread-affinity model (a connection is normally only safe to use from the thread that
  created it) and would be a real architectural change to how `get_connection()` is used
  throughout the codebase. Given the hynek.me finding is about *coordination overhead*, not raw
  connect() cost, the cheaper fix (Quick Wins above, especially fixing deferred-vs-immediate
  transactions and adding retry/backoff) should be tried and measured first — only escalate to a
  pooling redesign if instrumentation shows the per-connection WAL coordination overhead is
  actually a measurable, recurring source of `SQLITE_BUSY` after the cheaper fixes land.

---

## Risks & Trade-offs

- **Longer timeouts trade failure-rate for latency, not for fairness.** Raising `busy_timeout`
  further would reduce observed `SQLITE_BUSY` errors but increase worst-case per-call stall time
  proportionally — a crashed or slow holder of a lock now blocks siblings for the full window
  before anyone notices, per SkyPilot's own conclusion that this is a probability/latency
  trade-off, not a fix.
- **`TRUNCATE`/`RESTART` checkpoints can block readers while they run.** Scheduling these from the
  Librarian must be done carefully (ideally only while the Librarian itself holds the leader lock
  and activity is otherwise low) so that checkpoint maintenance doesn't itself become a new source
  of agent-visible stalls.
- **`PRAGMA optimize` is "usually" free but not provably free at SALTMDB's actual call volume.**
  It should be measured, not assumed zero-cost, especially if per-request connections are opened
  at high frequency during a busy multi-agent session.
- **Adding an application-level retry/backoff loop on top of `busy_timeout` adds a second timeout
  knob and interaction to reason about** — worst-case latency becomes roughly
  `busy_timeout × (retries + 1)`, so the retry budget must be small and explicitly bounded, or the
  20s ceiling silently becomes an effective 60s+ ceiling.
- **The Windows Defender/OneDrive/AV risk is only partially fixable in SALTMDB's own code.** It
  depends on end-user machine configuration outside the project's control; the realistic mitigation
  is documentation/detection, not a pure code fix.
- **None of the "Larger Bets" architectural changes (writer queue, MVCC engine) are recommended
  now** — pursuing them anyway would add real complexity and a new dependency surface to solve a
  contention problem that, at this process count, likely doesn't exist yet in practice.

---

## Open Questions for zbalint

1. Does the current write path use `BEGIN IMMEDIATE` (or equivalent), or Python `sqlite3`'s default
   deferred `BEGIN`? This single fact determines whether the just-raised 20s `busy_timeout` is
   actually effective for write-write contention, per the berthub.eu deferred-upgrade caveat —
   worth checking directly in the write-path code (outside this web-research task's scope).
2. Is `~/.saltmdb` ever located under a OneDrive-synced or otherwise cloud-synced folder on any
   target Windows machine (common via OneDrive Known Folder redirection on managed/corporate
   Windows installs)? That's a concrete, documented source of file-locking flakiness unrelated to
   SQLite tuning, worth a one-time check or a startup warning.
3. Has a real `SQLITE_BUSY`/"database is locked" error actually been observed reaching an agent or
   user *since* the 5s→20s bump, or was that change preemptive? If it's still occurring even at
   20s, per SkyPilot's findings the more likely root cause is lock-acquisition unfairness or the
   deferred-transaction-upgrade bug, not "needs an even bigger number."
4. What is the actual steady-state WAL file size / page count today? This document's checkpoint-
   starvation concern is currently theoretical (grounded in official docs, not observed telemetry)
   — a one-line log from the Librarian reporting `PRAGMA wal_checkpoint` output before more
   investment goes into checkpoint tuning would settle it empirically.
5. Is there a target ceiling for how many concurrent agent processes/MCP server subprocesses
   SALTMDB is expected to support at once (e.g., "at most ~5")? That number is the actual
   determinant of whether any "Larger Bets" item should ever be revisited, versus staying firmly
   in "quick wins are enough" territory.

---

## Sources

- [Write-Ahead Logging (SQLite official docs)](https://www.sqlite.org/wal.html) — checkpoint types, autocheckpoint default, and the documented checkpoint-starvation-under-overlapping-readers failure mode.
- [File Locking And Concurrency In SQLite Version 3 (SQLite official docs)](https://sqlite.org/lockingv3.html) — the five-state OS file lock (via Windows `LockFile()`) is what actually serializes cross-process writes.
- [PRAGMA optimize (SQLite official docs)](https://www.sqlite.org/pragma.html#pragma_optimize) — official recommendation to run `PRAGMA optimize` before closing short-lived connections.
- [SQLite Over a Network, Caveats and Considerations (SQLite official docs)](https://sqlite.org/useovernet.html) — WAL requires shared memory between processes and does not work over network filesystems.
- [Bert Hubert — What to do about SQLITE_BUSY errors despite setting a timeout](https://berthub.eu/articles/posts/a-brief-post-on-sqlite3-database-locked-despite-timeout/) — `busy_timeout` is not honored on a deferred-transaction read-to-write lock upgrade; use `BEGIN IMMEDIATE`.
- [SkyPilot — Abusing SQLite to Handle Concurrency](https://skypilot.ai/blog/abusing-sqlite-to-handle-concurrency/) — production war story showing SQLite's busy-retry backoff has no FIFO fairness, producing long-tailed latency under many concurrent writers.
- [Hynek Schlawack — TIL: SQLite WAL Mode Can Lock Short-Lived Readers](https://hynek.me/til/sqlite-read-only-wal-locked/) — WAL's own connection open/close coordination can lock out short-lived per-operation connections, directly matching SALTMDB's connection pattern.
- [phiresky — SQLite performance tuning](https://phiresky.github.io/blog/2020/sqlite-performance-tuning/) — widely-cited concrete PRAGMA recipe that SALTMDB's current settings substantially already match.
- [Fly.io — I'm All-In on Server-Side SQLite](https://fly.io/blog/all-in-on-sqlite-litestream/) — Litestream/LiteFS make WAL-mode SQLite production-viable mainly by solving backup/DR, not write concurrency.
- [Expensify — Scaling SQLite to 4M QPS on a Single Server](https://use.expensify.com/blog/scaling-sqlite-to-4m-qps-on-a-single-server) — Bedrock's hyperscale numbers come from a custom replicated networking layer, not applicable as-is to a local dev tool.
- [rqlite — Design docs](https://rqlite.io/docs/design/) and [Queued Writes](https://rqlite.io/docs/api/queued-writes/) — the "single writer + queue" pattern in its purest form, but built to amortize network/Raft round-trips, not local file-lock contention.
- [Turso — Beyond the Single-Writer Limitation with Turso's Concurrent Writes](https://turso.tech/blog/beyond-the-single-writer-limitation-with-tursos-concurrent-writes) — MVCC/`BEGIN CONCURRENT` is a real fix to SQLite's single-writer model but requires a different storage engine entirely.
- [Anton Zhiyanov — SQLite is not a toy database](https://antonz.org/sqlite-is-not-a-toy-database/) — WAL's "many readers, one writer" model is sufficient for the vast majority of real applications; reaching for a client-server DB for concurrency alone is often premature.
- [oven-sh/bun issue #25964 — SQLite database file locked on Windows after close() with WAL mode](https://github.com/oven-sh/bun/issues/25964) — current, concrete evidence of Windows-specific WAL lock lifetime quirks outliving `close()`.
- [SQLite Forum — Can -wal -shm files be deleted if -wal file is empty?](https://sqlite.org/forum/forumpost/2a8c51e0b8?t=h) — operational guidance that `-wal`/`-shm` files are only safe to delete when no process holds the DB open, relevant to crash-recovery runbooks.
