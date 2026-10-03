# SALTMDB backlog

A running list of ideas, bugs, features, experiments and chores that are not being worked on right
now, or are queued behind something that is. Keep entries short; the detail lives in the spec,
issue or commit the entry links to.

**Rules**

- Never paste SALTMDB memory content (titles, excerpts) here; this file is committed.
- Give every entry the next free `BL-nnn` id. Never reuse or renumber an id.
- Change an entry's **Status** in place; when it ships or is dropped, move it to **Closed** with the
  commit or reason.
- An entry that becomes real work gets a spec in `docs/SPEC-*.md`; link it in **Links**.

**Types:** `feature`, `bug`, `chore`, `idea` (not yet decided), `experiment` (benchmark-first).

**Status:** `queued` (decided, waiting on something), `in-progress`, `open` (not started, not
decided), `blocked`, `needs-owner` (waiting on a decision or authorization).

## Open

| Id | Type | Title | Status | Notes / links |
|---|---|---|---|---|
| BL-003 | feature | Frozen DB snapshot plus replay harness for retrieval failures (Phase B) | open | Needed to replay a ranking miss fairly: memories written after the miss can quote the query and contaminate the corpus. Depends on BL-002. Needs its own spec. |
| BL-004 | experiment | Overlap/confuser benchmark subset and failure taxonomy | open | Queries where the gold memory and at least one competitor are lexically or semantically close; classify each miss by stage (FTS, vector, RRF, cross-encoder, lifecycle). Do not change ranking before this exists. Depends on BL-002, BL-003. |
| BL-005 | experiment | Lineage-head tie-break (Phase C) | open | Benchmark-only. Baseline arm must include the existing `collapse_supersedes_families` option. Run with and without the cross-encoder, which can become the final order. Reject if history or conflict queries regress. Depends on BL-004. |
| BL-006 | idea | Evidence-origin metadata on memories (Phase E) | needs-owner | Would reverse the recorded decision in `docs/architecture.md` (conversation-trace section) that prompt origin is handled in agent instructions, not in SALTMDB code. Needs an explicit decision to reopen; capture origin at write time, never infer it from trace text. |
| BL-007 | bug | Stop hooks fire on investigation-only turns; digest labels ended sessions "running" | open | Retrieval-outcome and self-critique gates add little when a turn only investigates. Hooks are managed from `~/.agents`; any change goes through that repo first. Not triaged. |
| BL-008 | chore | Confirm Dependabot alerts cleared after the lockfile bump | open | Lockfile bumped in 693c0bf and pushed to `develop` and `master`. Alerts were still listed right after the push (likely a stale rescan). One medium `PyJWT` alert had no patched version at the time. Check https://github.com/zbalint/SALTMDB/security/dependabot. |
| BL-009 | chore | Merge `develop` to `master` and push so `./verify` is green there | needs-owner | `master` fails `./verify` twice: `ruff format --check` on `mcp/tools.py` (fixed in 93520a5) and bandit B101 in `trace_service.py` (fixed in 345296e). Both fixes are on `develop` only. Needs the owner's explicit authorization for `master`. |
| BL-010 | idea | Unknown-argument error could suggest the closest valid parameter name | open | Was left out of scope in the reject-unknown-arguments change. A `did you mean entity_id?` hint would cover the `memory_id` mistake on every tool without aliases. Compare with BL-001 before building either. |

## Closed

| Id | Type | Title | Closed by |
|---|---|---|---|
| BL-000 | bug | MCP tools silently ignored unknown arguments | e51eb32 (all tools now reject an unknown argument and name it) |
| BL-002 | feature | Per-candidate ranking diagnostics for `search_memory` (Phase A) | 93520a5 (spec `docs/SPEC-SEARCH-RANKING-DIAGNOSTICS.md`, 34ca32d); `return_diagnostics` is now on the MCP tool |
| BL-001 | feature | `get_memory` accepts `memory_id` as an alias for `entity_id` | 88a87a0 (spec `docs/SPEC-GET-MEMORY-MEMORY-ID-ALIAS.md`, 6cd8398); `get_memory` only, other tools unchanged |
