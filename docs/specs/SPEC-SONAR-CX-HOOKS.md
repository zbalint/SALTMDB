# SPEC-SONAR-CX-HOOKS

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-VIEWER-JS is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: in-process for `_claim_ready` (`test_digest_retry.py`), `bootstrap_text` and bootstrap `main` (`test_session_start_bootstrap.py`), and `parse_interactions` (`test_trace_capture_copilot.py`); for the critique gate `main` and related-memories `main` the existing tests (`test_stop_related_memories.py`, class `GATE` for the gate and `run_hook` for related memories) run the hook as a subprocess with a minimal environment, which `coverage` cannot see.
- Scope (may edit), source:
  - `hooks/saltmdb-prompt-submit-digest-retry.py`
  - `hooks/saltmdb-session-start-bootstrap.py`
  - `hooks/saltmdb-stop-critique-gate.py`
  - `hooks/saltmdb-stop-related-memories.py`
  - `hooks/saltmdb-stop-trace-capture.py`
- Scope (may edit), tests: `hooks/tests/` (existing files `test_session_start_bootstrap.py`, `test_digest_retry.py`, `test_stop_related_memories.py`, `test_trace_capture_copilot.py`, and a new `hooks/tests/test_stop_critique_gate.py` for in-process tests of the critique gate).
- Does not touch: everything else.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 6 functions above the cognitive-complexity limit of 15 in this group. The hooks are standalone scripts that agents run on every session event, so a regression here silently disables safeguards; that is why the characterization tests matter more than the refactor itself. The fix is a behavior-preserving refactor: extract cohesive private helpers until each function is below the limit. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. Behavior is unchanged: outputs, exit codes, log text, error messages, SQL text, call order and side-effect order stay identical. Public names and signatures stay as they are.
- D2. Characterization tests come first. For each function in section 3, run the focused tests under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <tests>` then `coverage report --show-missing --include=<file>`; `coverage` is already a project dev dependency and measures in-process tests only) and list the function's uncovered lines and branches. Write failing-or-passing characterization tests through the test seams for every uncovered branch whose behavior the refactor could change. Expected values are literals taken from running the unrefactored code, written down before the refactor. A branch that cannot be reached through a seam is listed in the report; then refactor it only by pure mechanical extraction (moving lines unchanged into a helper). The two functions whose existing tests run as subprocesses (critique gate `main`, related-memories `main`) cannot be measured with `coverage`: for them, first write the branch inventory by reading the code against the existing subprocess tests, then add in-process characterization tests (load the hook with `importlib`, patch `sys.stdin` and `HOME`, catch `SystemExit`; `main` of the critique gate calls `sys.exit(0)` on eight paths, and `random` only seeds the cap, so pre-seed the `.session_cap` file as `test_stop_related_memories.py` does around line 528) and measure coverage on those in-process tests.
- D3. Extract private module-level helpers (or private methods on the same class) in the same file, with names that say what they do. No new modules, no new dependencies, no behavior flags. Do not "improve" logic while moving it.
- D4. Measure with the proxy in section 6: every function in section 3 and every new helper reaches `complexipy` score at most 12 (a margin below Sonar's 15, because the two tools differ by a few points). If the proxy and Sonar disagree after the architect's scan, a follow-up fix is assigned.
- D5. Remove a `# noqa: C901` / `# noqa: PLR09xx` marker only when the function no longer needs it (`.venv/bin/ruff check` passes without it). Do not add suppressions.
- D6. A function that cannot be brought under the limit with a safe, mechanical change is left as it is and reported with the reason.
- D7. Hooks are executed as scripts. Do not add imports beyond `hooks/_saltmdb_hook_common.py` and the standard library, and keep each hook's stdin/stdout/exit-code contract byte-identical.

## 3. Changes per file

| File | Function | Line | Sonar score |
|---|---|---|---|
| `hooks/saltmdb-prompt-submit-digest-retry.py` | `_claim_ready` | 71 | 20 |
| `hooks/saltmdb-session-start-bootstrap.py` | `bootstrap_text` | 54 | 16 |
| `hooks/saltmdb-session-start-bootstrap.py` | `main` | 139 | 16 |
| `hooks/saltmdb-stop-critique-gate.py` | `main` | 147 | 27 |
| `hooks/saltmdb-stop-related-memories.py` | `main` | 207 | 23 |
| `hooks/saltmdb-stop-trace-capture.py` | `parse_interactions` | 67 | 24 |

## 4. Tests

Per D2. New tests go in the existing test file for the module, or, when none exists, a new `test_<module>_characterization.py` next to the other tests of that area. Tests must not touch the real `~/.saltmdb` data or the working daemon: use temp directories and the existing fixtures/helpers.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`, plus the complexity proxy. The proxy lives outside the repo and must never be added to `pyproject.toml` or `uv.lock`:

```sh
SCRATCH=<a scratch directory you create under /tmp>; echo "$SCRATCH"
uv venv -q "$SCRATCH/cxvenv" && uv pip install -q --python "$SCRATCH/cxvenv/bin/python" complexipy==8.0.1
"$SCRATCH/cxvenv/bin/complexipy" --max-complexity-allowed 12 <files in scope>
```

It must report no function above 12 in the files in scope (functions outside section 3 that already exceeded 12 before your change are listed in the report as pre-existing; do not touch them). Run it once before editing to record the baseline (the PY-STYLE commit already changed `hooks/saltmdb-stop-trace-capture.py`, so take the baseline from the tree you start from). Delete only under the scratch directory afterwards: `rm -rf "${SCRATCH:?}/cxvenv"` after printing the resolved path.

## 7. Out of scope

Other Sonar rules, other files, any change of behavior, performance work, and these items: `scripts/diagnostics/live_audit_repro.py` (needs a live daemon; a later decision).

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy command in section 6 reports no function above 12 in the files in scope, except items listed under D6 in the report.
3. `git diff --stat` lists only files from section 0 scope.
4. The report lists, per function: before and after proxy score, the characterization tests added (or why none), and anything left under D6.
5. Mutation check for the new tests: for a sample of at least one behavior per refactored function, break it in the production file in the assigned checkout, show a new or existing test fails, restore it; the tree must match your own diff afterward.
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the S3776 keys for these functions gone.

## 9. Pre-lock gate notes

Per-function seams, existing tests and in-process versus subprocess measurement were checked by the consultant against `5a4e7ac`; `complexipy --max-complexity-allowed` spelling confirmed against 8.0.1 `--help`; the proxy matched Sonar exactly on four functions and within a few points on the rest (architect probe on `HEAD` copies). Gate step 7: the 12 versus 15 thresholds are stated in D4, section 6 and acceptance 2 identically.
