# SPEC-SONAR-PY-STYLE

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after the Sonar scope/fsspec commit; `./verify` exits 0 (about 2251 passed, 1 skipped).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the existing test suite. This spec changes no behavior, so it adds no tests.
- Scope (may edit): exactly these 20 files, and only at the lines in section 3 plus the new constant definitions or helper locals those fixes need:
  - `hooks/saltmdb-stop-trace-capture.py`
  - `src/saltmdb/daemon/server.py`
  - `src/saltmdb/daemon/service.py`
  - `src/saltmdb/db/connection.py`
  - `src/saltmdb/db/schema.py`
  - `src/saltmdb/domain/services/corpus_snapshot_service.py`
  - `src/saltmdb/domain/services/embedding_service.py`
  - `src/saltmdb/domain/services/memory_service/lifecycle.py`
  - `src/saltmdb/domain/services/memory_service/orchestrator.py`
  - `src/saltmdb/domain/services/memory_service/ranking.py`
  - `src/saltmdb/domain/services/memory_service/search_primitives.py`
  - `src/saltmdb/domain/services/memory_service/write.py`
  - `src/saltmdb/domain/services/relation_service.py`
  - `src/saltmdb/mcp/tools.py`
  - `src/saltmdb/utils/nlp.py`
  - `src/saltmdb/viewer/routes/base.py`
  - `src/saltmdb/viewer/routes/relations.py`
  - `src/saltmdb/viewer/routes/scatterplot.py`
  - `src/saltmdb/viewer/routes/search.py`
  - `src/saltmdb/viewer/server.py`
- Does not touch: anything else, including `tests/`, `docs/`, `scripts/benchmarking/`, `src/saltmdb/utils/process_memory.py`, and every other Sonar rule.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 35 low-risk Python style findings on `develop` across src/ and hooks/. They are behavior-preserving to fix. Fixing them first shrinks the issue list before the large complexity refactors, and keeps those refactor diffs clean. The owner's rule: never close or hide an issue; if a finding cannot be fixed with certainty, leave it and report it.

## 2. Decisions

- D1. Behavior is unchanged. No function signature, public name, log message text, SQL text, or returned value changes.
- D2. S1192 (duplicated literal): add a module-level private constant in the same file, named `_<MEANING>`, placed with the file's other module constants, and use it at every occurrence in that file. Do not create a shared constants module. SQL text and log format strings stay byte-identical.
- D3. S3358 (nested conditional expression): rewrite as an `if`/`elif`/`else` that assigns the same value. Evaluation order and short-circuiting stay identical.
- D4. S1515 (closure over loop variable): bind the variable as a default argument of the lambda, or use `functools.partial`, so the call-time value is unchanged. Check the lambda is not called after the variable is rebound; if the current behavior depends on late binding, leave it and report it.
- D5. S117 (naming): rename the local or nested-function parameter to snake_case. The `A` parameter at `scatterplot.py` line 37 belongs to the nested function `_gram_schmidt`; confirm with grep that no caller passes it by keyword.
- D6. S108 and S1186 (empty block or method): add a short comment saying why it is empty. Add no logic.
- D7. S7500, S1066, S1481: apply the exact change Sonar's message describes. For S1481 use `_` only where the value is truly unused. S7500: `dict(semantic_rows)` replaces the comprehension.
- D7a. S6353 (`orchestrator.py:35`, `_METADATA_FILTER_KEY_RE`): this regex is an allowlist for keys interpolated into SQL. In a Python 3 `str` pattern `\w` also matches non-ASCII letters, so a bare `\w` widens the allowlist. Write the fix as `\w` together with `re.ASCII` (compile flag), which is equivalent to `[A-Za-z0-9_]`. Do not change `.match` to `.fullmatch` (out of scope; reported to the owner).
- D7b. The two S7508 findings in `lineage_assembly_service.py` are NOT in scope: `_sort_ancestor_nodes` sorts three times on purpose and relies on sort stability for its tie-break order. They go to the owner as a report.
- D8. If any finding cannot be fixed with certainty under D1, do not fix it and do not suppress it (no `# noqa`, no `NOSONAR`). List it in the report with the reason.

## 3. Changes per file

Fix every finding in this table. Line numbers are from the Sonar analysis of `5a4e7ac`; they may drift slightly, so match by rule and message.

| Rule | File | Line | Sonar message |
|---|---|---|---|
| `S1192` | `hooks/saltmdb-stop-trace-capture.py` | 139 | Define a constant instead of duplicating this literal ".saltmdb" 3 times. |
| `S1192` | `src/saltmdb/daemon/server.py` | 299 | Define a constant instead of duplicating this literal "daemon is shutting down" 3 times. |
| `S1192` | `src/saltmdb/daemon/server.py` | 433 | Define a constant instead of duplicating this literal "database writer unavailable" 4 times. |
| `S3358` | `src/saltmdb/daemon/service.py` | 128 | Extract this nested conditional expression into an independent statement. |
| `S1192` | `src/saltmdb/db/connection.py` | 73 | Define a constant instead of duplicating this literal "PRAGMA busy_timeout=5000 |
| `S1192` | `src/saltmdb/db/connection.py` | 74 | Define a constant instead of duplicating this literal "PRAGMA foreign_keys=ON |
| `S1192` | `src/saltmdb/db/schema.py` | 1271 | Define a constant instead of duplicating this literal "PRAGMA user_version" 3 times. |
| `S1066` | `src/saltmdb/domain/services/corpus_snapshot_service.py` | 381 | Merge this if statement with the enclosing one. |
| `S1192` | `src/saltmdb/domain/services/embedding_service.py` | 119 | Define a constant instead of duplicating this literal "DELETE FROM retrieval_embeddings WHERE entity |
| `S1515` | `src/saltmdb/domain/services/embedding_service.py` | 338 | Add a parameter to the parent lambda function and use variable "after_id" as its default value |
| `S1515` | `src/saltmdb/domain/services/embedding_service.py` | 358 | Add a parameter to the parent lambda function and use variable "retrieval_after_id" as its default v |
| `S1192` | `src/saltmdb/domain/services/embedding_service.py` | 779 | Define a constant instead of duplicating this literal "BAAI/bge-small-en-v1.5" 3 times. |
| `S1192` | `src/saltmdb/domain/services/memory_service/lifecycle.py` | 140 | Define a constant instead of duplicating this literal "entity_id is mandatory." 4 times. |
| `S1192` | `src/saltmdb/domain/services/memory_service/lifecycle.py` | 797 | Define a constant instead of duplicating this literal "UPDATE entities SET last_accessed_at = ? WHER |
| `S1481` | `src/saltmdb/domain/services/memory_service/lifecycle.py` | 1321 | Replace the unused local variable "scope" with "_". |
| `S6353` | `src/saltmdb/domain/services/memory_service/orchestrator.py` | 35 | Use concise character class syntax '\w' instead of '[A-Za-z0-9_]'. |
| `S7500` | `src/saltmdb/domain/services/memory_service/ranking.py` | 659 | Replace this comprehension with passing the iterable to the dict constructor call |
| `S1192` | `src/saltmdb/domain/services/memory_service/search_primitives.py` | 236 | Define a constant instead of duplicating this literal " AND " 3 times. |
| `S1192` | `src/saltmdb/domain/services/memory_service/write.py` | 43 | Define a constant instead of duplicating this literal "#core" 3 times. |
| `S1515` | `src/saltmdb/domain/services/memory_service/write.py` | 56 | Add a parameter to the parent lambda function and use variable "submitted_key" as its default value |
| `S3358` | `src/saltmdb/domain/services/relation_service.py` | 221 | Extract this nested conditional expression into an independent statement. |
| `S3358` | `src/saltmdb/domain/services/relation_service.py` | 622 | Extract this nested conditional expression into an independent statement. |
| `S108` | `src/saltmdb/mcp/tools.py` | 228 | Either remove or fill this block of code. |
| `S1192` | `src/saltmdb/utils/nlp.py` | 242 | Define a constant instead of duplicating this literal r"\b\w+\b" 7 times. |
| `S1186` | `src/saltmdb/viewer/routes/base.py` | 35 | Add a nested comment explaining why this method is empty, or complete the implementation. |
| `S1192` | `src/saltmdb/viewer/routes/base.py` | 158 | Define a constant instead of duplicating this literal "/api/entities/" 3 times. |
| `S1192` | `src/saltmdb/viewer/routes/relations.py` | 53 | Define a constant instead of duplicating this literal "r.predicate = ?" 3 times. |
| `S1192` | `src/saltmdb/viewer/routes/relations.py` | 110 | Define a constant instead of duplicating this literal "SALTMDB Viewer handler error: %s" 4 times. |
| `S1192` | `src/saltmdb/viewer/routes/relations.py` | 111 | Define a constant instead of duplicating this literal "Internal server error. Check viewer logs for  |
| `S117` | `src/saltmdb/viewer/routes/scatterplot.py` | 37 | Rename this parameter "A" to match the regular expression ^[_a-z][a-z0-9_]*$. |
| `S117` | `src/saltmdb/viewer/routes/scatterplot.py` | 43 | Rename this local variable "Qo" to match the regular expression ^[_a-z][a-z0-9_]*$. |
| `S1481` | `src/saltmdb/viewer/routes/scatterplot.py` | 53 | Replace the unused local variable "n_samples" with "_". |
| `S117` | `src/saltmdb/viewer/routes/scatterplot.py` | 144 | Rename this local variable "X_centered" to match the regular expression ^[_a-z][a-z0-9_]*$. |
| `S1192` | `src/saltmdb/viewer/routes/search.py` | 49 | Define a constant instead of duplicating this literal "Hybrid search unavailable" 3 times. |
| `S1481` | `src/saltmdb/viewer/server.py` | 56 | Replace the unused local variable "exc_tb" with "_". |

## 4. Tests

No new tests: every change is behavior-preserving. The existing suite is the safety net. Run the tests that cover each touched module after each file, not only at the end.

## 5. Documentation

None.

## 6. Verification procedure

`./verify` only. Do not start parallel full runs.

## 7. Out of scope

All other Sonar rules (S3776, S1172, S3516, S107, security rules, JavaScript, scripts/diagnostics, tests). The `S101` class rename in `process_memory.py` (a Windows-only test imports the class; reported to the owner). Any refactor beyond the finding.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff --stat` lists only files from section 0 scope.
   (Pre-lock check of the claims behind D2, D3, D4, D5, D7: S1515 lambdas are called immediately by `DbWriteCoordinator.submit` or `max(key=)`, so default-argument binding is safe; the S3358 ternaries have no side effects; the S117 names are locals of the nested `_gram_schmidt`, called once with `A` positionally; the S1192 literals and counts match the code.)
3. `git diff` shows no change to any string literal's value, SQL text, or log format (only moves into constants).
4. The report lists any finding left unfixed, with the reason.
5. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the fixed keys gone.

## 9. Pre-lock gate notes

Line anchors, S1515 call timing, S3358 purity, S117 call sites, S1192 literals and counts, and the S6353 and S7508 findings were checked against `5a4e7ac` in a consultant review; S7508 was dropped and S6353 given `re.ASCII` as a result. Scope list matches the table files (20). Gate step 7: the 35-row count appears in section 1 and the table identically.
