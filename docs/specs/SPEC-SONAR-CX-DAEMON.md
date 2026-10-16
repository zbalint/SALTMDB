# SPEC-SONAR-CX-DAEMON

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-CX-VIEWER-MCP is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the public functions and classes of `saltmdb.daemon.client`, `saltmdb.daemon.server`, `saltmdb.daemon.service` and `saltmdb.daemon.db_write_coordinator`. `_DaemonState._handle_session_method` is reached through `state.handle_request(build_request("hello", ...))` as in `tests/test_daemon_server.py` lines 57 to 111; `service.start_daemon`/`stop_daemon` through `tests/test_daemon_service.py`; `db_write_coordinator._run` through `tests/test_db_write_coordinator.py`.
- Scope (may edit), source:
  - `src/saltmdb/daemon/client.py`
  - `src/saltmdb/daemon/db_write_coordinator.py`
  - `src/saltmdb/daemon/server.py`
  - `src/saltmdb/daemon/service.py`
- Scope (may edit), tests: existing `tests/test_daemon_client.py`, `tests/test_daemon_server.py`, `tests/test_daemon_service.py`, `tests/test_db_write_coordinator.py`; new tests go in those files.
- Does not touch: everything else.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 7 functions above the cognitive-complexity limit of 15 in this group. The daemon is the only process that opens the database and its client authenticates every call, so a regression here can break every tool and hook; the characterization tests matter more than the refactor. `server.main` (308 lines) is NOT in this spec: it has no in-process test and gets its own spec. The fix is a behavior-preserving refactor: extract cohesive private helpers until each function is below the limit. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. Behavior is unchanged: outputs, exit codes, log text, error messages, SQL text, call order and side-effect order stay identical. Public names and signatures stay as they are.
- D2. Characterization tests come first. For each function in section 3, run the focused tests under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <tests>` then `coverage report --show-missing --include=<file>`; `coverage` is already a project dev dependency) and list the function's uncovered lines and branches. Write failing-or-passing characterization tests through the test seams for every uncovered branch whose behavior the refactor could change. Expected values are literals taken from running the unrefactored code, written down before the refactor. A branch that cannot be reached through a seam is listed in the report; then refactor it only by pure mechanical extraction (moving lines unchanged into a helper).
- D3. Extract private module-level helpers (or private methods on the same class) in the same file, with names that say what they do. No new modules, no new dependencies, no behavior flags. Do not "improve" logic while moving it.
- D4. Measure with the proxy in section 6: every function in section 3 and every new helper reaches `complexipy` score at most 12 (a margin below Sonar's 15, because the two tools differ by a few points). If the proxy and Sonar disagree after the architect's scan, a follow-up fix is assigned.
- D5. Remove a `# noqa: C901` / `# noqa: PLR09xx` marker only when the function no longer needs it (`.venv/bin/ruff check` passes without it). Do not add suppressions.
- D6. A function that cannot be brought under the limit with a safe, mechanical change is left as it is and reported with the reason.
- D7. Do not change authentication, token handling, socket or port behavior, locking or shutdown ordering. Keep every `logger` call, its level and its text identical. Daemon tests use the existing fixtures (temp directories, fake servers) and never touch the working daemon or the real `~/.saltmdb`.
- D8. Early-exit extraction: `client.open` and `client.call_method` contain `continue`/`raise` inside `except` blocks, under locks. Extracting such a block may use a small sentinel or tuple return that the caller turns back into the same `continue`/`raise`, with the lock still held by the caller and no reordering of statements. For these two functions the target is the proxy score at most 15 (not 12); every new helper still scores at most 12.
- D9. The remaining Sonar security findings on `daemon/client.py` and `daemon/service.py` are NOT part of this spec; they go to the owner as a report.

## 3. Changes per file

Line numbers and scores come from the Sonar analysis of commit `5a4e7ac` and may have drifted by a few lines; match by function name.

| File | Function | Line | Sonar score |
|---|---|---|---|
| `src/saltmdb/daemon/client.py` | `begin_lazy_start` | 160 | 20 |
| `src/saltmdb/daemon/client.py` | `call_method` | 557 | 36 |
| `src/saltmdb/daemon/client.py` | `open` | 747 | 36 |
| `src/saltmdb/daemon/db_write_coordinator.py` | `_run` | 242 | 16 |
| `src/saltmdb/daemon/server.py` | `_handle_session_method` | 345 | 27 |
| `src/saltmdb/daemon/service.py` | `start_daemon` | 216 | 24 |
| `src/saltmdb/daemon/service.py` | `stop_daemon` | 288 | 16 |

## 4. Tests

Per D2. New tests go in the existing test file for the module, or, when none exists, a new `test_<module>_characterization.py` next to the other tests of that area. Tests must not touch the real `~/.saltmdb` data or the working daemon: use temp directories and the existing fixtures/helpers.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`, plus the complexity proxy. The proxy lives outside the repo and must never be added to `pyproject.toml` or `uv.lock`:

```sh
SCRATCH=<a scratch directory you create under /tmp>; echo "$SCRATCH"
uv venv -q "$SCRATCH/cxvenv" && uv pip install -q --python "$SCRATCH/cxvenv/bin/python" complexipy
"$SCRATCH/cxvenv/bin/complexipy" --max-complexity-allowed 12 <files in scope>
```

It must report no function above 12 in the files in scope (functions outside section 3 that already exceeded 12 before your change are listed in the report as pre-existing; do not touch them). Run it once before editing to record the baseline. Delete only under the scratch directory afterwards: `rm -rf "${SCRATCH:?}/cxvenv"` after printing the resolved path.

## 7. Out of scope

Other Sonar rules, other files, any change of behavior, performance work, and these items: `server.main`, all Sonar security findings on these files, the S1172 findings left by SPEC-SONAR-PY-BEHAVIOR.

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy command in section 6 reports no function above 12 in the files in scope, except items listed under D6 in the report.
3. `git diff --stat` lists only files from section 0 scope.
4. The report lists, per function: before and after proxy score, the characterization tests added (or why none), and anything left under D6.
5. Mutation check for the new tests: for a sample of at least one behavior per refactored function, break it in the production file in the assigned checkout, show a new or existing test fails, restore it; the tree must match your own diff afterward.
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the S3776 keys for these functions gone.

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (function table, tests per function, coupling); see the assignment event.
