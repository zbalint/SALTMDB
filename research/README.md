# SALTMDB Improvement Research — Index

Seven research tracks, each produced by an independent web-research subagent, each grounded in
SALTMDB's actual current implementation (schema, service files, known live issues) and each
containing: Research Summary, Current SALTMDB State, Gaps/Problems, a phased Improvement Plan
(Quick Wins / Medium-Term / Larger Bets), Risks & Trade-offs, Open Questions for zbalint, and a
Sources list with real URLs. Research date: 2026-07-27.

None of this touched any code — these are planning documents to execute from selectively, not a
commitment to build all of it.

## The seven tracks

| # | File | One-line takeaway |
|---|------|--------------------|
| 1 | [`01-dashboard-design.md`](./01-dashboard-design.md) | **✅ Phase 1 (Quick Wins) implemented in v0.1.0-alpha.59** (2026-07-29). Killed glassmorphism (`backdrop-filter`+translucent fills) and gradient accents, collapsed to a single reserved accent color, adopted an opaque Linear/Raycast-style surface ladder, replaced the stale-bound SVG donut with a real proportion bar, added spacing/radius token scales (partially retrofit). A found-but-deferred gap: predicate/event-type badges still reuse the status-triad/accent hues categorically, left unresolved. A `<template>`-partials split of the 1728-line template and vendoring `d3-force` remain future work, not shipped this round. |
| 2 | [`02-sqlite-concurrency.md`](./02-sqlite-concurrency.md) | **✅ Implemented in v0.1.0-alpha.53** (2026-07-28). The current WAL setup was sound at this scale — no writer-queue/MVCC engine needed — but the one real bug risk found was confirmed and fixed: writes now use `BEGIN IMMEDIATE` via `write_transaction_retrying`, not Python's default deferred `BEGIN`, so `busy_timeout` actually engages on write-write contention (verified with a real two-connection test). Also fixed a bonus bug found during implementation: bulk operations weren't actually atomic despite claiming to be. See `MIGRATION.md`'s alpha.53 entry. |
| 3 | [`03-agent-memory-systems-survey.md`](./03-agent-memory-systems-survey.md) | **✅ Quick Wins (3 of 4 scoped items) implemented in v0.1.0-alpha.60** (2026-07-29). Architecture comparison of mem0, Letta, Zep/Graphiti, Cognee, ChatGPT memory, Anthropic's memory tool, txtai, LangMem/LlamaIndex. Shipped: `relations` gained an independent `valid_at`/`invalid_at` event/world-time axis (alongside alpha.57's `valid_from`/`valid_to` system/transaction-time pair) plus a new `invalidate_relation` capability via `manage_relation(invalidate=True)`; `episodic`/`semantic`/`procedural` canonical tags seeded additively alongside the existing `memory_type` column; `store_memory` nudges the caller toward `manage_relation` on new tagged memories. A fourth scoped item (`is_core` forcing `scope='shared'`) was dropped before implementation — it collided with `WORKER_TEMPLATE.md`'s documented "Private Core Rule" for subagent workers, a real multi-agent isolation convention this round chose not to silently break. Deferred: wiring the new columns into `point_in_time` traversal, automatic semantic-conflict auto-invalidation, contradiction heuristics, community/summary layer, pluggable graph backend. See `MIGRATION.md`'s alpha.60 entry. |
| 4 | [`04-adjacent-projects-lessons.md`](./04-adjacent-projects-lessons.md) | Broader PKM/graph-DB/ontology/decay survey (Obsidian, Roam, GraphRAG, SKOS, LanceDB, Ebbinghaus/SM-2/FSRS). Cleanest gap: SALTMDB already tracks `weight` and `last_accessed_at` but neither feeds a decay function — free-standing infrastructure with no consumer. |
| 5 | [`05-tag-system.md`](./05-tag-system.md) | **✅ Quick Wins implemented in v0.1.0-alpha.54** (2026-07-28). Diagnosed *why* fragmentation still happened: `get_canonical_tags` was advisory not enforced, and heuristic merging was lexical-only. Fixed via a shared `resolve_or_create_tag()` helper (exact → normalized → new plural/suffix fallback → create) now used by both write paths — which turned up a bonus bug: `commit_consolidation()` had its own divergent, buggier tag logic ignoring `canonical_id` entirely. Medium-Term embedding-based review queue and the faceted/hierarchy model remain future work. See `MIGRATION.md`'s alpha.54 entry. |
| 6 | [`06-relation-system.md`](./06-relation-system.md) | **✅ Quick Wins implemented in v0.1.0-alpha.55; Medium-Term implemented in v0.1.0-alpha.57** (2026-07-29, Schema Version 10). Quick Wins: `UNIQUE(source_id, target_id, predicate)` index on `relations` + standalone `predicates` canonicalization table via `resolve_or_create_predicate()`. Medium-Term: `commit_consolidation` now expires old relation edges (`valid_to = now`) and inserts replacements instead of mutating rows in place (Graphiti's "expire, never mutate," consolidation-only trigger); the unique index became partial (`WHERE valid_to IS NULL`) to allow that; `analyze_lineage` now walks `relations` instead of `entities.parent_ids`, resolving the redundancy (`parent_ids` demoted to derived/display-only); `point_in_time` traversal added; the viewer's independently-duplicated lineage traversal now delegates to `analyze_lineage`. Larger Bets (confidence/provenance fields) remain future work. See `MIGRATION.md`'s alpha.57 entry. |
| 7 | [`07-information-categorization.md`](./07-information-categorization.md) | **✅ Quick Win implemented in v0.1.0-alpha.56** (2026-07-28). Shipped an additive `memory_type` enum column (`fact`/`event`/`procedure`/`decision`/`preference`, CoALA-style episodic/semantic/procedural plus decision/preference, CHECK-constrained, `DEFAULT 'fact'`) on `entities`, wired into `store_memory`/`search_memory` and the MCP tool layer. See `MIGRATION.md`'s alpha.56 entry. **Phase 2's manual-scope precursor was later shipped (a `domain TEXT` column + `VALID_DOMAINS` service-layer enum, alpha.58) then fully removed (alpha.61)** — an adoption audit found it was a closed vocabulary hardcoded to one operator's personal project split, near-zero real adoption, and redundant with `tags`; the embedding-cluster-assisted UMAP/HDBSCAN *suggestion* pipeline Phase 2 actually proposed was never built and is now moot. See `MIGRATION.md`'s alpha.58/alpha.61 entries. |

## Cross-cutting findings worth noting before picking what to build

**Two tracks independently converged on the same fix — shipped in v0.1.0-alpha.57.** Track 3 (agent
memory survey) and Track 6 (relation system) each separately identified SALTMDB's dormant
`valid_from`/`valid_to` columns as the highest-leverage gap, and each independently proposed
adopting Graphiti's bi-temporal invalidation pattern (new edge inserted + old edge's `valid_to` set,
never UPDATE-in-place) as the fix. Two unrelated research passes landing on the same specific prior
art was a stronger signal than either alone — Track 6's narrower proposal (reuse the existing
`valid_from`/`valid_to` pair rather than Track 3's fuller 4-column retrofit with separate
`valid_at`/`invalid_at`) is what shipped, scoped to `commit_consolidation` and consolidation-only
invalidation triggers. See `MIGRATION.md`'s alpha.57 entry.

**One real design collision needed a decision, not two implementations — resolved in v0.1.0-alpha.56.**
Track 5 (tags) proposed a `type:value` / `component:value` *facet convention inside the tag
namespace* (e.g. `type:fix`, `type:feature`) to stop categorical concepts from colliding with
descriptive tags. Track 7 (categorization) independently proposed a dedicated `memory_type` *enum
column* on `entities` (fact/event/procedure/decision) for the same underlying need —
distinguishing what *kind* of memory something is. These were two different mechanisms for
overlapping intent, and building both would have recreated exactly the fragmentation-surface
problem both documents separately warn about (two ways to say the same thing). **Resolved in
favor of the column**: shipped in v0.1.0-alpha.56 as a first-class, CHECK-constrained
`memory_type` column (`fact`/`event`/`procedure`/`decision`/`preference`, `DEFAULT 'fact'`) —
queryable/indexable and closed-vocabulary by construction, versus a tag-namespace convention that
would still live in the same free-text tag table everything else does. Track 7's Phase 2
manual-scope precursor (a `domain` column) was later shipped in alpha.58, then fully removed in
alpha.61 as a generalization fix (near-zero adoption, hardcoded to one operator's project split);
the embedding-cluster-assisted UMAP/HDBSCAN *suggestion* pipeline Phase 2 actually proposed was
never built and is now moot. See `MIGRATION.md`'s alpha.56/alpha.58/alpha.61 entries.

**Two tracks agree the current SQLite/schema-versioning choices are already good, not just
tolerated.** Track 2 explicitly concludes the current WAL setup doesn't need a writer-queue/MVCC
architecture at this scale — most of its "Larger Bets" section is talked out of rather than argued
for. Track 4 independently notes that Roam's Datomic transaction-log model and LanceDB's zero-copy
versioning both validate SALTMDB's existing SCD-Type-2 approach rather than suggesting a
replacement. Worth reading both as "this part of SALTMDB is in reasonably good shape" rather than
assuming every track found a pile of problems.

**The tag and relation tracks each reference the other's machinery — resolved in favor of separate tables.** Track 6's research suggested reusing/extending Track 5's tag-alias infrastructure (parameterized by a `kind` column) for predicates. When Track 6 was implemented (v0.1.0-alpha.55), grounding exploration found `resolve_or_create_tag()`/the `tags` table are not actually generic (no `kind` column, tag-specific `#`-prefix/sanitization logic baked in) — parameterizing them would have meant reworking code that had just shipped in Track 5. Shipped a standalone `predicates` table mirroring the same `canonical_id`-alias *shape* instead of literally sharing the table — same pattern, deliberately not the same rows.

## Suggested cross-cutting priority order

Roughly cheapest-and-highest-value first, pooling all seven tracks' own phase labels:

1. **Immediate correctness check (near-zero cost):** verify `BEGIN IMMEDIATE` vs. deferred
   transactions in the write path (Track 2) — this determines whether the already-shipped 20s
   `busy_timeout` bump is even doing its job.
2. **Quick Wins, low-risk, additive-only, from Tracks 1/2/5/6/7:** dashboard token/CSS pass
   (Track 1), `PRAGMA optimize` + retry/backoff (Track 2), tag write-time validation + mandatory
   canonical-lookup (Track 5), relation dedup + unique index (Track 6 — **✅ done, alpha.55**), additive `memory_type`
   column (Track 7 — **✅ done, alpha.56**).
3. **Medium-term, needs a design pass but no irreversible schema break:** embedding-based tag
   merge-candidate queue (Track 5), Graphiti-style `valid_from`/`valid_to` wiring for consolidation
   (Tracks 3 + 6, converging recommendation — **✅ done, alpha.57**), `parent_ids`/`relations`
   redundancy resolution (Track 6 — **✅ done, alpha.57**), decay function consuming
   `weight`/`last_accessed_at` (Track 4).
4. **Larger bets needing explicit buy-in:** vendoring `d3-force` for the graph view (Track 1, the
   one dependency-boundary exception), confidence/provenance fields on relations (Track 6),
   community/cluster detection over the relation graph (Track 4), faceted/hierarchical tag model
   (Track 5). Track 7's Phase 2 embedding-cluster-assisted `domain`-suggestion pipeline is now moot
   — its manual-scope precursor shipped (alpha.58) and was later removed as a generalization fix
   (alpha.61); building the suggestion layer on top of a column that no longer exists isn't a live
   option without redesigning from scratch.

## Next step

Each file's own "Open Questions for zbalint" section has track-specific decisions (predicate
canonicalization ownership, facet vs. enum, dark-mode-only, embedding threshold tuning, etc.) —
those are the actual per-topic decisions to work through before implementation starts on any one
track.
