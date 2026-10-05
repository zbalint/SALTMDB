# SPEC — Rename `owner_id` to `agent_id`

## 0. Status

**LOCKED** — 2026-09-29, against `develop` @ `0b2de50`. Written by an agent; decisions by the owner.

**Scope (files you may edit or create).** Every tracked file that the command in §13 (Acceptance **A**)
lists on the *unmodified* tree — 138 files on 2026-09-29: 37 under `src/saltmdb/`, 86 under `tests/`
(including `tests/fixtures/development_fusion_synthetic.py`), 6 under `hooks/` (four scripts,
`hooks/README.md`, `hooks/claude-settings-example.json`), 3 under `scripts/benchmarking/`, 2 skill files
(`skills/*/SKILL.md`), 3 root docs (`INSTALL.md`, `MIGRATION.md`, `SECURITY.md`) and `docs/architecture.md` —
plus one file the
scan cannot list because it does not exist yet: the new `tests/test_agent_id_rename.py` (§10). The new prose
required in `docs/architecture.md`, `SECURITY.md` (§9) and `MIGRATION.md` (§11) lands in files already in scope.

**Does not touch:** `docs/specs/SPEC-*.md` (including this file), `docs/specs/beta-readiness-tdd-plan.md`,
`docs/specs/conversation-trace-provenance-plan.md`, the other files in `docs/specs/` (historical records; they keep the old name),
the existing rows of `MIGRATION.md`, `pyproject.toml`, `uv.lock`, `src/saltmdb/config.py::__version__`
(no version bump — that is a release decision made at commit time, and `tests/test_version_consistency.py`
ties `uv.lock` to it), anything outside this repository (`~/.agents`, harness MCP configs, live databases).

## 1. Why

`owner_id` names the identity of the *agent* that wrote a record, and it is used for attribution and for
the `scope='private'` visibility filter. The events ledger already calls the same concept `agent_id`
(`events.agent_id`, `get_events(agent_id=...)`, and every tag/predicate writer receives the owner's value
under an `agent_id=` keyword — e.g. `write.py:518`, `lifecycle.py:266`, `relation_service.py:476`), and
sessions are already `agent_session_id`. Two names for one thing is the smell being removed.

This rename is the prerequisite for the planned *remote mode* work: a network daemon will authenticate
per-agent and must talk about an `agent_id` from day one.

**Decisions locked by the owner (2026-09-29):**

1. Rename the database columns, with a migration (not an API-only rename).
2. Env var becomes `SALTMDB_AGENT_ID`; `SALTMDB_OWNER_ID` remains accepted as a deprecated fallback.
3. Responses emit `agent_id` only (no dual output); every in-repo consumer is updated in the same change.

**Semantics (unchanged, restated so nobody "fixes" them):** `agent_id` is attribution plus a visibility
filter. `scope='private'` is a *visibility convenience* — an agent marks a note as only useful to itself
or as scratch/test data; `shared` stays the default. It is not access control. No behaviour changes here.

## 2. Rename map

Apply to identifiers, dict/JSON keys, SQL column names, keyword arguments, HTTP query parameters, and
strings. Preserve behaviour exactly.

| Old | New |
|---|---|
| `owner_id` (any context: param, kwarg, key, column, attribute) | `agent_id` |
| `OWNER_ID`, `_OWNER_ID_RE`, `SENTINEL_OWNER_ID` | `AGENT_ID`, `_AGENT_ID_RE`, `SENTINEL_AGENT_ID` |
| `SALTMDB_OWNER_ID` (env var) | `SALTMDB_AGENT_ID` (old name: fallback only, §3) |
| `get_owner_id`, `validate_owner_id` | `get_agent_id`, `validate_agent_id` |
| `SESSION_IDENTITY.configure_owner` / `.owner_id` / `._owner_id` | `.configure_agent_id` / `.agent_id` / `._agent_id` |
| `_effective_owner` | `_effective_agent_id` |
| `owner_id_` (local in `mcp/tools.py`) | `agent_id_` |
| `owner_val` | `agent_val` |
| `item_owner_id`, `owner_id_filter` | `item_agent_id`, `agent_id_filter` |
| `inherited_owner`, `existing_owner` | `inherited_agent_id`, `existing_agent_id` |
| `eowner` | `eagent_id` |
| `_OWNER_INJECTED_TOOLS`, `_strip_item_owner_id` | `_AGENT_ID_INJECTED_TOOLS`, `_strip_item_agent_id` |
| `idx_entities_owner_scope`, `idx_traces_owner_status` | `idx_entities_agent_scope`, `idx_traces_agent_status` |
| viewer label `'Owner'`, placeholder `'Owner id (partial match)'`, `ownerField` | `'Agent'`, `'Agent id (partial match)'`, `agentField` |
| `<session ... owner="...">` attribute (`session_digest_service.py:115`) | `agent_id="..."` |
| `owner={item['owner_id']}` inventory text (`core_governance_service.py:494`) | `agent_id={item['agent_id']}` |

**Do NOT rename:**

- The daemon *election* owner: `legitimate_owner` (`daemon/server.py:862-863`) and the prose at
  `daemon/server.py:724` and `:746`. That "owner" is the process that owns a DB path, not an agent.
- `ownerDocument` and any other DOM/browser API name.
- English prose "owner" / "ownership" / "owner-scoped" / "owner mismatch" in comments, docstrings and
  the existing error text `Memory '...' owner mismatch.` (`lifecycle.py:1334`). Only the identifiers
  and strings listed above change. When unsure whether an "owner" means the agent identity, leave it.
- `agent_session_id` and the meaning of `scope`.

**Name collisions.** `agent_id` already exists as a symbol in `event_service.py`,
`memory_service/tags.py::resolve_or_create_tag`, `relation_service.py::resolve_or_create_predicate`,
`mcp/tools.py` (`log_event` wrapper, `get_events` parameter) and `daemon/dispatch.py::_dispatch_get_events`.
Existing call sites such as `resolve_or_create_tag(conn, name, agent_id=owner_id)` simply become
`agent_id=agent_id`. If any rename would create two parameters or two locals with the same name in one
scope, **stop and report BLOCKED** — do not invent a name.

## 3. Identity configuration

**`src/saltmdb/config.py`** (`_OWNER_ID_RE` at line 6, `validate_owner_id` at 9, `get_owner_id` at 29):

- `validate_agent_id(agent_id)` — same regex `^[a-z][a-z0-9_-]{0,63}$`. Error text becomes:
  `SALTMDB_AGENT_ID is required and must match ^[a-z][a-z0-9_-]{0,63}$. Configure it in the MCP server environment before starting SALTMDB.`
- `get_agent_id()` resolves the value as follows (module-level `logger = logging.getLogger(__name__)`,
  add the import if the module lacks it):
  1. `SALTMDB_AGENT_ID` set and non-blank → validate and return it. If `SALTMDB_OWNER_ID` is *also* set
     and differs, log one warning naming both variables and stating that `SALTMDB_AGENT_ID` wins.
  2. Else `SALTMDB_OWNER_ID` set and non-blank → validate and return it, after logging one warning that
     `SALTMDB_OWNER_ID` is deprecated and `SALTMDB_AGENT_ID` should be used.
  3. Else raise the error above.
- Add a `# shortcut:` comment on the fallback: `# shortcut: SALTMDB_OWNER_ID fallback kept so existing harness configs keep working; remove one release after every config is migrated`.

**`mcp/identity.py`:** rename per §2 (`configure_agent_id`, `agent_id` property, `_agent_id`,
`IdentityRebindRejected` messages naming `SALTMDB_AGENT_ID`), update module/class docstrings' env var name.
**`mcp/server.py`, `__main__.py`, `cli.py`:** call the renamed functions. `cli.py:79` and `:127` pass
`agent_id=get_agent_id()`.

## 4. Adapter envelope — `src/saltmdb/mcp/tools.py` (the one place that is not a pure rename)

Today `RpcBackend.call` (around lines 217-227) re-asserts the identity for tools in `_OWNER_INJECTED_TOOLS`
and **strips** any `owner_id` key from every other tool's kwargs. After the rename the identity key is
`agent_id`, which is *also*:

- the public read filter of `get_events` (`tools.py:1644`, forwarded as `"agent_id": agent_id` at `:1682`), and
- the key the `log_event` wrapper already sets to the adapter identity (`tools.py:342`).

A blind rename would silently strip both. Required behaviour instead:

```python
_AGENT_ID_INJECTED_TOOLS = frozenset({...old set..., "log_event"})
_AGENT_ID_PASSTHROUGH_TOOLS = frozenset({"get_events"})

if tool_name in _AGENT_ID_INJECTED_TOOLS:
    kwargs = {**kwargs, "agent_id": _effective_agent_id()}
elif tool_name in _AGENT_ID_PASSTHROUGH_TOOLS:
    pass  # get_events' agent_id is a caller-chosen read filter, never an identity claim
else:
    kwargs = {k: v for k, v in kwargs.items() if k != "agent_id"}
```

- `log_event` joins the injected set because `event_service.log_event` now genuinely takes `agent_id`.
  Replace the old `NOTE: log_event is deliberately absent...` comment (it described the 2026-08-26 outage
  caused by injecting a key the service did not accept) with a comment stating that `event_service.log_event`
  accepts `agent_id`, so injection is safe and re-asserts the adapter identity over any wrapper mistake.
  Keep the `log_event` wrapper's own `"agent_id": agent_id_` binding as it is.
- `_strip_item_agent_id` strips a stray `agent_id` from each bulk item exactly as `_strip_item_owner_id`
  strips `owner_id` today (`manage_relation.relations`, `consolidate_memories.consolidations`).
- The user-facing error at `tools.py:160` becomes
  `SALTMDB agent identity is not configured; set SALTMDB_AGENT_ID in the MCP server environment and restart the MCP server.`
- Update every docstring in the file that says `owner_id`/`SALTMDB_OWNER_ID` (including the `effective`
  block description, `utils/envelope.py:14`).

## 5. Daemon — `dispatch.py`, `server.py`, `client.py`

**`daemon/dispatch.py`:** every `kw.get("owner_id")` becomes `kw.get("agent_id")`; `_required_str(kw, "owner_id")`
(line 513) becomes `_required_str(kw, "agent_id")`; every keyword passed to a service is `agent_id=`.
Telemetry block in `dispatch_tool` (lines 753-763): the identity recorded for a call is
`kwargs.get("agent_id")` **except for `get_events`, where `agent_id` is a filter, not the caller — record
`None` there.** `telemetry_service.record_call(owner_id=...)` becomes `agent_id=`.

**`daemon/client.py`:** `SessionConnection.__init__(..., owner_id=...)` becomes `agent_id=`, attribute
`_agent_id`, `hello_params["agent_id"]` (lines 496, 534-535). `mcp/server.py:35` passes `agent_id=`.

**`daemon/server.py`:** `params.get("owner_id")` at line 391 becomes `params.get("agent_id")` (passed to
`agent_sessions.record_session`). Add a **fail-closed guard for stale adapters** — a long-running agent
session started before the upgrade still runs the old adapter and can reach a new daemon; without a guard
its writes would silently lose their identity:

1. In `_handle_session_method`, at the top of the `hello` branch (before the lock and before any session
   is registered): if `"owner_id" in params`, return
   `protocol.build_error_response(request_id, protocol.MALFORMED_REQUEST, "hello parameter 'owner_id' was renamed 'agent_id'; restart the MCP adapter on the current SALTMDB version")`.
2. In `_handle_tool_call`, right after the `kwargs` type check: if `"owner_id" in kwargs`, return
   `protocol.build_error_response(request_id, protocol.MALFORMED_REQUEST, "tool kwarg 'owner_id' was renamed 'agent_id'; restart the MCP adapter on the current SALTMDB version")`.

Item-level `owner_id` keys inside bulk lists are not guarded: the services read only `agent_id` there, so a
stale key is ignored, never honoured. These two guards and the two messages are the only legacy-name text
allowed in `server.py`.

## 6. Database — `src/saltmdb/db/schema.py`, `db/agent_sessions.py`, `telemetry_service.py`

**Verified facts (probe against a DB created by the unmodified code, SQLite 3.53.1 in the project venv):**
exactly four tables have an `owner_id` column — `entities`, `conversation_traces`, `tool_call_telemetry`,
`_agent_sessions` — and exactly three indexes mention it: `idx_entities_owner_scope`,
`idx_entities_content_hash`, `idx_traces_owner_status`. `ALTER TABLE ... RENAME COLUMN owner_id TO agent_id`
on all four rewrote the index definitions automatically, left no object mentioning `owner_id`, kept the FTS
triggers working on a following `UPDATE`, and `PRAGMA integrity_check` returned `ok`. Index *names* are not
changed by the rename. `RENAME COLUMN` needs SQLite ≥ 3.25.

**New function in `schema.py`:**

```python
_AGENT_ID_TABLES = ("entities", "_agent_sessions", "conversation_traces", "tool_call_telemetry")

def _migrate_owner_id_to_agent_id(conn) -> None:
    for table in _AGENT_ID_TABLES:          # constants, never user input
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "owner_id" not in columns:
            continue                          # fresh DB, or table without the column
        if "agent_id" in columns:
            raise RuntimeError(f"{table} has both owner_id and agent_id; refusing to guess which to keep")
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN owner_id TO agent_id")
    conn.execute("DROP INDEX IF EXISTS idx_entities_owner_scope")
    conn.execute("DROP INDEX IF EXISTS idx_traces_owner_status")
```

- **Placement:** the very first statement inside `init_db`'s `_write` closure (currently `# 1. Events Table`
  at line 384), before any `CREATE TABLE`, `ALTER TABLE ... ADD COLUMN`, or `CREATE INDEX`. On an old DB every
  later statement that mentions `agent_id` would otherwise fail with `no such column`.
- Idempotent and gated on column existence, **not** on `PRAGMA user_version`. `user_version` stays at 3.
  `_write` is retried from scratch on `database is locked`; the function is safe to re-run.
- The old-named indexes are dropped because `CREATE INDEX IF NOT EXISTS idx_entities_agent_scope ...` is a
  different name and would otherwise leave a duplicate index on the same columns. `idx_entities_content_hash`
  keeps its name; its definition was already rewritten by the rename.
- Rename `owner_id` to `agent_id` in every `CREATE TABLE`, index statement (lines 1118, 1127 and the traces
  index at 642), the `_agent_sessions` legacy-rebuild column tuple and `INSERT ... SELECT` (lines 161-210),
  and the `ALTER TABLE _agent_sessions ADD COLUMN` loop at line 971 (`"agent_id TEXT"`). The legacy rebuild
  reads column names from the *old* table; because the migration above runs first, that table already says
  `agent_id`.
- `db/agent_sessions.py` (line 30 upsert `agent_id = COALESCE(_agent_sessions.agent_id, excluded.agent_id)`,
  the `record_session` parameter, the dict key at line 149) and `domain/services/telemetry_service.py`
  (`record_call(agent_id=...)`, its INSERT) follow.
- **Stored JSON payloads in existing rows (`events.content`, `entities.metadata`, `tool_call_telemetry.param_names`)
  are not rewritten.** Old rows may still contain an `owner_id` key inside JSON. Before finishing, grep for any
  code that reads a key named `owner_id` out of a *stored* JSON payload; the survey in preparing this spec
  found none (all hits read a column or a service-built dict). If you find one, stop and report BLOCKED.

## 7. Services, viewer, snapshot — mechanical rename

Apply §2 to every remaining file that Acceptance **A** lists under `src/saltmdb/` (domain services, `utils/`,
`viewer/`). Behavioural points that are easy to get wrong:

- **Response keys:** memory records, session and trace records, orphans, duplicate candidates, `effective`
  blocks, viewer payloads, and corpus-snapshot provenance all emit `agent_id` and no `owner_id`.
- **Visibility filter** stays exactly `(agent_id = ? OR scope = 'shared')` in every place it appears today
  (`orchestrator.py:206`, `relation_service.py:729/731/1039/1041`, `write.py:202`, `duplicates.py:64-65,236`,
  `corpus_snapshot_service.py:159`, `utils/text.py:86,129,138`, and the community services).
- **Viewer HTTP:** query parameter `owner_id` on `/entities` and the sessions route becomes `agent_id`;
  `viewer.js` reads `data.agent_id`, sends `agent_id`, and shows the label `Agent`. Do not touch vendored
  or DOM code (`ownerDocument`).
- **Session-digest XML** attribute `owner=` becomes `agent_id=`; **core inventory** text `owner=` becomes `agent_id=`.
- **Corpus snapshot** (`corpus_snapshot_service.py`): `_ENTITY_COLUMNS` and provenance keys use `agent_id`. Snapshot
  hashes therefore change and snapshot files exported before this change are not readable by the updated reader.
  That is accepted; do not add a compatibility reader.
- `scripts/benchmarking/build_diverse_test_db.py`, `benchmark_rerank_thresholds.py`: rename identifiers and raw-SQL
  column names. `scripts/benchmarking/build_lexical_snapshot_db.py`: rename everything, but read the provenance
  as `provenance.get("agent_id") or provenance.get("owner_id")` with a one-line comment that historical frozen
  exports still carry the old key (this is one of the seven files allowed to keep the old name, §13).

## 8. Hooks, skills, install docs

- `hooks/saltmdb-checkable-fact-drift-sweep.py`, `saltmdb-pre-compact-sweep.py`, `saltmdb-session-end-wrapup-reminder.py`,
  `saltmdb-skill-review-sweep.py`: prompt strings say `agent_id=...` instead of `owner_id=...`. Do not reword
  anything else in them.
- `hooks/README.md`, `hooks/claude-settings-example.json`, `skills/saltmdb-usage/SKILL.md` (line 122),
  `skills/saltmdb-skill-review/SKILL.md` (line 50): `SALTMDB_AGENT_ID` / `agent_id`.
- `INSTALL.md`: all examples use `SALTMDB_AGENT_ID`; add a short "Migrating from `SALTMDB_OWNER_ID`" paragraph
  stating the old name still works with a logged deprecation warning and describing the precedence rule of §3.
  This paragraph is why `INSTALL.md` is allowed to keep the old name (§13).

## 9. Docs that describe the model

- `docs/architecture.md` (lines 56-58, 134, 142, 154, 183, 235 and any other hit): `agent_id` throughout.
  In the `entities` description add one sentence: `scope='private'` is a visibility convenience for an agent's
  own scratch or test notes, not access control.
- `SECURITY.md` (line 26 and any other hit): `agent_id`. Add one bullet with the same private-scope statement:
  any client attached to the same database can read `private` records by configuring the same `SALTMDB_AGENT_ID`.

## 10. Tests

**Existing tests (86 files):** update mechanically per §2. Known hardcoded copies of changed
strings that a symbol rename will not find — fix these explicitly:

- `owner="` XML attribute expectations: `tests/test_session_digest_service.py`, `tests/test_search_accuracy_stage2.py`.
- `SALTMDB_OWNER_ID` environment values: `tests/test_adapter_signal_shutdown.py`, `test_cli.py`, `test_corpus_snapshot_mcp.py`,
  `test_mcp_server.py`, `test_mcp_tools.py`, `test_session_identity.py`.
- Imports of `get_owner_id` (4 files) and `validate_owner_id` (1 file); `configure_owner` (47 uses, mostly
  `tests/test_session_identity.py` and `tests/test_mcp_tools.py`); `_OWNER_INJECTED_TOOLS` (1 use).
- No test patches a renamed symbol by dotted string (`patch("...get_owner_id")` count is 0), but re-check
  with `rg -n "patch.*owner" tests` after your edits.

**New file `tests/test_agent_id_rename.py`** — write these first (red), then implement to green. This file is
allowed to contain the old names.

1. Fresh `init_db` on a temp path: `entities`, `_agent_sessions`, `conversation_traces`, `tool_call_telemetry` each
   have `agent_id` and no `owner_id`; indexes `idx_entities_agent_scope` and `idx_traces_agent_status` exist and the
   two old-named indexes do not; `idx_entities_content_hash` SQL mentions `agent_id`.
2. Legacy DB: build it by running `init_db`, then reversing the change with raw SQL (`RENAME COLUMN agent_id TO owner_id`
   on the four tables, drop the two new-named indexes, create the two old-named ones), insert one `private` and one
   `shared` entity owned by `'claude'`, then run `init_db` again. Assert: columns renamed, old indexes gone, new
   indexes present, rows preserved, `PRAGMA integrity_check` is `ok`, and a search as `'claude'` sees both while a
   search as `'codex'` sees only the shared one (visibility filter still works through the renamed column).
3. Running `init_db` a second time on the migrated DB changes nothing and does not raise.
4. `_agent_sessions` in its earliest shape (only `session_id`, `started_at`, no `owner_id`): `init_db` ends with an
   `agent_id` column and no error.
5. A table that has both `owner_id` and `agent_id` makes `init_db` raise `RuntimeError` naming the table.
6. Config precedence: only `SALTMDB_AGENT_ID` works; only `SALTMDB_OWNER_ID` works and logs a deprecation warning;
   both set and different → `SALTMDB_AGENT_ID` wins and one warning names both; neither → `RuntimeError` whose message
   contains `SALTMDB_AGENT_ID`. Invalid values are rejected by the same regex under either name.
7. Legacy wire guard: a `hello` carrying `owner_id` gets `MALFORMED_REQUEST` and registers no session; a `tool_call`
   whose kwargs contain `owner_id` gets `MALFORMED_REQUEST`; both messages contain `agent_id` and `restart`.
8. `RpcBackend.call` with a stub `daemon_client.call` capturing kwargs: `log_event` and `store_memory` carry the
   adapter's `agent_id` even if the caller supplied another; `get_events(agent_id='codex')` reaches the daemon with
   `'codex'` untouched; a non-injected tool such as `search_tags` has a stray `agent_id` removed; a bulk item's stray
   `agent_id` is stripped by `_strip_item_agent_id`.
9. Telemetry: a `store_memory` call records the adapter's `agent_id`; a `get_events(agent_id='codex')` call records `None`.
10. `record_session` and the hello path persist `agent_id` into `_agent_sessions.agent_id`.

## 11. `MIGRATION.md`

Append one row at the end of the Version Schema Registry table (after the `v0.1.0-alpha.104` row): Package Version
`Unreleased`, Schema Version `20`, describing: columns `owner_id` → `agent_id` on `entities`, `_agent_sessions`,
`conversation_traces`, `tool_call_telemetry`; index renames; `PRAGMA user_version` unchanged (3); automatic in
`init_db`, idempotent; env var `SALTMDB_AGENT_ID` with `SALTMDB_OWNER_ID` fallback; wire/response key rename with the
stale-adapter guard; snapshot files exported earlier are not readable. Then add a short **Live cutover** subsection:
stop every agent and the daemon first; copy `saltmdb.db` together with its `-wal` and `-shm` files as a backup; update the
checkout; restart; a downgrade requires the reverse `RENAME COLUMN` statements (list them). The live database is
migrated by the user at cutover, never by you.

## 12. Out of scope

- Remote mode, tokens, any network binding, the client/server dependency split (separate specs that follow this one).
- Renaming `agent_session_id`; changing `scope` semantics; removing or reinterpreting `private`.
- Rewriting stored JSON payloads in existing rows; adding a legacy-snapshot reader.
- The legacy-input tolerance for the old env var beyond §3, and any tolerance for the old wire key beyond the §5 guards.
- Version bump, `pyproject.toml`, `uv.lock`.
- Historical documents and existing `MIGRATION.md` rows; anything outside this repository, including harness MCP
  configs, `~/.agents`, and live databases.
- The daemon-election "owner", English prose, vendored or DOM JavaScript, and any refactor or cleanup not required
  by the rename.

## 13. Acceptance

Run from the worktree root. First, **before editing anything**, run the test/lint commands in **D** once on the
unmodified tree and record the result; a failure that already exists there is not yours, and you report it rather than fix it.

**A. Residual old-name scan** — must print exactly these seven lines, in this order:

```
rg -l -i -e 'owner_id' -e '_effective_owner' -e 'configure_owner' -e 'owner_val' -e '_OWNER_INJECTED' \
   -e 'inherited_owner' -e 'existing_owner' -e 'item_owner' -e 'eowner' -e 'ownerField' . \
   --glob '!.venv/**' --glob '!docs/specs/SPEC-*' --glob '!docs/specs/beta-readiness-tdd-plan.md' \
   --glob '!docs/specs/conversation-trace-provenance-plan.md' --glob '!docs/specs/**' | sort
```

```
./INSTALL.md
./MIGRATION.md
./scripts/benchmarking/build_lexical_snapshot_db.py
./src/saltmdb/config.py
./src/saltmdb/daemon/server.py
./src/saltmdb/db/schema.py
./tests/test_agent_id_rename.py
```

(On the unmodified tree the same command lists 138 files.)

**B.** `rg -n "'Owner'|Owner id" src/saltmdb/viewer` prints nothing.

**C.** `git diff --name-only -- docs/specs/SPEC-CONVERSATION-TRACE-PROVENANCE-PHASE1.md docs/specs/beta-readiness-tdd-plan.md docs/specs/conversation-trace-provenance-plan.md pyproject.toml uv.lock` prints nothing.

**D. Tests and lint** (the repo's documented commands):

```
PYTHONPATH=src .venv/bin/python -m pytest tests/ hooks/tests/
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy src
```

Everything that passed on the unmodified tree passes now, and every test listed in §10 exists and passes.

**E. Finish state.** Leave the whole diff **uncommitted and unmerged** in this worktree. Do not commit, do not merge, do not push.
If you hit a spec contradiction, report `BLOCKED — SPEC ADJUDICATION REQUIRED` with the exact conflicting sections and stop.
