# SALTMDB Conversation-Trace Capture & Memory Provenance — Implementation-Ready Plan (v2)

Status: **PLAN, NOT A LOCKED SPEC.** This supersedes the v1 draft produced earlier in the same
investigation. All codebase claims were verified against `develop` (this branch was cut from
`develop` at commit `3bef55d`, which already includes the bounded-batch embedding fix described
in §9 below). Four research passes, direct fetches of the current Codex and Claude Code hooks
documentation, and two live empirical hook tests (one per harness) ground this document. Two
open decisions remain, listed in §16 — resolve those, then run a `spec-writing` pass before
handing anything to OMP.

Naming: the feature is called **traces** throughout (not "turns"), reviving the term from an
earlier, never-locked 2026-09-15 design discussion (memory `e62d47ee`).

---

## 0. Executive summary

The core design premise — a lifecycle hook invoking a SALTMDB MCP tool through the same
already-connected adapter, so the write inherits the adapter's trusted `agent_session_id` — is
**empirically confirmed on both Codex and Claude Code**, not just architecturally inferred. Several
other assumptions in the original brief needed correction along the way:

1. **No existing hook has ever performed a SALTMDB write.** Today's 11 hook scripts are read-only
   CLI wrappers or local nudges that get the *agent* to call tools itself. This is first-of-its-kind.
2. **The daemon already runs a real capability-token handshake per session** (`hello` RPC → token →
   HMAC-checked on every call) — not pure client-side trust. No new trust mechanism is needed.
3. **`retrieval_text` is a column on `entities`, not a separate table.**
4. **There is no single "memory revision" concept.** `store_memory` updates to an existing
   `entity_id` are true in-place UPDATEs (no stable id until next overwrite); `revise_memory`/
   `supersede_memory`/`consolidate_memories` mint a permanent new `entity_id`. A trace-to-memory
   link anchors on `(entity_id, content_hash)`, not `entity_id` alone, to handle both cases.
5. **The two harnesses' event taxonomies are not symmetric** (Claude Code has `Stop`+`StopFailure`,
   no `Interrupt`; Codex has `Stop`+`Interrupt`, no `StopFailure`). **Resolved**: only the
   events common to both harnesses are used (`UserPromptSubmit`, `PostToolUse`, `Stop`) — see §2.
6. **`explain_mode` is the wrong integration point** for RRF-contribution visibility;
   `return_diagnostics`'s per-channel dict is the right one.
7. **RRF fusion is a fixed-parameter function**, not a pluggable registry — adding trace channels
   means editing `weighted_reciprocal_rank_fusion`'s signature directly.
8. **A real viewer exists** (`src/saltmdb/viewer/`) with working session-browsing and
   entity-detail pages — trace provenance has concrete integration points there, not speculative
   ones.
9. **A separate, pre-existing production risk was found and fixed while investigating this
   feature's size policy**: `compute_entity_chunk_embeddings` embedded an entity's entire chunk
   list in one unbounded `embed_texts()` call — the exact pattern that caused two real host
   freezes/OOMs on this project previously. **Already fixed and merged to `develop`** (commit
   `3bef55d`, independent of this feature) via a new `embed_texts_in_batches()` primitive
   (`EMBEDDING_BATCH_SIZE = 32`). Phase 2's trace-embedding work reuses this primitive directly —
   see §9.

---

## 1. Verified current-state findings

*(Unchanged from the v1 investigation; summarized here, full file:line detail available on
request from the earlier research-fork transcripts if needed during spec-writing.)*

- **Identity**: `agent_session_id` is a UUIDv7 minted once per adapter process
  (`mcp/identity.py:47`), injected only via `RpcBackend.call()` (`mcp/tools.py:203-263`) for a
  fixed list of write tools. The daemon (`daemon/server.py:340-502`) runs a real capability-token
  handshake per session but doesn't cross-check the *value* written into a row — there's nothing
  to check against, since that value only ever comes from the same trusted code path. Startup is
  eager (`__main__.py:62-70`, `mcp/server.py:20-52`) — no lazy-connect path.
- **Schema**: `entities` has no dedicated revision-id column. In-place `store_memory` updates use
  an SCD shadow-row (`write.py` `_store_raw_entity`); `revise_memory`/`supersede_memory`/
  `consolidate_memories` mint a new permanent `entity_id` and archive the old row plus a
  `relations` edge. `events` is genuinely append-only. `retrieval_text`/`retrieval_text_hash` are
  columns on `entities`; FTS/vector support is separate (`retrieval_fts`, `retrieval_embedding_jobs`
  + a vec0 table). Migrations are additive `CREATE TABLE IF NOT EXISTS` blocks inside `init_db()`,
  documented in `MIGRATION.md`, forward-only.
- **Search/RRF**: five real channels today (entity FTS, entity vector, chunk vector,
  `retrieval_text` FTS, `retrieval_text` vector), fused in `_compute_pool`
  (`orchestrator.py:381-758`) via `weighted_reciprocal_rank_fusion` (`search_primitives.py:730-774`,
  fixed-signature, `k=60`). `get_memory`'s read-path gap (missing `agent_session_id` exposure) is
  fixed on `develop`. Strict-mode abstention (`ranking.py` `accept_or_abstain`) has no slot for a
  trace-only evidence signal today — see §12.
- **Hooks**: no existing hook writes to SALTMDB (§0.1). Conventions: pure Python, alias-tolerant
  field lookup, one JSON payload carrying every harness's expected keys, per-session state in
  `~/.saltmdb/hooks/.state/`, four documented Windows-specific bugs already fixed once.
- **Viewer**: `src/saltmdb/viewer/routes/entity_detail.py`'s `get_entity_detail()` (backing
  `GET /api/entities/{id}`) returns a curated dict including `agent_session_id`/
  `last_touched_session_id` and a bounded `relations` preview — the natural slot for a bounded
  trace-provenance block. `sessions.py`'s `get_sessions()`/`get_session_detail()` (backing
  `GET /api/sessions[/{id}]`) already aggregate per-`agent_session_id` `memory_count`/
  `event_count` from a persisted `_agent_sessions` table (with `ended_at`/`ended_reason` —
  `'goodbye'` vs `'orphaned'` — maintained by the daemon) — the natural slot for a `trace_count`,
  and critically, **the natural signal for trace-abandonment detection** (§4).

## 2. Hook event protocol (resolved: common-events-only)

Only events **both harnesses actually have** are used — no harness-specific branches, no
asymmetric hook wiring:

| Tool | Fired from | Effect |
|---|---|---|
| `capture_trace_start` | `UserPromptSubmit` | Upsert a `pending` row, keyed `(agent_session_id, harness_turn_id)`. `ON CONFLICT DO NOTHING` — idempotent. |
| `capture_trace_memory_link` | `PostToolUse` (only when the just-run tool is `store_memory`/`revise_memory`/`supersede_memory`/`consolidate_memories` and it succeeded — filtered by the hook script before calling) | Takes `harness_turn_id` + `entity_id`. Server re-reads that entity's *current* `content_hash` itself (never hook-supplied) and inserts the link idempotently. |
| `capture_trace_complete` | `Stop` | Takes `harness_turn_id` + `final_assistant_message`. Transitions `pending → completed`, only if still pending. |

No `Interrupt`/`StopFailure` hooks exist for this feature. Codex's `Interrupt` and Claude Code's
`StopFailure` are simply not wired — both harnesses' "abnormal end" cases fall through to the
same mechanism: §4's abandonment detection. This is deliberately simpler than the original
5-event design, and loses no *consistent* signal, since neither harness could tell you about the
other's abnormal-end case anyway.

Verified live (§3): both harnesses' `mcp_tool` hooks reuse the exact same adapter connection/
identity as in-conversation tool calls, are non-blocking on MCP unavailability, do not recursively
re-trigger other hooks (confirmed for Codex explicitly; strongly implied and structurally
consistent for Claude Code), and support `${field}`-style templating including plain top-level
fields (`session_id`) confirmed end-to-end in both live tests.

## 3. Identity binding — confirmed live on both harnesses

**Option A (adopted): `mcp_tool` hook reuses the existing adapter connection.** No new capability
mechanism, no new trust boundary — new capture tools sit behind the exact same
`_validate_caller_session` wall every existing write tool already sits behind.

Empirical confirmation:
- **Claude Code** (this session, 2026-09-28): a temporary `PostToolUse`/`mcp_tool` hook calling
  `log_event` recorded `agent_session_id: 01a0e816-645b-7f8d-8a7f-244573dc4051` — exact match to
  this conversation's own session identity. Hot-reloaded via the existing settings-file watcher,
  no adapter restart.
- **Codex** (same day, run by a Codex session): same test, same result —
  `01a0e85b-70f0-7bf0-8e61-dc1c20793386` matched on both sides. One caveat Codex itself caught: its
  adapter *restarted* (new `agent_session_id`) during the hook's approval flow, so its first
  pre-approval baseline was stale — Codex correctly re-ran a contemporaneous control call instead
  of trusting it. **New nuance for implementation**: on Codex, installing/approving a new
  `mcp_tool` hook can force an adapter reconnect. Likely a one-time cost at initial hook
  installation (before any real traces are captured), but Phase 1 should explicitly check whether
  this can also happen *mid-session* after hooks are already approved and running — if so, a trace
  mid-flight at that moment would span two `agent_session_id` values, which the idempotency keys
  (scoped to `agent_session_id` + `harness_turn_id`) would silently treat as two unrelated traces.

**Option B (not needed, kept as a documented fallback only)**: a dedicated CLI subcommand reading
a daemon-published session-state file, with daemon-side session-liveness validation of a
*presented* (not injected) `agent_session_id`. This was the fallback for if Option A's
connection-sharing assumption failed empirically — it didn't, on either harness, so Option B adds
unneeded surface (a new CLI write path, a new narrower capability, a new discoverable state file)
and should not be built.

**Multi-session-same-directory** (raised mid-investigation): not live-tested, but architecturally
sound — each harness process is its own MCP client and spawns its own adapter subprocess (standard
MCP stdio semantics: one server process per client connection), so two terminals in the same repo
get independently-identified adapters. This is also protected structurally regardless: the
idempotency key is `(agent_session_id, harness_turn_id)`, not `(harness_session_id,
harness_turn_id)` — `agent_session_id` is already guaranteed unique per adapter process, so even a
harness-side session-id collision across windows couldn't cause a SALTMDB-side collision.

## 4. Schema and invariants

Two new tables in Phase 1:

```sql
CREATE TABLE IF NOT EXISTS conversation_traces (
    id TEXT PRIMARY KEY,                    -- uuid6.uuid7(), chronologically sortable
    agent_session_id TEXT NOT NULL,         -- adapter-injected, trusted, never caller-suppliable
    owner_id TEXT NOT NULL,                 -- mirrors entities.owner_id for scope filtering
    harness TEXT NOT NULL CHECK(harness IN ('codex','claude_code')),
    harness_session_id TEXT NOT NULL,       -- untrusted correlation key
    harness_turn_id TEXT NOT NULL,          -- turn_id (Codex) / prompt_id (Claude Code); untrusted
    status TEXT NOT NULL CHECK(status IN ('pending','completed','incomplete')),
    user_prompt TEXT NOT NULL,              -- FULL text, never truncated -- see §9
    user_prompt_hash TEXT NOT NULL,
    final_assistant_message TEXT,           -- NULL until Stop; FULL text, never truncated
    final_assistant_message_hash TEXT,
    capture_error TEXT,                     -- diagnostic, not a hard failure
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_traces_session_harnessturn
    ON conversation_traces(agent_session_id, harness_turn_id);
CREATE INDEX IF NOT EXISTS idx_traces_owner_status ON conversation_traces(owner_id, status);

CREATE TABLE IF NOT EXISTS trace_memory_links (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES conversation_traces(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,             -- anchors the exact revision, incl. in-place updates
    write_operation TEXT NOT NULL CHECK(write_operation IN
        ('store_memory_new','store_memory_update','revise_memory','supersede_memory','consolidate_memories')),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracelinks_idem
    ON trace_memory_links(trace_id, entity_id, content_hash);
```

**Invariants**:
- `conversation_traces` rows are never returned by ordinary `search_memory`/`retrieve_context`
  results — only a bounded hint (§6) and the dedicated `search_traces`/`get_trace` tools touch
  them directly.
- `agent_session_id` follows the exact same non-caller-suppliable convention as every other write
  tool.
- **No size cap, no truncation, anywhere in the write path** — see §9. This was explicitly
  corrected mid-design: capture the complete text always.
- `owner_id` is stamped and filtered on every read path exactly like `entities` — see §8.
- **Abandonment detection is event-driven, not purely timeout-based.** A background sweep marks a
  `pending` trace `incomplete` when its `agent_session_id` shows up in `_agent_sessions` with
  `ended_at IS NOT NULL` (either `ended_reason`) — this is a real signal the daemon already
  maintains (§1), sharper than an arbitrary timeout. A fixed timeout (proposed: 1 hour) is only
  the fallback for a `pending` trace whose session hasn't been reconciled either way yet.

Phase 2 adds (mirroring the `retrieval_text`/`retrieval_embedding_jobs`/vec0 pattern — the closer
analog, since a trace is an independent optional text stream with its own freshness gate):

```sql
CREATE TABLE IF NOT EXISTS trace_text_chunks (
    id TEXT PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES conversation_traces(id) ON DELETE CASCADE,
    field TEXT NOT NULL CHECK(field IN ('prompt','response')),
    chunk_index INTEGER NOT NULL, chunk_text TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_embedding_jobs (   -- mirrors retrieval_embedding_jobs exactly
    id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, field TEXT NOT NULL, source_hash TEXT NOT NULL,
    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_until TEXT, created_at TEXT NOT NULL
);
-- + a trace_embeddings vec0 table, same shape as retrieval_embeddings, keyed on (trace_id, field, chunk_index)
-- + a trace_fts FTS5 virtual table over trace_text_chunks.chunk_text, mirroring retrieval_fts's trigger sync
```

## 5. State transitions (simplified)

```
pending ──(Stop)────────────────────────→ completed
pending ──(session ended, either reason)─→ incomplete   [event-driven, checked by the sweep]
pending ──(timeout, session still ambiguous)─→ incomplete   [fallback only]
```

Two terminal states, not five. A trace may reach either with zero `trace_memory_links` rows (no
memory was written) or without `final_assistant_message` ever set (`incomplete` traces will
typically lack it) — both expected and diagnosable via `status`, not integrity errors.

## 6. MCP/daemon/API surface changes

- `daemon/dispatch.py`: 3 new `DISPATCH_TABLE` entries (all in `MUTATING_TOOLS`), for the 3
  capture tools in §2.
- `mcp/tools.py`: 3 new public wrappers; extend `RpcBackend.call()`'s injection list.
- **`search_traces(agent_session_id=None, entity_id=None, query_keywords=None, limit, cursor)`**
  (new) — mirrors `search_memory`'s own convention exactly: returns **bounded previews only**
  (trace_id, harness, status, timestamps, short prompt/response snippets, linked entity_ids),
  never full text. `query_keywords` is inert until Phase 2 ships `trace_fts`/`trace_embeddings`.
  `agent_session_id` as a filter gives "what happened in this session" for traces the same way
  `get_events`/`search_memory` already do for events/memories.
- **`get_trace(trace_id)`** (new) — the actual full-content fetch: complete `user_prompt`/
  `final_assistant_message`, explicitly labeled untrusted historical evidence, plus its linked
  memory writes. Replaces the earlier bespoke `get_turn_context` name — this two-call shape
  (`search_traces` then `get_trace`) isn't an artificial privacy gate, it's just consistency with
  the `search_memory`/`get_memory` split every other part of this API already uses.
- `memory_service/lifecycle.py` (`_assemble_memory_record`): new optional param
  `include_trace_provenance: bool = False` → bounded (e.g. 5 most recent)
  `trace_provenance: [{trace_id, harness, created_at, status}]` list, metadata only.
- `memory_service/orchestrator.py` (`search_memory`): add a cheap `trace_evidence_count` field per
  result item (one batched `IN (...)` count query after ranking, not inside the ranking pipeline).
- **Deferred, not built now**: a combined memories+events+traces session-timeline tool. Same
  reasoning as the pre-existing, still-open question from the original `agent_session_id` design
  (memory `a689563e`, decision 5) — three separately-filterable calls
  (`get_events`/`search_memory`/`search_traces`, all sharing `agent_session_id`) get you the same
  information, and UUIDv7's embedded ordering makes client-side merging trivial.
- `config.py`: `SALTMDB_TRACE_CAPTURE_ENABLED` / `SALTMDB_TRACE_EMBEDDING_ENABLED` feature flags
  (both default off pending sign-off). No `MAX_TRACE_TEXT_CHARS` constant — deliberately absent.

No existing tool's *required* argument shape changes anywhere in this feature.

## 7. Retention

**Keep indefinitely — no default TTL.** Consistent with SALTMDB's standing archive-only
philosophy (nothing is ever hard-deleted; entities are archived, never dropped) and with the
project's no-redaction stance (§8). No cleanup job, no config knob for this in Phase 1.

## 8. Security and trust-boundary analysis

- Trace content is untrusted historical conversation data, never given instruction authority —
  every tool description and returned payload for `get_trace`/`trace_provenance` states this
  explicitly.
- `agent_session_id` on `conversation_traces` follows the same non-caller-suppliable convention as
  every write tool — no new trust mechanism for Option A (§3).
- **SUPERSEDED (2026-09-29): trace reads are now cross-agent** -- `owner_id` is attribution, not access control (private scope is an agent's own per-memory choice; traces are auto-captured with no such choice). The bullet below is kept for history only; do not re-add owner filters to trace reads. Writes stay bound to the writing agent.
- **Owner/scope isolation is non-negotiable here, not optional** — the two most recent commits on
  this codebase before this investigation (`7bd34c3`, `56950d2`) were both fixes for exactly this
  class of bug (cross-owner private-memory disclosure). Every new read path (`search_traces`,
  `get_trace`, the `trace_provenance`/`trace_evidence_count` hints) must filter by `owner_id`/
  `scope` with the same rigor from the first line of code.
- **No secret redaction — explicit, deliberate policy decision** (stored as SALTMDB memory
  `6233e904`, revised once). zbalint's reasoning: all captured content — both the user's prompt
  and the agent's response — already passed through the AI provider's servers regardless of which
  side originated it, so by the same "already exposed to a third party" logic neither side is
  "secret" in a way SALTMDB capturing it changes. This is a standing data-handling policy, not
  scoped only to this feature; don't propose redaction middleware for conversational text without
  re-confirming if the context changes (e.g. multi-tenant use, which this system is not).

## 9. Size policy and embedding safety (resolved, and already partly shipped)

**No truncation, no size cap, ever, at storage time.** `conversation_traces.user_prompt`/
`final_assistant_message` store the complete text unconditionally — explicitly corrected mid-design
after an initial (wrong) truncate-with-marker proposal. Rationale (zbalint): the entire point of
capture is to be a cheaper alternative to reading a raw transcript file when a memory alone isn't
enough to decide something; truncating would fail exactly the long, complex traces most worth
having provenance for.

This does **not** mean no safety work — it means the safety work belongs entirely in Phase 2's
*embedding* step, which is where two real past incidents on this project actually happened (memory
`575286de`: an overnight bakeoff run froze the whole host; memory `4bf438f0`: a WSL2 OOM in the
Needle-evaluation experiment) — both caused by passing an entire chunk list to one unbounded
`embed_texts()`/fastembed batch call.

**Already fixed, independent of this feature, merged to `develop` before this branch was cut**
(commit `3bef55d`): `compute_entity_chunk_embeddings` (the production code path with the identical
vulnerability, confirmed still live as of `develop` HEAD `492bd7f` before the fix) now routes
through a new `embed_texts_in_batches(texts, batch_size=EMBEDDING_BATCH_SIZE)` primitive
(`EMBEDDING_BATCH_SIZE = 32`, `config.py`), which splits into fixed-size batches instead of one
unbounded call. Verified: 1741 passed / 12 skipped, `ruff`/`mypy` clean.

**Phase 2's trace-embedding job must call `embed_texts_in_batches()` directly** — it's a shipped,
tested, reusable primitive now, not something Phase 2 needs to invent or (worse) accidentally skip
by copying the historically-vulnerable direct-`embed_texts()`-call pattern. No new engineering
needed here beyond calling the right function.

**Deliberately deferred, not locked now**: whether to also apply a hard `RLIMIT_AS` memory
ceiling (the other proven fix, from the Gate D bakeoff) to wherever trace-embedding jobs execute —
depends on whether that job runs in-process in the daemon (shared with unrelated work, needs care)
or something more isolated. A Phase 2 implementation-time decision, not a planning-time one.

## 10. Failure recovery and cleanup

- Abandonment sweep — §4/§5.
- Both harnesses guarantee non-blocking hook-error behavior (confirmed in both hook docs) — a
  capture-tool exception cannot block the harness's own lifecycle event. Server-side, such an
  error is caught and written to `capture_error` if a trace row exists, never allowed to propagate
  into the daemon's single-writer coordinator.

## 11. Migration and backward compatibility

Purely additive: two new tables (Phase 1), no `PRAGMA user_version` bump, a new `MIGRATION.md`
section. Every tool-surface change is additive (new tools, new optional params) — no existing
caller, including Antigravity, needs to change anything.

## 12. Retrieval integration (Phase 2, feature-flagged off)

- New channels: **`trace_fts`** (FTS5 over `trace_text_chunks`, mirroring `retrieval_fts`'s
  trigger-sync pattern) and **`trace_vector`** (chunked embeddings via `embed_texts_in_batches`,
  mirroring `retrieval_vector_search`'s freshness-gate pattern against `trace_embedding_jobs`).
  Two new channels, not one — `weighted_reciprocal_rank_fusion` gains two new parameters
  (`trace_fts_results`/`trace_fts_weight`, `trace_vector_results`/`trace_vector_weight`), both
  gated entirely behind `SALTMDB_TRACE_EMBEDDING_ENABLED` (the function must not even query the
  new tables when the flag is off).
- `return_diagnostics`'s dict gains a `trace_text` block mirroring the existing `retrieval_text`
  block's shape exactly (`requested`/`executed`/candidate counts/`candidate_evidence`).
- **Strict-mode eligibility for trace-only evidence — open, see §16.** Default recommendation: no,
  until Phase 3 benchmarks justify it. No code path currently plumbs a trace-only evidence signal
  into `accept_or_abstain`'s evidence dict, and adding one is a deliberate decision, not a side
  effect of adding the channel.
- Benchmark plan: extend the existing mature harness (`scripts/benchmarking/`,
  `benchmark_search_option_matrix.py` + friends) with adapter variants — baseline, +prompt-only
  trace channel, +response-only, +combined (separately weighted), plus a weight sweep — measured
  against `golden_queries.json`/`golden_queries_negative.json` for precision and (if §16.1 is
  answered "yes, allow it") strict-mode false-accept rate. `retrieval_adapters.py`/
  `eval_configs.py`'s exact registration interface needs one more read pass before writing the
  actual benchmark code — not done in this planning pass.

## 13. Viewer implications (now concrete, not speculative)

- `viewer/routes/entity_detail.py`'s `get_entity_detail()`: add a bounded `trace_provenance` block
  to the response dict, following the exact pattern `relations`/`tags` already use in that same
  function.
- `viewer/routes/sessions.py`'s `_load_sessions()`/`get_session_detail()`: add a `trace_count`
  alongside the existing `memory_count`/`event_count` aggregation (same `UNION ALL`-style query
  pattern already used for memories there).
- A dedicated trace-browsing page/route is a nice-to-have, not required for Phase 1 — the two
  additions above are enough to make provenance visible without new UI surface.

## 14. Phased implementation plan (file-level)

**Phase 1 — reliable capture, binding, provenance, retrieval APIs (no embeddings)**
- `db/schema.py` — `conversation_traces`, `trace_memory_links` tables + indexes.
- `domain/services/trace_service.py` (new) — capture/state-transition/idempotency logic, mirroring
  `memory_service`'s module structure. Includes the abandonment sweep (§4).
- `daemon/dispatch.py` — 3 new `DISPATCH_TABLE`/`MUTATING_TOOLS` entries.
- `mcp/tools.py` — 3 new public wrappers; extend `RpcBackend.call()` injection list.
- New tools: `search_traces`, `get_trace`.
- `memory_service/lifecycle.py` — `get_memory`'s `include_trace_provenance` param.
- `memory_service/orchestrator.py` — `search_memory`'s `trace_evidence_count` field.
- `viewer/routes/entity_detail.py`, `viewer/routes/sessions.py` — §13's two additions.
- `config.py` — `SALTMDB_TRACE_CAPTURE_ENABLED` flag (default off).
- `hooks/` — new scripts for Claude Code (`UserPromptSubmit`, `PostToolUse`, `Stop`) and Codex
  (same three); extend `_saltmdb_hook_common.py`'s field-extraction helpers for
  `prompt_id`/`turn_id`/`last_assistant_message`; `hooks/README.md` gets its first Codex example.
- Verify whether Codex's hook-approval-driven adapter restart (§3) can recur mid-session, not just
  at install time — resolve before shipping the Codex hook config.
- `MIGRATION.md` — new section.
- Tests — §15.

**Phase 2 — embeddings and experimental search integration (feature-flagged off)**
- `db/schema.py` — `trace_text_chunks`, `trace_embedding_jobs`, `trace_embeddings` (vec0),
  `trace_fts`.
- `embedding_service.py` — trace embedding enqueue, mirroring `retrieval_text`'s job pattern,
  calling the already-shipped `embed_texts_in_batches()` — no new batching logic to write.
- `search_primitives.py` — new `trace_fts_search`/`trace_vector_search` channel functions.
- `search_primitives.weighted_reciprocal_rank_fusion` — direct signature edit, two new params.
- `orchestrator.py` — wire both channels into `_compute_pool`, gated behind
  `SALTMDB_TRACE_EMBEDDING_ENABLED`; add the `trace_text` `return_diagnostics` block.
- `scripts/benchmarking/` — new adapter/config rows (read `retrieval_adapters.py`/
  `eval_configs.py` first).

**Phase 3 — benchmark-driven enablement**
- Run §12's benchmark plan. Flip `SALTMDB_TRACE_EMBEDDING_ENABLED` default only if precision
  improves without an unacceptable false-accept-rate rise — exact bar is §16.2.

## 15. TDD test matrix

- Codex/Claude Code hook payload parsing (`turn_id`/`session_id`, `prompt_id` incl. pre-v2.1.196
  absent-field degradation).
- Duplicate `UserPromptSubmit`/`PostToolUse` delivery → idempotent no-ops (unique-index-backed).
- Concurrent `PostToolUse` for two different `store_memory` calls in one trace → both links
  recorded (exercises the coordinator's single-writer serialization).
- **`store_memory` in-place-update path**: same `entity_id` written twice in one trace → two
  distinct `content_hash`-keyed links, not one collapsed link.
- **revise/supersede/consolidate path**: link uses the operation's *new* `entity_id`, never the
  predecessor's.
- **Abandonment**: a `pending` trace whose session shows `ended_at IS NOT NULL` in
  `_agent_sessions` → `incomplete` immediately (event-driven path); a `pending` trace past the
  timeout with an still-ambiguous session → `incomplete` (fallback path); both paths tested
  separately.
- No truncation regardless of size — a very large `user_prompt`/`final_assistant_message` is
  stored complete, byte-for-byte.
- **Owner/scope isolation**: a trace created under owner A is invisible via `get_trace`/
  `search_traces`/`trace_evidence_count`/`trace_provenance` for a call scoped as owner B — mirror
  the test pattern from commits `7bd34c3`/`56950d2` directly (read those diffs' test files as the
  template before writing these).
- `agent_session_id` never caller-injectable on any of the 3 new tools — mirror
  `test_write_tools_always_receive_current_agent_session_id`'s existing pattern.
- Phase 2: reuse the embedding-safety test pattern already shipped in `test_embedding_service.py`
  (`TestEmbedTextsInBatches`) directly for trace-chunk embedding — no new pattern needed, same
  primitive.
- Windows path/subprocess compatibility for the new hook scripts, extending the existing
  alias-tolerant helpers.
- Recursion check (empirical, one-time, Claude Code only — Codex already confirms non-recursion
  explicitly in its docs): configure a `PostToolUse` hook matcher broad enough to also match the
  capture tools; confirm a hook-triggered capture call doesn't re-trigger `PostToolUse`.

## 16. Open decisions

1. **Strict-mode eligibility for trace-only evidence** (§12): should a trace-only match (no FTS
   grounding) ever be allowed to independently satisfy `search_memory`'s `mode="strict"`
   abstention gate? Default: no, until Phase 3 benchmarks justify it.
2. **Phase 3 go/no-go bar**: what precision/false-accept-rate threshold makes trace embeddings
   worth enabling by default? Needs real benchmark numbers before this can be set meaningfully —
   don't guess a number now.

Everything else originally listed as open (identity binding, redaction, retention, size policy,
`get_trace`/`search_traces` shape, hook event scope, abandonment detection, viewer integration) has
been resolved during this investigation and is reflected directly in the sections above.

---

*Next step: resolve the two items in §16 when Phase 2 is actually being scoped (they don't block
Phase 1), then run a `spec-writing` pass turning this into a locked, numbered-section spec with an
exact acceptance command, followed by the usual worktree + OMP handoff for Phase 1's actual
implementation.*
