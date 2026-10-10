# SPEC-DEDUP-CANDIDATES

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-018. Shared `context_id`: `perf-dedup-candidates-2026-10-10`.
- Baseline: the commit that adds this spec on `develop`, plus the `./verify` counts the developer records first.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `memory_service.check_duplicate_memories` (observed through the SQL statements executed on a
  wrapping connection and through `reranker_service.get_model` calls) and `search_primitives._run_fts_search`
  (the new keyword).
- Scope (may edit): `src/saltmdb/domain/services/memory_service/duplicates.py` (`check_duplicate_memories`, the
  pre-filter block about L72-103 and the two slices at L109 and L122), `src/saltmdb/domain/services/memory_service/search_primitives.py`
  (`_run_fts_search`, one keyword-only parameter), `src/saltmdb/config.py` (two constants, L125 region),
  `docs/architecture.md` (the one sentence at L84), `tests/test_cross_owner_dedup.py`,
  `tests/test_store_duplicate_policy_phase5.py`, `tests/test_relevance_gate.py` (one new test of the new keyword),
  and `tests/test_consolidation_is_core_inheritance.py` only if one of its tests fails because of this change.
- Does not touch: the cross-encoder threshold, the exact-duplicate hash guard, `write.py`, the daemon, scripts, any
  other test file. A test outside the list above that goes red is a BLOCKED, not a licence to edit it.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`store_memory` spends about 99% of its wall time (1.4 to 2.2 s, measured; the whole tool runs on the daemon's
single writer thread) in the duplicate check. Cause, in `check_duplicate_memories`:

1. The FTS pre-filter query is `sanitize_fts_query(title + content)`, an implicit AND of every word, so it
   matches nothing as soon as any word differs (11 of 11 realistic stores).
2. The code then falls back to `SELECT ... FROM entities WHERE status != 'archived' LIMIT 30` with no ORDER BY,
   which returns the 30 oldest rows, and the cross-encoder scores all 30 pairs (about 45 ms each).
3. Near-duplicates whose parent is not among those 30 oldest rows are never found (0 of 5 in the test; the
   existing near-duplicate tests only pass because their databases hold fewer than 30 rows).

Measured with the existing `search_primitives._run_fts_search` (quoted prefix terms, stop words dropped, bm25
order, AND retried as OR): the true parent ranked first among original memories in 12 of 12 near-duplicate
cases (one-word edits and ~30% word-change paraphrases), with the term cap at 20, 40 and 80. One FTS query
costs 8, 15 and 27 ms at those caps. The cross-encoder costs 41 to 45 ms per pair, so K=10 costs about 0.42 s
instead of 1.4 s. The tester's raw ranks went up to 9 when a title-suffixed sibling row shared the edited words,
so K=10 keeps those parents; K=5 would not. A flaw to avoid: `_run_fts_search` retries as OR only when the AND
query finds nothing; an AND hit on one unrelated superset row hid the true parent in the tester's first attempt,
so this check must use OR only.

Rejected (do not re-explore without new evidence): skipping only the cross-encoder on the arbitrary fallback
(both pinning near-duplicate tests find their duplicates through that fallback); a vector pre-filter (needs a
synchronous embedding of the new document on the writer thread, and breaks the zero-embed-call test); lowering
the cross-encoder threshold or changing it (a separate calibration item).

## 2. Decisions

- D1. `_run_fts_search` gets a new keyword-only parameter `or_only: bool = False`. When true, the AND attempt is
  skipped and the OR-joined query is executed directly (a single term is executed as it is, quoted). Default
  behaviour and the return shape stay unchanged for every existing caller.
- D2. `check_duplicate_memories` replaces its FTS block with one call to `search_primitives._run_fts_search(conn,
  <capped terms>, fts_where_clauses, params, DEDUP_CROSS_ENCODER_MAX_CANDIDATES, 0, or_only=True)`.
  Terms: `sanitize_fts_query(input_text).split()`, stop words (`search_primitives.STOP_WORDS`) removed
  case-insensitively, de-duplicated case-insensitively keeping first occurrence (title words come first because
  `input_text` is title then content), and the first `DEDUP_FTS_MAX_TERMS` kept. If that leaves no terms, use the
  unfiltered split (same rule `_run_fts_search` applies). The rows are mapped to the existing 5-tuple
  `(id, title, full_content, agent_id, scope)` using row indexes 0, 1, 2, 8, 9 (the `_run_fts_search` column order).
  `_run_fts_search` takes the query as ONE STRING and splits it itself, so pass `" ".join(capped_terms)`, never a list
  (a list raises AttributeError, which the outer `except Exception` turns into an `{"error": ...}` result).
- D3. The arbitrary `LIMIT 30` scan of `entities` runs only when the FTS call raised `sqlite3.Error` (FTS unavailable,
  the already-logged degraded path). It no longer runs when FTS simply finds nothing; then there are no candidates
  and no cross-encoder call.
- D3a. When the candidate list is empty (FTS ran without error and found nothing), `check_duplicate_memories` returns
  `{"duplicate_found": False, "potential_duplicates": []}` immediately, before the cross-encoder block; the model is not
  loaded, `rerank` is not called and the lexical/semantic `except` fallback does not run. (Today the block runs
  `get_model` and `rerank(q, [])` unconditionally, `duplicates.py` L104-111.)
- D4. `DEDUP_CROSS_ENCODER_MAX_CANDIDATES` becomes 10 and its stale trailing comment is replaced. New constant
  `DEDUP_FTS_MAX_TERMS = 40` with a comment citing the measurement (8/15/27 ms at 20/40/80 terms, parent rank
  unchanged).
- D5. `docs/architecture.md` L84: change "roughly 30 candidates" to a bm25-ordered top 10 from FTS5, and say that
  when FTS finds nothing no cross-encoder call is made.
- D6. No other behaviour changes: the cross-encoder threshold, text caps, the exception fallback to the lexical and
  semantic logic, and the result shape stay as they are.

## 3. Changes per file

### 3.1 `src/saltmdb/domain/services/memory_service/search_primitives.py`

In `_run_fts_search` add `or_only: bool = False` after `return_fallback_flag`. After `fts_query_str` is built, when
`or_only` is true execute `" OR ".join(f'"{t}"*' for t in terms)` first (same SQL, same params) instead of the AND
string, and do not run the retry block; `used_or_fallback` is then `len(terms) > 1 and bool(rows)`. Update the
docstring in one sentence.

### 3.2 `src/saltmdb/domain/services/memory_service/duplicates.py`

Replace the FTS block (the `fts_candidates` assignment through the `LIMIT 30` fallback) as in D2 and D3. Import the
new config constant. Keep the `except sqlite3.Error` warning text. Add the empty-candidates early return of D3a directly after
the candidate list is built. Do not touch the cross-encoder or lexical branch otherwise; the candidate slicing keeps
using `DEDUP_CROSS_ENCODER_MAX_CANDIDATES`.

### 3.3 `src/saltmdb/config.py`, 3.4 `docs/architecture.md`

D4 and D5.

## 4. Tests

Written first; red against the baseline for the right reason.

- `tests/test_cross_owner_dedup.py::test_dedup_check_fts_fallback_scan_is_bounded` (L216-260) pins the old
  behaviour. Rewrite it as `test_dedup_check_without_fts_hits_runs_no_scan_and_no_cross_encoder`: with 35 stored
  entities and the unmatched query, assert that no executed SQL contains both `FROM entities` and `LIMIT 30`, that
  `reranker_service.get_model` is not called (patch it), and that the result has no error and no duplicates.
- Add `test_dedup_check_fts_error_keeps_bounded_scan`: wrap the connection so the FTS statement raises
  `sqlite3.OperationalError`, and assert the bounded `LIMIT 30` scan statement still runs.
- Add `test_dedup_check_finds_parent_beyond_first_thirty_rows`: store 40 unrelated entities first, then a distinctive
  parent, then call `check_duplicate_memories` with the parent's text plus one changed word; assert the parent's
  id is in the duplicate candidates (patch `reranker_service.get_model` with a fake model whose `rerank` returns 8.0
  for the parent text and 0.0 otherwise, so no real model is needed). Red on the baseline.
- Add `test_dedup_check_candidate_cap_is_ten`: with 15 entities sharing a term, assert the fake model receives
  exactly `DEDUP_CROSS_ENCODER_MAX_CANDIDATES` (10) texts.
- `tests/test_relevance_gate.py`: add one test of `_run_fts_search(..., or_only=True)` on a table where the AND query
  would hit exactly one row but the OR query hits three: assert all three rows are returned, `used_or_fallback`
  is true, and that the default call (no keyword) still returns only the one AND row.
- Fixture rules for every new test that stores entities through `store_memory`: each store runs the duplicate check
  itself, so patch `reranker_service.get_model` (fake model, finite float per text) for the whole test including the
  setup stores, otherwise the real model loads; assert on the LAST `rerank` call, not the first. Wrap the setup in
  `patch.object(memory_service._embed_pool, "submit", return_value=None)` as `test_dedup_check_no_per_row_embedding_loop`
  does, so background embedding jobs do not run the real embedding model. The FTS error test matches the statement
  containing `entities_fts MATCH`. "Beyond the first thirty rows" relies on the unordered `LIMIT 30` returning rowid
  order on the baseline; if the baseline unexpectedly finds the parent, say so in the report instead of changing the test.
- Known disposition, `tests/test_cross_owner_dedup.py::test_dedup_check_mixed_embedding_readiness` (L136-215): today
  candidate C (dissimilar text, shares only SALTMDB/Database/System) is never scored because the AND query does not match
  it; with the OR top-10 it is scored by the real cross-encoder. If C appears in `potential_duplicates` with a score at
  or above the threshold, do NOT edit the test silently: print the real scores of A, B and C in the report, and replace
  only the C assertion with `assertLess(<C score>, min(<A score>, <B score>))` (C kept below the true duplicates), plus a
  comment that precision against topical neighbours is a threshold-calibration item. If C is absent, no edit.
- `tests/test_store_duplicate_policy_phase5.py::test_near_duplicate_stores_and_returns_inline_candidates_and_guidance`
  and the other tests in `tests/test_cross_owner_dedup.py` should stay green unchanged because their texts share
  many terms; if one goes red, report the exact failing assertion before editing it.

## 5. Out of scope

Changing the cross-encoder threshold (the tester saw 3 to 6 topical neighbours score at least 4.0 for a new memory
on a synthetic corpus; calibration needs the real database and is a separate item), moving the duplicate check
off the writer thread (BL-023), a vector pre-filter, caching.

## 6. Acceptance

1. Before editing: `./verify`; record counts and any pre-existing failure.
2. Tests written first: `.venv/bin/python -m pytest tests/test_cross_owner_dedup.py tests/test_relevance_gate.py tests/test_store_duplicate_policy_phase5.py -q`
   shows red only on the rewritten and new tests, with the reasons above.
3. After implementing: the same command exits 0, and
   `.venv/bin/python -m pytest tests/test_consolidation_is_core_inheritance.py tests/test_search_scores.py tests/test_bitemporal_relations.py tests/test_advanced_quality_features.py -q`
   exits 0 (the last two hold NEAR_DUPLICATE expectations: `test_bitemporal_relations.py` about L525-548 and
   `test_advanced_quality_features.py` about L50-71).
4. Timing probe on a throwaway temp database (temp HOME, `SALTMDB_DB_PATH` in the temp dir, `SALTMDB_VIEWER_ENABLED=false`,
   `SALTMDB_DISABLE_LIBRARIAN=1`, repo `.venv`, never the live daemon): store 40 distinct memories of about 150 words,
   then time `check_duplicate_memories` for one new 150-word text; report the wall time and the number of texts the
   cross-encoder received. On the baseline code it receives 30 and takes about 1.4 s; after the change it must
   receive at most 10 (and 0 for a text sharing no term).
5. `./verify` exits 0. One full run at a time.
6. `git status --short` lists only files named in section 0 (the architect commits the spec before the assignment, and
   the tree is otherwise clean when you start).

## 7. Pre-lock gate notes

- Callers of `_run_fts_search` outside the file: production (`orchestrator.py`, `ranking.py` through the package),
  benchmark scripts and many tests that patch it; the new keyword is optional and defaulted, so none changes.
  Tests that call `check_duplicate_memories`: `tests/test_cross_owner_dedup.py`, `tests/test_consolidation_is_core_inheritance.py`
  (wraps the real function with a racing side effect, so the real check runs under the new logic),
  `tests/test_bitemporal_relations.py`, `tests/test_advanced_quality_features.py`, and the store path in `tests/test_store_duplicate_policy_phase5.py`.
- Content search of the old text `LIMIT 30`: `tests/test_cross_owner_dedup.py` L217-257 (rewritten), `duplicates.py`
  L87 and L100 (changed), `docs/architecture.md` L84 (changed). The constant `DEDUP_CROSS_ENCODER_MAX_CANDIDATES`
  is used only in `duplicates.py` and `config.py`.
- The row index mapping (0, 1, 2, 8, 9) is taken from the SELECT list in `_run_fts_search`: `e.id, e.title,
  e.full_content, e.weight, e.is_core, rank_score, e.created_at, e.updated_at, e.agent_id, e.scope, ...`.
- Consultant pre-lock review done; it found the unconditional cross-encoder block on an empty candidate list (D3a), the
  risky mixed-readiness test (disposition above), two unlisted NEAR_DUPLICATE tests, the string-argument rule, and test
  fixture traps; all amended here.
- Known limit: `_run_fts_search` orders by `bm25 * weight`, so a high-weight unrelated row can rank into the top 10; the
  tester's corpus used default weights, so the 12/12 rank result does not cover that case. Accepted.
