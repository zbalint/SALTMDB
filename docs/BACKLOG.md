# SALTMDB backlog

A running list of ideas, bugs, features, experiments and chores that are not being worked on right
now, or are queued behind something that is. Keep entries short; the detail lives in the spec,
issue or commit the entry links to.

**Rules**

- Never paste SALTMDB memory content (titles, excerpts) here; this file is committed.
- Give every entry the next free `BL-nnn` id. Never reuse or renumber an id.
- Change an entry's **Status** in place; when it ships or is dropped, move it to **Closed** with the
  commit or reason.
- An entry that becomes real work gets a spec in `docs/specs/` (start from `docs/specs/TEMPLATE.md`); link it in **Notes / links**.
- This is a tracker, not a design: a decision belongs in `docs/architecture.md`. Entries can lag the code, so check an entry against the code before acting on it.
- In **Notes / links**, say how the state was learned when it matters: `code`, `docs`, `manual`, or `reported` (the owner said so and it was not re-run).

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
| BL-010 | idea | Unknown-argument error could suggest the closest valid parameter name | open | Was left out of scope in the reject-unknown-arguments change. A `did you mean entity_id?` hint would cover the `memory_id` mistake on every tool without aliases. Compare with BL-001 before building either. |
| BL-012 | bug | First agent session fails to connect its MCP server on a cold start | needs-owner | `ensure_daemon_running` waits about 10 s (`config.py` `DAEMON_DISCOVERY_RETRY_ATTEMPTS` x `DAEMON_DISCOVERY_RETRY_DELAY_S`) and `server_lifespan` has no retry, but `daemon.log` shows daemon startup after a reboot takes 20 to 86 s (warm: 1 to 3 s). Evidence: code and `daemon.log`, not yet reproduced on demand. Owner assigned it to the architect and consultant. Implemented in 8180ade (spec `docs/specs/SPEC-DAEMON-COLD-START.md`, final amendment 2662e71), `./verify` green; waiting on the owner's manual cold-start check (spec section 8 item 6), then move to Closed. Look at `startup_lag_s` in `daemon.log` after the next cold spawn. |
| BL-013 | feature | Start the daemon by hand in the background, or as a system service (systemd on Linux) | needs-owner | `python -m saltmdb.daemon.server --foreground` already runs without the idle-shutdown timer (tests in `tests/test_daemon_server.py`). Missing: a detached persistent start, stop and status for it, a systemd unit and install docs. Reduces BL-012 for users who run it, but does not replace the BL-012 fix. Owner decided (2026-10-05): scope is the full installer (a start/stop/status command that runs a detached persistent daemon, plus an install-service command that writes and enables the unit, plus docs); platform is Linux/WSL2 systemd only. Implemented in 9e9af0d (spec `docs/specs/SPEC-DAEMON-BACKGROUND-SERVICE.md`, 36dafa9), `./verify` green; waiting on the owner's manual `systemctl --user` check (spec section 8 item 7), then move to Closed. First step toward BL-015. |
| BL-014 | bug | Daemon exits on `Failed to bind probe port ... Address already in use` after a quick restart, leaving no daemon for minutes | open | `~/.saltmdb/daemon.log` has 25 such errors; e.g. ten spawns in a row won the election guard then exited on the probe bind. Root cause not checked: candidates are no `SO_REUSEADDR` on the probe socket (`daemon/server.py` L879-883) or a lingering process holding the port. Found by the consultant during BL-012; separate from the cold-start wait, and it breaks hooks and CLI calls too. Evidence: code and `daemon.log`. |
| BL-015 | idea | Run the daemon on a remote host and let agent adapters connect to it | open | An adapter cannot spawn a daemon on another host, so BL-013 (a daemon started and kept alive outside the adapter) is the first step. Today everything is loopback and single-machine (`daemon/server.py` binds `127.0.0.1`; `daemon/client.py` connects to `127.0.0.1`; discovery is a local file). Open questions: where the daemon runs and how clients find it, authentication and transport, `stop`/`status` for a non-local daemon, and a rule that an unreachable configured remote is an error, never a silent local fallback (BL-013 D9 keeps the adapter spawning a local daemon). Needs owner input on the target setup before any spec. Evidence: code. |

## Closed

| Id | Type | Title | Closed by |
|---|---|---|---|
| BL-000 | bug | MCP tools silently ignored unknown arguments | e51eb32 (all tools now reject an unknown argument and name it) |
| BL-002 | feature | Per-candidate ranking diagnostics for `search_memory` (Phase A) | 93520a5 (spec `docs/specs/SPEC-SEARCH-RANKING-DIAGNOSTICS.md`, 34ca32d); `return_diagnostics` is now on the MCP tool |
| BL-001 | feature | `get_memory` accepts `memory_id` as an alias for `entity_id` | 88a87a0 (spec `docs/specs/SPEC-GET-MEMORY-MEMORY-ID-ALIAS.md`, 6cd8398); `get_memory` only, other tools unchanged |
| BL-009 | chore | Merge `develop` to `master` and push so `./verify` is green there | 3dcd37d (owner authorized; `master` fast-forwarded and pushed, `./verify` green) |
| BL-008 | chore | Confirm Dependabot alerts cleared after the lockfile bump | 693c0bf (owner confirmed 2026-10-03 the Dependabot alert list is empty) |
| BL-011 | bug | Bootstrap digest and handover go empty after 10 content-free sessions in one directory | c13c53d (spec `docs/specs/SPEC-BOOTSTRAP-EMPTY-SESSION-WINDOW.md`, 9a35d56); sessions are filtered by content in SQL and `entities(last_touched_session_id)` has a partial index. A running MCP server needs a restart from a checkout at or after c13c53d |
