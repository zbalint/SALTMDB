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
whatever it infers from the tool descriptions alone. This content is extracted from and stays in
sync with `AGENT_GUIDE.md` §3–4 in the SALTMDB repo; if the two drift, the repo doc is
authoritative.

Deliberately excluded: anything not specific to SALTMDB itself (delegation-tool rules, general
coding standards, harness-specific hook mechanics — those belong in their own skills/config,
not here).

## A. Titles

- Use specific, unique, entity-resolution-friendly titles (`SALTMDB Hybrid Vector Search
  Architecture & RRF Scoring`, not `Search` or `Notes`) — several tools (`manage_relation`,
  `get_memory`, `archive_memory`, `get_lineage`, `get_related_memories`, `consolidate_memories`,
  `revise_memory`, `supersede_memory`) auto-resolve entity IDs from an exact title match, so a
  distinctive title lets you chain tool calls without a UUID lookup in between.
- Prefix by domain/component when applicable: `[Viewer UI] Bento Grid & Force Graph Layout`,
  `[Auth] OAuth2 Refresh Token Strategy`. Put chronology, evidence, and prose in `content` — never
  compress a title into a generic label like `[Verification]` alone.

## B. Crafting quality memories

- Write rich, structured Markdown: headings, context, code snippets, trade-offs, exact steps.
  Avoid vague one-line facts — capture *why* a decision was made and what alternatives were
  rejected, not just the outcome, so a future session doesn't re-explore a dead end.
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
  `supersede_memory` (one candidate should simply replace another) or `consolidate_memories`
  (several overlapping candidates should merge), not just something to glance at and continue
  past.
- Proactively link every durable memory to its meaningful context — search for the
  decision/plan/issue/evidence it relates to and create a `manage_relation` edge with the
  correct canonical predicate and direction. An unlinked memory is only acceptable when no
  meaningful connection genuinely exists.

## C. Effective search usage

- `search_memory` combines FTS5 BM25 keyword search with dense-vector semantic search via RRF
  fusion, and returns 1-hop knowledge-graph relations by default (`include_related=True`).
- Mode selection: `mode="strict"` when you specifically need superseded matches resolved to
  their live successor and low-confidence results dropped rather than returned (an empty `[]`
  result under strict mode is a valid, deliberate abstention — not an error, and not proof
  nothing exists; consider retrying with `broad` before concluding the territory is genuinely
  new). `mode="history"` surfaces superseded candidates explicitly tagged rather than hiding
  them. Default `mode="broad"` for ordinary retrieval.
- `manage_relation` accepts a `store_memory` status string or an exact title directly as
  `source_id`/`target_id` — no need to manually parse a UUID out of a response string.

## D. Operational lifecycle

### Phase A — Bootstrap (session start)
1. If your harness has a tool-discovery mechanism (e.g. Claude Code's `ToolSearch`), load the
   `mcp__saltmdb__*` schemas before doing anything else, even if you believe they're already
   loaded.
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
   `get_events(agent_id=<your owner_id>)`) for anything durable that only exists there.
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
   when several memories hold complementary/overlapping/partial detail on the same topic;
   **single-node promotion** (`consolidate_memories` with one `parent_id`, re-supplying the
   source's own title/content verbatim if no rewording is needed) when one memory is already
   comprehensive and self-contained — don't force-merge it with unrelated notes just because it
   surfaced as a duplicate candidate; **straight replacement** (`supersede_memory`) when one
   candidate should simply replace another with corrected/newer knowledge, not a synthesis of
   both.
3. Source memories are soft-archived and auto-linked via lineage edges — nothing is destroyed;
   full ancestry stays auditable via `get_lineage`.

**Explicit non-goal**: don't automate or hook-pressure the decision of *which* memories are
cohesive enough to consolidate — that judgment call stays deliberately manual.
