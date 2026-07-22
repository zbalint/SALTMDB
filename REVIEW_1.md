# SALTMDB Rewrite Checklist (Condensed)

## Design principles (check every decision against these)

- **Portable** — nothing tied to a specific model, CLI tool, or machine.
- **Domain-agnostic** — nothing coding/project-shaped baked into schema.
- **Pull-based, not push** — memories only enter context via deliberate
  retrieval, never auto-injected.
- **Relevance over identity** — a relevant memory should surface regardless
  of who wrote it; isolation is for sensitivity, not organization.
- **Server-enforced over agent-trusted** — validate/default mechanically
  wherever possible. Model quality is not a reliable dependency — the real
  floor is lower than any model tested so far.
- **Recoverable over prevented** — where correctness can't be guaranteed
  (lossy consolidation), make failure recoverable instead of trying to
  detect/prevent it upfront.
- **Archive only on supersession or consolidation — never on staleness or
  disuse.** A memory going unaccessed is not evidence it's low-value (see
  rejected: decay). Archiving is only justified when the memory's content is
  now represented somewhere else (a newer version, or a consolidated
  summary) — and the original must stay reachable via a link either way.
- **Cheap signal now, expensive action on demand** — snippet-then-fetch,
  exhaustion flags, status signals — don't pay a cost until it's asked for.
- **Don't build for an unconfirmed gap** — several ideas looked justified
  until a confound was found (tag bug, natural-language-query habit). Verify
  before investing.

---

## KEEP AS-IS (verified working, no change needed)

- Core schema shape: `entities` / `events` / `tags` / `entity_tags` /
  `relations`, SCD-style versioning.
- FTS5 + BM25 keyword search.
- Parameterized SQL throughout — no injection issues found.
- `#core` tag / `is_core` decay immunity.
- Librarian leader-election lock pattern (atomic compare-and-set) — correct
  by inspection, just needs real concurrency tests (see below).
- Stemming + stopword-removal for duplicate detection (already upgraded by
  hand from raw `difflib`) — closes most near-duplicate cases cheaply.
- Bootstrap → log → wrap-up → consolidate session lifecycle concept.
- Metadata-as-JSON-blob mechanism (`json_extract` exact-match filtering) —
  sound mechanism, just needs the constraint fix below.

---

## FIX (bugs / gaps in existing features)

| Item | Problem | Fix |
|---|---|---|
| `analyze_dependencies` cycle detection | Uses title-text matching, not entity ID — false positives truncate real dependency chains | Track visited IDs in the recursive CTE, not title substrings |
| Tag-based search | Currently broken (confirmed in real use) | Already fixed by hand, not yet pushed — verify against repo once pushed |
| `store_knowledge` dedup | Exact-title-match only; fuzzy check exists but not enforced | Call duplicate check internally before falling back to exact match |
| `search_memory` result cap | Hardcoded `LIMIT 5`, no override | Make it a bounded parameter (default 5, cap ~25) |
| `trigger_librarian` | Spawns subprocess before atomic lock check — redundant spawns under bursty writes | Acquire/release lock check before spawning, not after |
| `store_knowledge` title/content | Currently optional; weak models drop them even under explicit instruction | **Already planned:** make both mandatory + validated (non-empty) at the parameter level |
| `metadata` field | Free-form keys/values, no consistency enforcement — silent mismatch, not loud failure | Constrain or validate against a known set; don't leave fully open (see also project/context scoping, below) |
| No concurrency tests | Existing lock/isolation tests are single-threaded/sequential | Add real multi-process tests (spawn actual OS processes racing the lock and racing writes) |

---

## MODIFY (existing concept, different shape than current implementation)

| Item | Current shape | New shape |
|---|---|---|
| `owner_id` access control | Hard gate — private by default, isolates by exact string match | Demote to provenance-only metadata; access control should be relevance-based, not identity-based. Portability (same persona across tools/machines) makes exact-string identity matching fragile anyway |
| `session_id` / `project_id` | Not originally your idea — vibe-coded assumption of coding-project-shaped work | Replace with a domain-agnostic equivalent (e.g. generic `context_id` / registered `contexts` table) — same reliability benefit, no baked-in assumption about what's being organized |
| Consolidation (`commit_consolidation`) | Hard-deletes parent entities, silently cascades relation loss | **Archive, never delete** + auto-create `consolidated_from` relation from consolidated entity to every archived source, as part of the same operation (not an optional follow-up). Applies regardless of whether the source was `raw` or already `consolidated` — see supersession pipeline below |
| `relations` predicate design | Flat, undifferentiated predicates | Explicitly distinguish lineage/derivation edges (`derived_from`, `consolidated_from`) from general semantic edges (`caused_by`, `relates_to`) — enables clean ancestry-chain queries as consolidation depth grows (Spark-lineage-inspired) |
| Bulk operations | Single-item only; needed 7 sequential calls for one logical batch | **Already being implemented by hand** — verify single-transaction semantics + explicit, consistent partial-failure behavior across all bulk tools once pushed |
| Memory decay (`decay_lru_memories`) | Archives non-core memories after 90 days unaccessed, based purely on access recency | **Removed entirely.** Access-recency is not a proxy for value — penalizes rare-but-important memories (e.g. an infrequent but critical root-cause fact) over frequent-but-shallow ones. No replacement; only pressure-relief mechanism going forward is tag/consolidation clutter, which correlates with actual redundancy |

---

## NEW (not present in current version)

| Feature | What it does | Why |
|---|---|---|
| `include_related` search param | `search_memory` optionally pulls in higher-weight linked memories alongside direct hits (capped total) | Standalone traversal tool is rarely used; folding into search removes the extra decision point |
| `graph_exhausted` signal | Reports whether traversal hit a real dead end vs. was just depth-capped | Prevents wasted round-trips and prevents mistaking a truncated result for a complete one |
| Query normalization (server-side) | Strip filler/low-signal words from natural-language queries before hitting FTS5, or instruct via tool description | Cheaper fix for the "agent used full sentences first, then learned to use keywords" friction — may reduce need for semantic search on its own |
| Lineage/derivation-chain queries | Full ancestry traversal for multi-generation consolidation (not just immediate parent) | Needed once re-consolidation (storage-growth mitigation) becomes real; Spark-DAG-inspired |


