# SPEC-SONAR-CX-SEARCH-EQUIV-GATE

## 0. Status

LOCKED 2026-10-19

- Backlog item: none (gate for the Sonar cleanup of `search_memory`, SPEC-SONAR-CX-SEARCH-2 and -3). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.
- Baseline: `develop` at `20f6b44` (SEARCH-1 committed); `./verify` exits 0.
- Test seams: `saltmdb.domain.services.memory_service.search_memory` called in-process with `db_path` set to a database copy, exactly as `scripts/benchmarking/benchmark_search_option_matrix.py` does.
- Scope (may edit): two new files, `scripts/benchmarking/search_equivalence_gate.py` (runner) and `scripts/benchmarking/compare_search_equivalence.py` (comparer), and one new test file `tests/test_search_equivalence_gate.py` (synthetic fixtures only). Nothing else.
- Does not touch: every file under `src/`, every existing benchmark script, `scripts/benchmarking/golden_queries*.json`, `.gitignore`, every other test file.

## 1. Why

SPEC-SONAR-CX-SEARCH-2 and -3 split the 1,000-line `search_memory`. The search ranking was tuned and benchmarked over weeks. The owner requires that search results stay exactly the same after the refactor. Unit characterization tests (SEARCH-1, 109 tests) pin behaviour on tiny synthetic fixtures. They cannot show that real embeddings, a real cross-encoder and a real corpus give the same ordering, scores, previews and cursors. The existing benchmark scripts compare coarse metrics (top-1, MRR, latency) and are not proven to run today, so a regression that moves rank 7 to rank 8 passes them. This spec builds an exact-equality gate: run the same queries through the same snapshot before and after, and fail on any difference in the full result lists.

## 2. Decisions

- D1. The gate compares complete outputs, not metrics: for every case, the JSON of the full `search_memory` return value (ids, order, scores, titles, previews, snippets, cursors, related items, diagnostics, and the type and message of any exception) must be identical between a baseline run and a candidate run, including dict key order inside result items (compare the serialized text, no global `sort_keys`). `search_memory` returns no timings or timestamps, so the volatile-key allowlist starts EMPTY. Runner-measured latency is stored in a separate envelope that is never compared. A key may be added to the allowlist only with evidence from D4 of what makes it volatile; nothing is excluded to make a diff go away. One known exception to key order: `diagnostics.retrieval_text.candidate_evidence` is a dict built by iterating a set of string ids, so its key order depends on `PYTHONHASHSEED`; the runner refuses to start unless `PYTHONHASHSEED` is set in its environment (the architect uses `0`) and records it, and the comparer fails if the two headers differ in it.
- D2. Inputs are read from paths passed on the command line. Query files are memory-derived and gitignored (`scratch/`); the repo contains no query text and no output. Outputs are written only below a directory the caller passes and refuse to write inside a tracked path (`git check-ignore` or a `scratch/` / system-temp prefix check).
- D3. The database is a copy. The runner refuses the live default path (the `get_db_path()` resolution and anything under `~/.saltmdb`), refuses a path that is not a regular file OR does not end in `.db`, and never opens the file it is given: the caller's snapshot is hashed (SHA-256 recorded in the header as `input_sha256`), then copied to a FIXED file name `gate.db` inside the output directory, and only that copy is opened. Each run makes its own fresh copy (opening a database runs `PRAGMA journal_mode=WAL`, which rewrites the header of the copy, so the copy's hash is not comparable; the input hash is). A fixed copy name keeps path text in error strings identical between runs. The architect produces the snapshot with `saltmdb-cli snapshot --dest-dir <dir>` (daemon-owned, sanctioned); the developer and tester never open the live database. For the developer's and tester's own trial runs the architect provides a snapshot copy at a path named in the assignment.
- D4. Determinism first, then switching. Before the gate is used on any refactor: (a) run the runner twice on the same unchanged tree and snapshot: the comparer must report zero differences. (b) Run it on `HEAD` from a `git worktree` (D7 mechanism) and on `HEAD` in the main checkout: zero differences. This proves the switching mechanism, which (a) cannot. If a field differs on identical code, find the cause; add it to the D1 allowlist only with evidence, otherwise the harness is broken and is fixed. If float drift between processes appears, pin thread counts (`OMP_NUM_THREADS`, `ORT` intra-op threads) in the runner and run with no other load; do not allowlist it. Run baseline and candidate back to back (relation validity checks use `datetime('now')`).
- D5. The matrix is fixed in the runner as data, so baseline and candidate cannot diverge by configuration. Every `search_memory` argument is passed explicitly in every case (defaults are `limit=5`, `include_related=True`), and the runner checks each case's keyword names against `inspect.signature(search_memory)` before the run starts and aborts on an unknown name. Cases are keyed by `(file, index, config_name)`, never by query text (one text occurs twice). Cross-encoder on/off is NOT an argument: it is the environment variable `SALTMDB_RERANKER_MODEL` (read on each call; unset means disabled), set per configuration and recorded in the case key. `use_cross_encoder` and `rerank_by_topic` are not parameters of `search_memory` and must not be passed.
  - Tier A, every query of every supplied query file (389 entries) against six configurations: `mode` broad / strict / history with defaults; broad with CE on; strict with CE on; broad with `prefer_durable_types=True, demote_superseded=True` and CE off. `limit=10`, `include_related=False`.
  - Tier B, the queries at index positions 0, 9, 18, ... of the concatenated list (files in sorted-name order, entries in file order; 44 queries), against the DEDUPLICATED option matrix: the distinct argument combinations among {broad, history, strict} x {`prefer_durable_types`, `demote_superseded`} (strict forces both on, so one strict row), each with CE off and on, plus each of these alone on broad with defaults: `collapse_supersedes_families=True`, `include_related=True`, `explain_mode=True`, `return_diagnostics=True` (also under strict and history), `use_chunk_candidates=True` (with `oversampling_multiplier`, `candidate_window`, `chunk_weight` set to non-default valid values in a second case), `use_retrieval_text_candidates=True` (with `retrieval_fts_weight` and `retrieval_vector_weight` non-default in a second case; includes `return_diagnostics=True`), `cross_encoder_candidate_cap` and `cross_encoder_text_cap_chars` non-default with CE on, `disable_semantic=True`, an invalid cap (records the error), `collapse_supersedes_families=True` with strict (records the raise).
  - Tier C, ten queries (every 39th entry), fixed offsets instead of paging to exhaustion: broad and strict, `limit=3`, cursors `offset:0`, `offset:3`, ... `offset:30`, then `offset:57`, `offset:99`, `offset:198`, `offset:201`; a malformed cursor (`offset:x`) and a cursor without the `offset:` prefix; the cursor values are written by the runner, not taken from earlier output, so that a changed cursor encoding is itself caught by the compared `next_cursor` fields. Filter probes on the same ten queries and on the browse path (no `query_keywords`, which includes ties on `updated_at`): `tags_filter` AND and OR using a tag of the top result, a nonexistent tag under AND and OR, an alias or canonical-form tag if the corpus has one, `memory_type_filter`, `agent_id`, `context_id`, `agent_session_id`, `metadata_filter` valid and with an invalid key, `is_core`.
  - Exact-title fast path: for each of the ten Tier C queries' top result, a query equal to its `title` (the path at `orchestrator.py` near line 337), run with broad, history, strict, `collapse_supersedes_families=True`, `limit=0`, and a cursor with offset greater than 0.
  - Environment cases: `SALTMDB_RERANKER_MODEL` unset with `return_diagnostics=True` (reports the reason the cross-encoder did not run), and `SALTMDB_ENABLE_SEMANTIC=false` with a query (records the error).
  - A per-call exception is recorded (type and message), not raised, so baseline and candidate must also agree on errors. The validation `ValueError`s raised before the function's own try block are among them.
- D6. The cross-encoder and the embedding model run for real, and the run must prove they did: the header records the value of every `SALTMDB_*`, `OMP_*` and `ORT_*` environment variable, `os.cpu_count()`, the package versions of `fastembed`, `onnxruntime`, `sqlite-vec`, and the SHA-256 of the bundled model files under the directory of the imported package (the loaders find them relative to `__file__`). Liveness checks run before the cases, and the run aborts if any fails: `reranker_service.score_pairs_preflight` returns finite and correctly sized scores; and, checked on the recorded outputs after the run, at least one CE-on case has `diagnostics.cross_encoder.executed` true, at least one case returned a `relevance_preview`, and at least one candidate has `in_semantic` true. (Both the cross-encoder and the preview code fall back silently on a load failure, so without this a broken environment would make both runs equally blind.)
- D7. Runner and comparer are separate processes. ONE runner file (the main checkout's, which is uncommitted and so absent from older commits) serves both runs. The baseline run uses a `git worktree add <dir> <baseline sha>` under `$TMPDIR`, removed afterwards (`git stash` is not allowed), with `PYTHONPATH=<dir>/src`; the candidate run uses the main checkout's `src`. The `.venv` has an editable install pointing at the main checkout; `PYTHONPATH` entries precede it, but only if the interpreter is not started with `-I` or `-E`, so the runner aborts when `sys.flags.isolated` or `sys.flags.ignore_environment` is set. The header records: `os.path.realpath(saltmdb.__file__)`; `git -C <that dir> rev-parse HEAD`; `git status --porcelain` of that tree (empty for the baseline run); the runner file's SHA-256; the input snapshot SHA-256; and a required `--expect-src <dir>` argument that the runner compares to the realpath and aborts on mismatch. The comparer prints the first differing path for each differing case, a summary count, and exits non-zero on any difference, any missing or extra case, or any header mismatch other than the expected `src` and `HEAD` fields (the runner file hash, snapshot hash, `PYTHONHASHSEED`, environment, versions and model hashes must match).
- D8. Sensitivity of the harness itself must be proven. `tests/test_search_equivalence_gate.py` loads the two scripts by path with `importlib` (`scripts/benchmarking` is not a package) and tests, from JSON fixtures only (no database, no network, no models): identical outputs compare equal; each of these single-cause changes to one output is reported as a difference by the comparer: two ids swapped, a score changed in the 9th decimal, one preview character, a cursor, a diagnostics key removed, a result added, an error message changed, a key reordered inside one result item, a header field changed (input hash, runner hash, `PYTHONHASHSEED`, a model hash); a key placed on the allowlist (a synthetic key used only by the test) changing is not a difference while the same change to any other key is; a missing and an extra case are reported. The runner's own checks get tests with a tiny temporary database or none: it refuses the live default path, a non-`.db` path and a directory; it aborts on an unknown keyword name in a case; it aborts under `-I`; the liveness failure path is exercised with a stubbed failing preflight.
- D9. No new Sonar findings in the new files (owner rule D13 of the cleanup: no unused parameters or locals, no nested conditional expressions, no `# noqa` to hide findings); `ruff check`, `ruff format --check` and `mypy` pass on the new files as `./verify` runs them; the scripts are not collected as tests and are not imported by `src/`.
- D10. Time budget: Tier A is about 2,300 calls and Tier B and C a few thousand more. The runner first times a sample of 20 calls per tier and prints the extrapolated total; the architect decides whether to run the full set or to trim Tier B queries before the real run. A run finishing in under about 120 minutes on this host with one process is expected, and each run is done twice per refactor (baseline and candidate). The runner prints progress per tier and resumes a partial run from its own output directory (completed cases are skipped on resume only if the header matches). No parallel test suites run while the gate runs.

## 3. Changes per file

| File | Change |
|---|---|
| `scripts/benchmarking/search_equivalence_gate.py` | new runner per D1 to D7, D10 |
| `scripts/benchmarking/compare_search_equivalence.py` | new comparer per D1, D7 |
| `tests/test_search_equivalence_gate.py` | new synthetic tests per D8 |

## 4. Tests

D8 only. The real-corpus run is an acceptance step, not a repo test.

## 5. Documentation

A module docstring in the runner with usage, the allowlist and the matrix. No other docs.

## 6. Verification procedure

`./verify`. The real-corpus run (Acceptance 3 and 4) is separate and is executed by the architect.

## 7. Out of scope

Any change to `search_memory` or other production code; accuracy metrics or judging; updating the older benchmark scripts; building query sets; committing any query file, snapshot or output.

## 8. Acceptance

1. `./verify` exits 0 (judge: architect, fresh run).
2. `git status --short` shows only the three new files (this spec is committed beforehand); no file under `scratch/` or `reports/` is staged (judge: architect).
3. Determinism and switching (D4 a and b) on a snapshot copy made by the architect with `saltmdb-cli snapshot --dest-dir`: zero differences, allowlist still empty or each addition justified, liveness checks passed (judge: architect; the developer first runs the same on a small query subset against the snapshot path the architect names).
4. Sensitivity (D8): the listed single-cause mutants are each reported as a difference by a named test; the architect additionally mutates the comparer (three mutants of its own choosing) and shows a named test fails (judge: architect, tester).
5. Tester pass: independent read of the runner for configurations that can differ between baseline and candidate runs by accident (ordering of dict keys, set iteration, timestamp leaks, environment variables, cache state, `PYTHONHASHSEED`), and an independent determinism run (judge: tester).
6. Both scripts refuse the live default database path and a non-`.db` path; a test per refusal (judge: `tests/test_search_equivalence_gate.py`).

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (read-only); its findings (flags that are not parameters, silent fallbacks, runner absent from the baseline commit, WAL header rewrite, empty allowlist, hash-order dict, worktree import proof, Tier C cost, missing coverage) are folded into D1 to D8 and D10.

Claims checked:

| Claim | Where | Verified |
|---|---|---|
| Query file shapes and counts (49, 49, 2, 60, 45, 100, 60, 15, 9 = 389; one duplicated text) | files under `scratch/fresh-retrieval-benchmark-20260831/`, `scratch/next-retrieval-benchmark-20260831/`, `scripts/benchmarking/golden_queries*.json` | probed by architect and by consultant |
| `search_memory` parameters; defaults `limit=5`, `include_related=True`; no `use_cross_encoder` / `rerank_by_topic` parameter; cross-encoder switched by env `SALTMDB_RERANKER_MODEL` | `orchestrator.py:39-70`, `config.py:630-637` | read by architect and consultant |
| Search path writes nothing to the database | `orchestrator.py`, `search_primitives.py`, `ranking.py`, `validation.py` (no UPDATE/INSERT/commit) | consultant grep |
| Opening a connection runs `PRAGMA journal_mode=WAL` | `db/connection.py:110` | consultant read |
| Result items and diagnostics carry no timestamps or durations | `orchestrator.py:158-188, 731-748, 989-1039`, `reranker_service.py:217-275` | consultant read |
| `candidate_evidence` key order depends on the hash seed | `orchestrator.py:456-479` | consultant read (not run) |
| Editable install points at the main checkout; `PYTHONPATH` precedes it unless `-I`/`-E` | `.venv/.../__editable__.saltmdb-0.1.0a104.pth` | consultant read; precedence is standard CPython (not run) |
| Bundled models are tracked plain files and are found relative to `__file__` | `git ls-files`, `reranker_service.py:40-50` | consultant |
| Snapshot via `saltmdb-cli snapshot --dest-dir` goes through the daemon | `cli.py:136-151, 599-606` | read; used earlier in this cleanup |
| `scratch/` and `reports/` are gitignored | `.gitignore:20, 43` | `git check-ignore -v` |
| Old option-matrix script keeps `use_cross_encoder` / `rerank_by_topic` only as labels | `benchmark_search_option_matrix.py:208-224` | consultant read |

## Amendment 1 (2026-10-19): header provenance of the imported tree

Developer BLOCKED on D7 (confirmed): `git status --porcelain` run in the imported tree reports repository-wide state, so in the main checkout it lists the untracked gate files and, during a refactor, the candidate's own changes, while the baseline worktree is clean. Requiring it to match is impossible for any real candidate, and D4b (main checkout vs worktree at the same HEAD) would never pass.

- A1. The header records, for the imported tree, `src_realpath`, `src_head` (`git rev-parse HEAD`), `src_status_full` (`git status --porcelain` of the whole tree) and `src_status_src` (`git status --porcelain -- src` of the imported tree). All four are provenance: the comparer prints them and never compares them between the two runs. They replace the "expected `src` and `HEAD` fields" exception of D7 (the header-mismatch rule keeps every other field).
- A2. The comparer takes `--baseline <file>` and `--candidate <file>`. It fails unless the BASELINE header has `src_status_full` empty and its `src_head` equals the sha the caller passes as `--baseline-sha` (so a dirty or wrong baseline is rejected). For the candidate it only requires `src_realpath` to equal the run's `--expect-src`; a candidate whose `src_status_src` is empty and whose `src_head` equals the baseline sha is allowed (that is the D4 identical-code case) and is reported as `candidate tree is unchanged from baseline` in the summary line, so an accidentally unchanged candidate in a refactor run is visible.
- A3. D8 gains tests: baseline header with a dirty `src_status_full` is rejected; wrong `--baseline-sha` is rejected; candidate headers differing in the four provenance fields compare equal on every other field; a candidate whose `src_realpath` differs from the baseline in a non-provenance way is not special-cased.

## Amendment 2 (2026-10-19): the baseline cleanliness test is scoped to `src`

Developer report: with Amendment 1, the D4a determinism run in the main checkout fails the comparer, because the baseline header's `src_status_full` lists the three untracked gate files that live in that checkout (outside `src`). Amendment 1 A2 contradicted D4a; the architect did not walk D4a through A2 when writing it (cause of the second adjudication round).

- A5. Replaces the baseline cleanliness requirement of Amendment 1 A2: the comparer requires the BASELINE header's `src_status_src` (`git status --porcelain -- src` of the imported tree) to be empty and its `src_head` to equal `--baseline-sha`. `src_status_full` is provenance only for both runs (printed, never compared, never required empty). A baseline with any change under `src` is rejected; untracked files elsewhere (the gate scripts, scratch output) do not matter. Everything else in Amendment 1 stands, including the candidate rules and the unchanged-candidate message.
- A6. D8 test change: replace "baseline header with a dirty `src_status_full` is rejected" with two tests: a baseline with a non-empty `src_status_src` is rejected; a baseline with a non-empty `src_status_full` and an empty `src_status_src` is accepted.
