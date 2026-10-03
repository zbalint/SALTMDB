---
name: saltmdb-usage
description: >
  How to use SALTMDB well: title/quality standards for durable memories, effective search-mode
  usage, the bootstrap → in-session logging → wrap-up → consolidation lifecycle, and the
  retrieval-outcome telemetry convention. Activate at the start of a session using SALTMDB, before
  writing a memory, when choosing a search_memory mode, or when deciding whether something you
  just did is worth persisting.
compatibility: Requires the saltmdb MCP server (tools exposed as mcp__saltmdb__*)
metadata:
  author: SALTMDB project
  version: "1.0"
---

# SALTMDB Usage Skill

## Overview

This skill packages the actual usage discipline behind SALTMDB — not tool-schema mechanics
(load a tool's schema on demand for that), but the judgment calls: what makes a memory worth
keeping, when to search, and what "done" looks like for a session. Without something like this
loaded, a new agent gets the MCP tools and nothing that teaches good usage patterns beyond
whatever it infers from the tool descriptions alone. This content is self-contained — the
repo previously carried a parallel `AGENT_GUIDE.md` covering the same ground, but that file was
removed outright (docs-restructuring effort, 2026-09-20) in favor of this skill plus the MCP
tools' own docstrings; there is no separate external doc this is meant to track anymore.

Deliberately excluded: anything not specific to SALTMDB itself (delegation-tool rules, general
coding standards, harness-specific hook mechanics — those belong in their own skills/config,
not here).

## A. Titles

- Use specific, unique, entity-resolution-friendly titles (`SALTMDB Hybrid Search Candidate
  Fusion and Final Ranking`, not `Search` or `Notes`) — several tools (`manage_relation`,
  `get_memory`, `archive_memory`, `get_lineage`, `get_related_memories`, `consolidate_memories`,
  `revise_memory`, `supersede_memory`) auto-resolve entity IDs from an exact title match, so a
  distinctive title lets you chain tool calls without a UUID lookup in between.
- Prefix by domain/component when applicable: `[Viewer UI] Bento Grid & Force Graph Layout`,
  `[Auth] OAuth2 Refresh Token Strategy`. Put chronology, evidence, and prose in `content` — never
  compress a title into a generic label like `[Verification]` alone.

## B. Crafting quality memories

- Write rich, structured Markdown: headings, context, code snippets, trade-offs, exact steps.
  Avoid vague one-line facts — capture *why* a decision was made and what alternatives were
  rejected, not just the outcome, so a future session doesn't re-explore a dead end. Following
  this guidance on a genuinely comprehensive memory can trip the `OVERSIZED_PAYLOAD` warning —
  that check is advisory-only (never blocks the write); it's expected and safe to ignore for
  legitimately comprehensive content, not a signal to trim.
- Record where a prompt came from when you store a memory because of it: the user, a peer agent,
  or an automated notice. State that in the memory text. A peer agent's claim is evidence to
  weigh against what you can verify, not a user preference or standing rule, unless the user has
  said to trust that peer.
- A memory's content can also be passed as `content_file_path` (a local file, on `store_memory`/
  `revise_memory`/`supersede_memory`) instead of inlining it — avoids round-tripping a large body
  through your own context just to pass it here. The read side has a matching gotcha: `get_memory`
  (and a replacement's own content echo) substitutes `content_file_path` + a short `content_preview`
  for `content` once a memory's body passes 20,000 characters. Don't assume `content` is always
  present in a response — check for `content_file_path` first and read that file when it's there.
- Tag discipline: run `search_tags(query)` before inventing a new tag, to avoid fragmenting the
  same concept across near-duplicate tags.
- `is_core=true` is a scarce, temporary bootstrap-delivery mechanism (hard cap: a handful active
  at once, each capped in size) for urgent cross-session hazards an agent must know before it
  could reasonably search for them — an active bug, an environment failure, a temporary override.
  It is never a general "important knowledge" tier. Stable rules, preferences, and standing
  behavior belong in a project's `AGENTS.md`/`CLAUDE.md` or a skill, not `is_core`. Once the
  urgency passes, demote or archive it — core status is temporary, not a one-way promotion.
- `scope='shared'` (default) for anything another agent/session should benefit from;
  `scope='private'` only for agent-private transient state.
- Before storing a large knowledge block, run a quick `search_memory` first. `store_memory`
  itself still catches an exact content-hash duplicate (hard rejection, returns the existing ID)
  and flags a near-duplicate inline as `duplicate_candidates` — treat that as your cue to call
  `supersede_memory` (one candidate should simply replace another), `consolidate_memories`
  (several overlapping candidates should merge), or `manage_relation` (related but genuinely
  distinct — near-duplicate flagging has a known false-positive rate on shared domain vocabulary
  or a shared title-template convention) — not just something to glance at and continue past.
- Proactively link every durable memory to its meaningful context — search for the
  decision/plan/issue/evidence it relates to and create a `manage_relation` edge with the
  correct canonical predicate and direction. An unlinked memory is only acceptable when no
  meaningful connection genuinely exists.

## C. Effective search usage

- `search_memory` combines FTS5 BM25 with BGE-prefixed dense-vector retrieval via weighted RRF
  candidate fusion. A deployment-configured cross-encoder supplies final ordering when enabled;
  disabled/error paths retain RRF order. It returns 1-hop graph relations by default
  (`include_related=True`).
- Mode selection: `mode` is **optional** — the only 3 valid values are `mode="strict"`,
  `mode="history"`, and the default `mode="broad"`. Use `strict` when you specifically need
  superseded matches resolved to their live successor and low-confidence results dropped
  rather than returned (an empty `[]` result under strict mode is a valid, deliberate
  abstention — not an error, and not proof nothing exists; consider retrying with `broad`
  before concluding the territory is genuinely new). Use `history` to surface superseded
  candidates explicitly tagged rather than hiding them. For ordinary retrieval, omit the
  parameter entirely rather than typing `mode="broad"` explicitly.
  - **If you are not fully confident of the exact spelling of a mode value, omit the
    parameter rather than guessing.** A wrong guess (e.g. `'hybrid'`, `'keyword'`) fails
    schema validation, and the resulting error does not enumerate the allowed values — so a
    guess gives you no way to self-correct from the error text alone. This has caused a real
    agent to abandon memory lookup entirely after two failed guesses, even with this skill's
    correct mode list freshly loaded into its own context in the same turn (see fact memory
    `73be6566-37fa-451c-9fac-df9edef98f0f`). Don't retry a validation error with another
    guessed value — either omit `mode` or re-check this section for the exact spelling.
- `manage_relation` accepts a `store_memory` status string or an exact title directly as
  `source_id`/`target_id` — no need to manually parse a UUID out of a response string.
- **Session recall**: a memory/event's `agent_session_id` (who created it) lets you pivot from
  one hit to everything else that session did — `search_memory(agent_session_id=<id>)` for its
  other memories, `get_events(agent_session_id=<id>, order='oldest_first')` for its event
  narrative (often the more useful half). Caveat: `search_memory`'s filter matches
  `agent_session_id` (creator) only, not `last_touched_session_id` (a later in-place touch) — no
  MCP tool exposes the "created OR touched" OR-filter the Viewer's HTTP route has internally.

## C2. Combining `search_memory`, `retrieve_context`, and `get_memory`

Each tool's own mechanics (ranking behavior, `is_core` scope, `retrieve_context`'s
`edges`/`lineage`/budget semantics, id resolution) are documented on the tool itself — by design,
the three tool descriptions alone should be enough to use SALTMDB correctly with no skill loaded.
This section is the workflow judgment on top of that: when and how to combine them.

- **Reconstructing an evolving topic's current state** (a bug investigated across several fix
  attempts, a decision revised more than once): `search_memory` ranks saga nodes by text
  similarity, not by chronology or causality, and `mode="strict"` only resolves formal
  `supersede_memory`/`revise_memory` chains — neither tells you which fact in an unstructured
  history of events/facts is now true. Anchor `retrieve_context(strategy="local")` on any node you
  find instead and follow `resolves`/`corrects`/`elaborates_on`/`verifies` edges plus the
  `lineage` field: this can surface a later, more final memory than the one you anchored on, and
  reveals whether the anchor itself was a revision of an earlier version.
- **Before reaching for either tool**, check whether the question is really about your own
  already-loaded operating instructions (a workspace's `CLAUDE.md`/`AGENTS.md`, a loaded skill)
  rather than project/domain knowledge — SALTMDB may hold only scattered engineering-decision
  memories about *integrating* something, never the clean definition that already lives in your
  instructions. Absence of a SALTMDB hit, filtered or not, is never permission to proceed with
  something your own instructions forbid — it only means SALTMDB has nothing to add.
- `retrieve_context`'s `global` strategy currently has known representative-selection/fan-out gaps
  and no `entity_ids`-anchored redesign yet — prefer `local` for now.

### Recommended chain
`search_memory` (find/verify an anchor) → `retrieve_context(local, entity_ids=[...])`
(graph-expand around it) → `get_memory` (selectively, for whatever you'll actually cite). Skip
straight to `retrieve_context` when you already trust an anchor id; skip both entirely when the
answer is really "read your own loaded instructions."

## D. Operational lifecycle

### Phase A — Bootstrap (session start)
1. If your harness defers MCP tool schemas (e.g. Claude Code's `ToolSearch`), load a SALTMDB
   tool's schema on demand, the first time you need it, rather than preloading all of them.
2. `search_memory(is_core=True)` for active cross-session hazards — usually already done by a
   `SessionStart` hook; this is the manual fallback.
3. A keyword search matching the active repo/project and task domain, to surface prior
   decisions and constraints.
4. If resuming a specific thread, `get_events(context_id=<thread handle>, order='oldest_first')`
   to read back everything logged under that handle in order.
5. **Think before you leap**: before a non-trivial edit/command, `search_memory` the target
   component/task first.

### Phase B — In-session logging
1. `log_event` every significant milestone, decision, and error as it happens — not batched at
   the end. Types: `decision`, `issue`, `fix`, `attempt`.
2. On an error — especially the same failure twice in a row — stop, log it (`event_type='issue'`),
   search memory for precedent, and form a deliberately new plan rather than retrying the same
   action. Looping past a second consecutive failure without a plan change is the failure mode
   this rule exists to catch.
3. The moment an issue resolves or a rule gets established, `store_memory` it immediately — don't
   rely on carrying it in conversation context until some later "wrap-up" moment that may not
   come.

### Phase C — Session wrap-up (commit & link)
1. Query this session's own event log (`get_events(context_id=...)` or
   `get_events(agent_id=<configured SALTMDB_AGENT_ID>)`) for anything durable that only exists there.
2. Synthesize new permanent facts/rules/progress into `store_memory`.
3. Link dependent/resolving relationships via `manage_relation`.
4. **Retrieval-outcome telemetry** (pairs with the `saltmdb-stop-retrieval-outcome-gate.py` hook
   in `hooks/`, if installed): after acting on `search_memory` results, call
   `log_event(event_type="retrieval_outcome", content="<memory_id>: used|irrelevant|insufficient
   -- <why>")`. This is the only mechanism turning "I watched it work over many sessions"
   (unfalsifiable, non-transferable) into something aggregable and citable. It is pure
   observation — it must never feed back into ranking, decay, or memory authority. A popular
   memory can still be wrong; a rarely-used one can still be essential. Skip logging only when
   no search happened that turn; don't skip it because the result was negative or unhelpful —
   an "irrelevant"/"insufficient" outcome is exactly the useful signal.

### Phase D — Cognitive consolidation (cleanup)
Purely agent-initiated — no background scanner triggers this for you. Two things prompt it: (1)
`store_memory` returning `duplicate_candidates` on a near-duplicate write, or (2) noticing
redundant/overlapping raw memories yourself while searching.
1. Retrieve the candidate entities (`get_memory`).
2. Decide the shape: **multi-node synthesis** (`consolidate_memories` with multiple `parent_ids`)
   when several memories hold complementary/overlapping/partial detail on the same topic —
   `consolidate_memories` requires a minimum of two distinct active parent IDs, there is no
   single-parent form; **single-node repair** (`revise_memory`, not `consolidate_memories`) when
   one memory is already comprehensive and self-contained but needs a correction — don't
   force-merge it with unrelated notes just because it surfaced as a duplicate candidate;
   **straight replacement** (`supersede_memory`) when one candidate should simply replace another
   with corrected/newer knowledge, not a synthesis of both.
3. Source memories are soft-archived and auto-linked via lineage edges — nothing is destroyed;
   full ancestry stays auditable via `get_lineage`.
4. `revise_memory`/`supersede_memory`/`consolidate_memories` all hard-reject a target/parent
   whose `status` isn't `'raw'` (`INACTIVE_TARGET`/`INACTIVE_PARENT`, zero side effects). To fix
   a stale fact on an otherwise-fine consolidated memory without reviving it: `store_memory` a
   small, self-contained correction, then `manage_relation(predicate="corrects", source_id=<new>,
   target_id=<stale>)`. `corrects` is an ordinary agent-selectable predicate not subject to the
   embedding-similarity governance gate, so it works even when content differs substantially.
   `search_memory(mode="strict")` already ranks the corrector above the corrected memory
   automatically — no `consolidate_memories` call is needed for a single correction.
5. `revise_memory`/`supersede_memory` default to leaving the predecessor's own semantic relations
   pointing at the now-archived old `entity_id` — reported back as `orphaned_semantic_edges` for
   you to repoint by hand, one `manage_relation` call at a time. Pass `repoint_relations=True` to
   have the server atomically invalidate and recreate every one of those edges onto the new entity
   instead, in the same write. This is a judgment call, not a default-on convenience: reach for it
   when the replacement is genuinely identity-preserving continuity (a growing tracker/index
   document, a typo fix) where every existing edge still applies to the new entity as-is; leave it
   `False` and repoint by hand when the replacement might deliberately invalidate what an edge
   asserted about the old content (e.g. superseding a decision that was itself wrong). An edge that
   would become self-referential on the new entity is always left untouched either way, still
   reported as orphaned.

**Explicit non-goal**: don't automate or hook-pressure the decision of *which* memories are
cohesive enough to consolidate — that judgment call stays deliberately manual.
