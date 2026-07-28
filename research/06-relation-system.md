# SALTMDB Relation/Graph System — Research & Improvement Plan

> **Status: Quick Wins implemented in v0.1.0-alpha.55** (2026-07-28). Shipped: a `UNIQUE(source_id, target_id, predicate)` index on `relations` (preceded by a one-time dedup backfill), and a new standalone `predicates` canonicalization table (mirroring tags' `canonical_id`-alias shape, seeded with `resolves`/`depends_on`/`references`/`elaborates_on`/`consolidated_from`/`supersedes`/`relates_to`, with `relates_to`/`references` pre-aliased to `elaborates_on`) via `resolve_or_create_predicate()` in `relation_service.py`, wired into `store_relation`/`bulk_store_relations` (an `ON CONFLICT DO NOTHING` no-op on duplicate edges instead of inserting a second identical row). New `get_canonical_predicates` MCP tool mirrors `get_canonical_tags`. Medium-Term (Graphiti-style `valid_to` versioning in `commit_consolidation`, `parent_ids`/`relations` redundancy resolution) and Larger Bets (confidence/provenance fields) remain future work — see `MIGRATION.md`'s alpha.55 entry.

*Scope: the typed directional knowledge-graph layer (`relations` table, `relation_service.py`, `manage_relation`/`inspect_graph` MCP tools). Web research only — no SALTMDB source was re-read for this document; findings and the plan below reason from the grounding description supplied in the task brief.*

---

## Research Summary

### 1. Predicate vocabulary: fixed enum vs. free text vs. hybrid

Neo4j's own modeling guidance is unambiguous that relationship types should be **specific, verb-based, and few in number** — "CONTROLLED_BY" beats "CONNECTED_TO" because a narrow, well-named type lets the engine (and the human reading a query) traverse only what's relevant, and avoids "gather-and-inspect" scans across a catch-all type. Neo4j also explicitly warns against **semantically symmetric duplicate types** (`PARENT_OF` vs `CHILD_OF` encode the same fact twice) — directly relevant to SALTMDB, which already has this exact risk with predicate variants like `elaborates_on` / `relates_to` / `references` being used interchangeably.

At the same time, pure controlled vocabularies have a known cost: a "restrictive list of pre-selected terms" improves consistency but requires **governance overhead** to extend — Wikidata's property system is the largest live example: every new property goes through a week-plus community **property-proposal process** with discussion, scope definition, and post-hoc **property constraints** that flag misuse after the fact (constraints affect an estimated 99% of Wikidata properties). That process works because Wikidata has a standing community; a single-user/multi-agent local tool like SALTMDB does not have the bandwidth to run a proposal process for every new predicate an agent wants to coin mid-task.

The literature on **Open Information Extraction (OpenIE) canonicalization** is the closest analogue to SALTMDB's actual situation: multiple independent extractors (here, multiple LLM agents) each mint their own free-text relation phrases ("founded", "was founded by", "established") for what is semantically one relation. The standard answer in that literature is not to force a fixed schema at write time, but to **cluster/canonicalize free-text relation phrases after the fact** — approaches include hierarchical agglomerative clustering over relation-phrase embeddings (Galárraga et al., "Canonicalizing Open Knowledge Bases") and embedding/VAE-based clustering (CESI, CUVA) that map noisy surface forms to canonical relation clusters while keeping the original text as a "surface form" pointer.

**Position:** SALTMDB should not adopt a hard enum. It should adopt the same **hybrid free-text + canonical-mapping layer it already runs for tags** (`get_canonical_tags`/`merge_tags`). This is directly precedented by the OpenIE canonicalization literature (surface form → canonical cluster, non-destructive) and avoids the Wikidata-style governance tax that doesn't fit SALTMDB's usage pattern (background agents inventing predicates on the fly, no standing review community).

### 2. Graphiti's bi-temporal model — concrete pattern to copy

Graphiti (Zep's open-source temporal graph library, and the most directly relevant prior art since SALTMDB already has unused `valid_from`/`valid_to` columns) implements a genuine **bi-temporal** model with four timestamp fields per edge:
- `created_at` — when the edge was written to the DB (transaction time, system clock)
- `expired_at` — when the edge was invalidated at the DB level (nullable; null = still current)
- `valid_at` — when the fact became true in the real world
- `invalid_at` — when the fact stopped being true in the real world

Graphiti's own docs state the operating rule plainly: *"Facts have validity windows. When information changes, old facts are invalidated — not deleted. Query what's true now, or what was true at any point in time."* The invalidation algorithm (per Zep's engineering blog and the Neo4j developer-blog writeup of Graphiti): when a new edge is extracted, an LLM-driven "invalidation" pass runs against the semantically/keyword/graph-retrieved set of existing similar edges to detect contradiction (e.g., new edge `Maria —works_as→ senior manager` vs. existing `Maria —works_as→ junior manager`). On a detected conflict, the **old edge gets `expired_at` (and, if a real-world date is extractable, `invalid_at`) set — it is never deleted or overwritten**, and the natural-language fact is often rephrased to carry the history ("Maria used to work as a junior manager, until her promotion"). When multiple non-chronologically-ingested episodes conflict, Graphiti orders them by extracted `valid_at` and invalidates whichever occurred earlier on the real-world timeline, not whichever was inserted first.

**Position:** this is a direct, adoptable pattern for SALTMDB's already-present but dormant `valid_from`/`valid_to` pair. SALTMDB's current `commit_consolidation` behavior — **mutating (`UPDATE`)** existing relation rows to repoint them at a new consolidated entity when their old endpoint is archived — is the opposite of Graphiti's philosophy. Graphiti would model that same event as: leave the old edge alone, set its `valid_to`/expiry, and **insert a new edge** from source to the new consolidated entity. That preserves an honest audit trail ("this edge used to point at the pre-consolidation entity, until date X") instead of silently rewriting history.

### 3. Edge confidence/provenance in LLM-extracted graphs

This is now a mainstream concern in GraphRAG-style pipelines specifically because LLM-asserted triples are not uniformly reliable:
- **Microsoft GraphRAG** attaches a `weight` (aggregated relationship strength, used directly in Leiden community detection) and a `description` plus `text_unit_ids` (source provenance — which chunks a relationship was extracted from) to every extracted relationship edge. Confidence and source-traceability are treated as first-class edge properties, not afterthoughts.
- **TrustGraph** goes further and keeps a dedicated **provenance layer** (a separate named graph, `urn:graph:source`) recording how each triple entered the system, aligned to the W3C **PROV-O** ontology (Entities, Activities, Agents, with relations like `wasDerivedFrom`, `wasGeneratedBy`). PROV-O is the standard vocabulary for "which process/agent produced this data, and from what" — a well-trodden design for exactly the source/confidence gap SALTMDB has.
- Academic work on KG quality control (e.g. confidence-aware KG augmentation with LLMs, multi-layer triple-confidence scoring) treats confidence as a **continuous, aggregable** score assembled from multiple signals (extraction-model confidence, cross-verification hits, domain classifiers) rather than a boolean valid/invalid flag.
- A related and directly useful pattern from KG-update literature: when the **same fact is asserted again**, well-designed systems don't just no-op or reject — they **increase the edge's confidence/weight** (reinforcement), and when a *contradicting* assertion arrives, they decrease it, rather than doing a hard overwrite. This maps cleanly onto "what should happen when two agents independently assert the same relation."

**Position:** yes, this is a good and increasingly standard fit for SALTMDB. Every SALTMDB edge today is agent-asserted with zero indication of which agent/session produced it or how confident that assertion should be treated — this is exactly the gap the GraphRAG/TrustGraph/PROV-O precedent addresses. A minimal `source`/`created_by_owner_id` field (provenance) plus a lightweight numeric `confidence`/`weight` column (defaulting to 1.0, bumped on reassertion) would bring SALTMDB in line with current practice without requiring a full PROV-O ontology import.

### 4. Entity resolution / edge dedup

Neo4j's own guidance is direct: use `MERGE` (match-or-create) for relationships, and pair it with a **uniqueness constraint** on whatever you're merging on — without one, "Neo4j will do a full label scan on every MERGE call... a fast upsert into a slow table scan at scale." Neo4j's de-duplication write-up frames this as the general graph-DB answer to "does this edge already exist": match on the combination of endpoints (+ type, if relevant) before writing, and enforce it with a real constraint/unique index rather than relying purely on app-level pre-checks (which race under concurrent writers). Patent/production literature on large-scale triple stores describes the same idea via an **edge signature** (derived from the two endpoint IDs + predicate) used as a fast existence/dedup key, plus a **count column** so repeated identical assertions increment a counter instead of inserting duplicate rows.

**Position:** `store_relation` should check for an existing `(source_id, target_id, predicate)` tuple before inserting. Given SALTMDB is SQLite with potentially concurrent agent writers, the check-then-insert should be backed by a genuine **unique index** on `(source_id, target_id, predicate)` (mirroring Neo4j's "constraint, not just app logic" advice) so the guarantee holds under races, with the app-level check remaining as a fast path to decide whether to no-op, bump confidence, or return the existing row's ID.

### 5. `parent_ids` JSON vs. `relations` edges — is dual representation an anti-pattern?

This isn't a knowledge-graph-specific question so much as a general "hierarchical data in a relational store" one, and the literature there is well-established: the standard options are **adjacency list** (parent pointer — cheap writes, needs recursion to read ancestry), **materialized path** (full ancestry as a delimited string — cheap ancestry reads, expensive re-parenting), **nested sets** (fast reads, very expensive inserts), and **closure table** (a dedicated table pre-computing every ancestor–descendant pair — "fast subtree queries, moderate insert cost, and natural support for edge attributes"). Crucially, every comparative source treats these as **alternative** representations of *one* relationship, and the implicit assumption throughout is that you pick **one** as the source of truth — none of the standard literature describes deliberately maintaining two independently-mutable stores of the same hierarchy edge (one JSON blob, one relational table) as a recommended pattern. That specific redundancy (source of truth split across a denormalized JSON column and a normalized edge table) is the classic denormalization risk called out generically in relational design: two representations of the same fact that must be kept in sync by hand are two chances to drift, and SALTMDB's own `analyze_dependencies` vs `analyze_lineage` split (one walks `relations`, the other walks `parent_ids`) is a direct instance of exactly that risk — a bug in `commit_consolidation`'s edge-repointing logic (item 2 above) could desync the two views of "this entity's history" and neither `inspect_graph` mode would be able to tell you the other disagrees.

**Position:** treat `parent_ids` JSON as a **materialized-path-style read cache**, and `consolidated_from` edges in `relations` as the source of truth, or vice versa — but pick one and make the other derived/generated (e.g., `analyze_lineage` could be rewritten to walk `relations` via `WITH RECURSIVE` on `consolidated_from` edges only, matching how `analyze_dependencies` already works, and `parent_ids` demoted to a cached denormalization refreshed from that walk, or dropped).

### 6. SQLite recursive CTE performance at scale

SQLite's `WITH RECURSIVE` is functionally capable of arbitrary-depth graph traversal (bounded by the recursion/step limits you configure), but the community guidance is consistent on the failure mode that matters here: **recursive CTEs re-visit the same node once per distinct path that reaches it**, so in a densely-connected or cyclic graph the same subtree gets re-expanded repeatedly and query cost blows up combinatorially rather than linearly with node count — this is called out specifically in the SQLite forum's own breadth-first-traversal discussion and in general recursive-CTE-for-graphs writeups. The mitigating practices that show up repeatedly: (a) **index the join columns** (`source_id`/`target_id` — SALTMDB already has these), (b) **hard-cap depth** with an explicit counter column in the CTE (SALTMDB's `analyze_dependencies` already does this via `max_depth`), (c) **cycle detection via a path string with a `NOT LIKE` guard** (exactly SALTMDB's current approach), and (d) for genuinely large/dense graphs, fall back to a precomputed structure (closure table, or an actual graph engine) rather than a live recursive CTE per query. Multiple sources are explicit that once a graph gets large and densely connected, SQLite (or any pure-recursive-CTE-over-a-relational-table approach) is not the right long-term engine — dedicated graph databases exist precisely to avoid this cost curve.

**Position:** SALTMDB's current implementation (depth cap + path-string cycle guard + indexed columns) already follows the standard SQLite mitigations — this is not an urgent problem today. The one gap worth flagging: nothing currently limits *breadth* (fan-out at each level) or total rows scanned per query, so a single entity with an unusually high out-degree (a "hub" node — same problem Neo4j's supernode guidance addresses by restructuring the query direction or adding intermediate nodes) could still make one `inspect_graph` call expensive even within the depth cap. Worth a row-count guard/early-exit if/when the graph grows large, not worth solving preemptively.

---

## Current SALTMDB State

(Restated from task grounding, as baseline for the gaps below.)

- `relations` table: `id`, `source_id`, `target_id`, `predicate` (free-text, unconstrained), `created_at`, `valid_from`, `valid_to`. Indexes on `source_id`, `target_id`, `predicate` individually. FK `ON DELETE CASCADE` to `entities(id)`.
- `valid_from`/`valid_to` exist but `valid_to` is never set by any code path — the schema is temporal-ready but temporally inert.
- No confidence/weight/provenance field on any edge.
- `relation_service.py`:
  - `store_relation`: rejects self-loops only; no predicate validation, no duplicate check, unconditional insert.
  - `analyze_dependencies`: recursive CTE forward traversal from a root, depth-limited, cycle-guarded via path-string `NOT LIKE`, honors `valid_to` (which is never set).
  - `analyze_lineage`: separate recursive CTE over the `parent_ids` JSON column on `entities` — a parallel, non-`relations` path to the same kind of ancestry information that `consolidated_from` edges also encode.
  - `commit_consolidation`: creates `consolidated_from` edges from new → archived parents, **and** `UPDATE`s any existing relation rows pointing at now-archived parents to point at the new entity instead (mutation, not versioning).
  - `bulk_store_relations`: batched insert, same lack of validation as `store_relation`.
- MCP surface: `manage_relation` (wraps `store_relation`), `inspect_graph` (wraps `analyze_dependencies`/`analyze_lineage`, plus an orphan-detection mode).
- Observed real-world inconsistency: agents already use `elaborates_on`, `relates_to`, `references` somewhat interchangeably for similar semantic relationships.

---

## Gaps / Problems

1. **Unused temporal columns.** `valid_from`/`valid_to` are schema-present but functionally dead — no code path ever sets `valid_to`, so every edge is implicitly "valid forever," making the bi-temporal-shaped schema no better than a plain adjacency table today.
2. **No predicate vocabulary at all.** Free-text, unvalidated, no canonicalization layer — confirmed producing real drift (`elaborates_on`/`relates_to`/`references` used interchangeably), which directly degrades `analyze_dependencies`/traversal quality since a query for one predicate silently misses semantically-identical edges stored under a different string.
3. **No confidence/provenance field.** Every edge — whether asserted confidently from a verified fact or speculatively by a background agent — carries identical, permanent epistemic weight. There is no way to prefer, downrank, or even identify the origin of a given edge later.
4. **No edge dedup.** `store_relation`/`bulk_store_relations` will happily insert N identical `(source, target, predicate)` rows; nothing merges or increments a repeated assertion, and there's no unique index backing the intent even if app-level dedup were added later.
5. **`parent_ids` JSON vs. `relations` redundancy.** Two independently-mutable representations of overlapping lineage information (`entities.parent_ids` JSON walked by `analyze_lineage`; `consolidated_from` edges in `relations` walked implicitly via `analyze_dependencies`-style traversal) with no enforced sync mechanism — a classic denormalization-drift risk, and doubly so because...
6. **Edge mutation on archive instead of versioning.** `commit_consolidation`'s `UPDATE` of existing relation rows to repoint at a newly consolidated entity destroys the historical record of what an edge used to point at — directly contradicting the Graphiti-style "invalidate, don't delete/overwrite" pattern that the schema's own `valid_from`/`valid_to` columns already anticipate but don't use. If this UPDATE has any bug or partial-failure mode, `parent_ids` and `relations` can disagree with no built-in way to detect it.

---

## Proposed Improvement Plan

### Quick Wins (low risk, ship soon)

1. **Dedup check in `store_relation`.**
   - Add a `UNIQUE(source_id, target_id, predicate)` index to the `relations` table in `schema.py` (migration-guarded — treat existing duplicate rows found at migration time as a one-time cleanup pass, e.g. collapse to the earliest `created_at`).
   - In `relation_service.store_relation`, do a `SELECT` (or rely on `INSERT ... ON CONFLICT DO NOTHING`/`DO UPDATE`) before/at insert time. On a hit, either no-op and return the existing edge's ID, or (once the confidence field from the Medium-Term phase exists) bump its confidence rather than silently duplicating.
   - Apply the same guard inside `bulk_store_relations`'s loop/batch logic.

2. **Starter canonical predicate list, reusing the tag machinery.**
   - Do **not** hard-enum the `predicate` column (keep it free text — matches the "hybrid" position from the research above and preserves agent flexibility).
   - Seed a small canonical predicate set (e.g. `resolves`, `depends_on`, `references`, `elaborates_on`, `consolidated_from`, `supersedes`) as a first-class analogue to `get_canonical_tags` — either a new `get_canonical_predicates(query)` MCP tool backed by the same alias-table approach already used for tags, or (cheaper to ship) literally extend the existing tag-alias infrastructure to a second `kind='predicate'` namespace if the current implementation is generic enough.
   - Wire `manage_relation` to call this suggestion function before insert (non-blocking — suggest/normalize, don't reject unknown predicates) so `elaborates_on` vs `relates_to` drift gets corrected at the point of write instead of needing after-the-fact cleanup.

### Medium-Term (real behavior change, needs a design pass)

3. **Actually wire up `valid_from`/`valid_to`, Graphiti-style.**
   - Change `commit_consolidation`'s current behavior from `UPDATE`-in-place to: **insert a new edge** from source → the new consolidated entity, and **set `valid_to` = now()** on the old edge that pointed at the archived parent (leave the row in place, don't rewrite its target). This is a direct port of Graphiti's "expire, don't delete/overwrite" rule.
   - Extend `analyze_dependencies` (already checks `valid_to`) to expose a `point_in_time` traversal option, mirroring Graphiti's "query what's true now, or what was true at any point" capability — this becomes possible for free once `valid_to` is actually populated.
   - Define a clear invalidation trigger set up front (this needs a design decision, not just code): today the only known invalidation event is consolidation/archival. Decide whether agent-driven "this relationship is no longer true" assertions should also be supported (would need a new tool surface, e.g. `invalidate_relation(id)`), or whether consolidation remains the only path for now — recommend starting with consolidation-only to keep scope bounded, and revisit if a second invalidation trigger emerges from real usage.

4. **Resolve `parent_ids`/`relations` redundancy.**
   - Pick `relations` (`consolidated_from` edges) as the single source of truth — it is the one both `analyze_dependencies`-style traversal and the temporal-invalidation work above already operate on.
   - Rewrite `analyze_lineage` in `relation_service.py` to run the same `WITH RECURSIVE` pattern as `analyze_dependencies`, filtered to `predicate = 'consolidated_from'`, instead of walking `entities.parent_ids`.
   - Either drop `parent_ids` entirely (breaking change, needs a migration) or demote it to a denormalized cache regenerated from the `relations` walk (safer short-term path, avoids a hard cutover) — flag explicitly in `schema.py`'s docstring/comments that it is derived, not authoritative, if kept.

### Larger Bets (bigger schema/behavior changes, worth scoping separately)

5. **Confidence/provenance field(s).**
   - Add two columns to `relations`: `confidence` (real, default `1.0`) and `source_owner_id` (text, nullable — which agent/owner asserted the edge; SALTMDB already has an `owner_id` concept elsewhere in the system per the CLAUDE.md governance doc, so this is a natural reuse, not a new concept). A `text_unit_ids`/free-text `provenance_note` column is a possible third addition if tracing back to the originating memory content proves valuable (mirrors GraphRAG's `text_unit_ids` and TrustGraph's provenance-layer idea), but start with just `confidence` + `source_owner_id` to avoid over-building before there's a consumer for the richer field.
   - On a duplicate-edge hit (from the Quick Win dedup check), increment `confidence` by a small fixed step (e.g. `+0.1`, capped at some max) instead of a bare no-op — this is the "reinforcement on repeated assertion" pattern from the KG-update literature, and gives `inspect_graph`/search consumers a genuine signal to rank/filter on later.
   - Expose `confidence` as an optional filter/sort key on `inspect_graph` once populated meaningfully.

6. **Revisit whether a full PROV-O-style provenance model is warranted.** Not recommended now — PROV-O's Entity/Activity/Agent triad is real conceptual overhead for a system with one class of "agent" (the calling LLM/tool) and no complex multi-step derivation chains yet. Revisit only if SALTMDB grows a genuine multi-hop derivation story (e.g. edge C asserted *because of* edges A and B) that a flat `confidence`/`source_owner_id` pair can't express.

---

## Risks & Trade-offs

- **Fixed enum vs. flexibility.** The single biggest risk in this whole plan is over-correcting into a hard predicate enum. Agents (multiple LLMs, different sessions, no shared vocabulary negotiation in the moment) need to be able to coin a new predicate mid-task without the write failing or requiring a schema migration. The hybrid approach (free text + suggestion/canonicalization, non-blocking) deliberately keeps `manage_relation` from ever rejecting a novel predicate — but this means canonicalization only helps to the extent agents actually take the suggestion, so it is a soft governance mechanism, not a hard guarantee. If drift keeps happening in practice, the canonical-predicate table needs active curation (same operational cost tags already have) — this is ongoing maintenance work, not a one-time fix.
- **Unique index on `(source_id, target_id, predicate)` changes semantics.** Some existing duplicate rows may currently exist and encode meaningfully different things (e.g. two `elaborates_on` edges from the same source/target created at different times for different reasons) — a naive migration could destroy information. The migration needs an audit pass, not a blind `CREATE UNIQUE INDEX`.
- **Switching `commit_consolidation` from mutate-in-place to insert-new-edge changes existing query results.** Anything downstream that currently expects a single, always-current edge per archived-parent relationship will now see multiple edges (one expired, one current) and must filter on `valid_to IS NULL` — every existing caller of `analyze_dependencies`/`inspect_graph` needs to be checked for this assumption, or it will silently return more/duplicate-looking rows after the change ships.
- **`confidence` field adds a scoring dimension with no established weighting policy yet.** Introducing a numeric confidence without defining how (or whether) it feeds into search ranking/traversal risks becoming a write-only column nobody reads — same failure mode `valid_from`/`valid_to` are in today. Ship the field only alongside at least one real consumer (e.g. an `inspect_graph` sort option), not speculatively.
- **Dropping/demoting `parent_ids`** is a breaking change for anything (including any external tooling or the viewer UI, if one exists) that reads that column directly. Safer to demote-and-keep-in-sync for at least one release before considering removal.

---

## Open Questions for zbalint

1. Should predicate canonicalization reuse the *exact* tag-alias table/mechanism (parameterized by a `kind` column), or is a separate, smaller table preferred to avoid coupling tag and predicate governance together?
2. For the Graphiti-style invalidation change to `commit_consolidation`: is consolidation the only invalidation trigger you want supported near-term, or do you already have a use case in mind for agents explicitly asserting "this relationship is no longer true" outside of consolidation (which would need a new `invalidate_relation` tool)?
3. On `parent_ids` vs `relations`: is there any existing external consumer (viewer UI, another script) reading `entities.parent_ids` directly today that would be broken by demoting it to a derived/cached column?
4. For the confidence field: do you want reinforcement (increment on duplicate assertion) to be automatic and silent, or should it surface as a log event (`log_event`) so the reinforcement history itself is auditable?
5. Is there an appetite for a breaking schema migration (unique index + confidence/provenance columns + `parent_ids` demotion) in one pass, or should these three land as separate, independently-revertible migrations given they touch the same table/rows?

---

## Sources

- [Graph Data Modeling Core Principles — Neo4j GraphAcademy](https://neo4j.com/graphacademy/training-gdm-40/03-graph-data-modeling-core-principles/)
- [Graph Data Modeling: All About Relationships — David Allen, Neo4j Developer Blog](https://medium.com/neo4j/graph-data-modeling-all-about-relationships-5060e46820ce)
- [Graph Database Patterns: Neo4j for Complex Relationship Modeling — Artem Khrienov](https://medium.com/@artemkhrenov/graph-database-patterns-neo4j-for-complex-relationship-modeling-f2281567aada)
- [MERGE — Neo4j Cypher Manual](https://www.neo4j.com/docs/cypher-manual/current/clauses/merge/)
- [Matching and De-duplication in a Graph Database — Neo4j](https://neo4j.com/news/matching-and-de-duplication-in-a-graph-database/)
- [Zep/Graphiti — Getting Started / Overview](https://help.getzep.com/graphiti/getting-started/overview)
- [Graphiti: Knowledge graph memory for an agentic world — Neo4j Developer Blog](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/)
- [Beyond Static Graphs: Engineering Evolving Relationships — Zep Blog](https://blog.getzep.com/beyond-static-knowledge-graphs/)
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv:2501.13956)](https://arxiv.org/abs/2501.13956)
- [getzep/graphiti — GitHub](https://github.com/getzep/graphiti)
- [Methods — Microsoft GraphRAG](https://microsoft.github.io/graphrag/index/methods/)
- [Unravelling Microsoft GraphRAG's Advanced Retrieval Technique — Tanmay Odapally](https://medium.com/@todap/unravelling-microsoft-graphrags-advanced-retrieval-technique-a-deeper-dive-on-indexing-and-local-8c2c41a03f13)
- [Ontologies and Context Graphs — TrustGraph](https://trustgraph.ai/guides/key-concepts/ontologies-and-context-graphs/)
- [Techniques for assigning confidence scores to relationship entries in a knowledge graph (US Patent 10606849)](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10606849)
- [PROV-O: The PROV Ontology — W3C Recommendation](https://www.w3.org/TR/prov-o/)
- [Runnable SQLite Docs: Recursive CTEs — Coddy](https://coddy.tech/docs/sqlite/recursive-cte)
- [SQLite Forum: Breadth-first graph traversal](https://sqlite.org/forum/info/3b309a9765636b79)
- [SQLite Recursive Queries for Graph Traversal: A Deep Dive — Runebook](https://runebook.dev/en/articles/sqlite/lang_with/rcex3)
- [Canonicalizing Open Knowledge Bases — Galárraga et al. (ResearchGate)](https://www.researchgate.net/publication/287457185_Canonicalizing_Open_Knowledge_Bases)
- [Relation Canonicalization in Open Knowledge Graphs: A Quantitative Analysis — Lomaeva et al.](https://2022.eswc-conferences.org/wp-content/uploads/2022/05/pd_Lomaeva_et_al_paper_240.pdf)
- [Recursive CTE vs Closure Tables in MySQL — Ramu Ramaiah](https://medium.com/@ramu.ramaiah/recursive-cte-vs-closure-tables-in-mysql-choosing-the-right-strategy-for-hierarchical-data-c1c89ebd264f)
- [Recursive Query vs Closure Table vs Graph Database — Poom Wettayakorn](https://blog.getdatascale.com/recursive-query-vs-closure-table-vs-graph-database-a-complete-guide-from-my-pov-2a8dd794b733)
- [The Closure Table Pattern for Hierarchical Filters — Boyan Balev](https://balevdev.medium.com/the-closure-table-pattern-for-hierarchical-filters-with-sql-31644e760c09)
- [What are the options for storing hierarchical data in a relational database? — Techgrind](https://www.techgrind.io/explain/what-are-the-options-for-storing-hierarchical-data-in-a-relational-database)
- [Help:Properties — Wikidata](https://www.wikidata.org/wiki/Help:Properties)
- [Wikidata:WikiProject property constraints](https://www.wikidata.org/wiki/Wikidata:WikiProject_property_constraints)
