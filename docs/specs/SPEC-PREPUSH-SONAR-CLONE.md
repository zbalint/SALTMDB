# SPEC-PREPUSH-SONAR-CLONE

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-066. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `9103f6d`; `./verify` exits 0 at `9103f6d` (3620 passed, 10 skipped, 391 subtests passed).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the script itself, run as a black box by a new pytest module against a throwaway git repository with stub `coverage` and `sonar-scanner` executables. No `SONAR_TOKEN` and no network.
- Scope (may edit or create): `scripts/prepush-sonar.sh`, `tests/test_prepush_sonar_script.py` (new), `AGENTS.md` (the one Commands bullet that names the script), `CONTRIBUTING.md` (the one sentence at line 143 that names it).
- Does not touch: `sonar-project.properties`, `.github/workflows/*`, `scripts/prepush-fast.sh`, any file under `src/`, `hooks/`, `docs/BACKLOG.md` (the architect closes BL-066 after the commit), any other test.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`scripts/prepush-sonar.sh` runs `sonar-scanner -Dsonar.branch.name=develop-prepush -Dsonar.branch.target=develop` from the main checkout. The scanner resolves `sonar.branch.target` as a git ref name, and in the main checkout `develop` is the very commit being scanned, because agents commit on local `develop` before they push. The merge base of HEAD and the target is then HEAD itself, so the scan sees no changed lines, no new code, and the gate passes without evaluating the coverage, duplication or new-issue conditions. This happened twice: a local pass was followed by a CI failure on a new `S3358`.

Fix: scan from a throwaway local clone whose `develop` branch points at `origin/develop`, detached at the commit under test, with no remote left to fall back on. Then the merge base is the last pushed commit and the scanner sees exactly the unpushed diff.

Cost accepted: `--no-hardlinks` copies the whole object store (about 180 MB on the owner's host) once per commit to push; do not optimise it away, the form is the one proven earlier on this host.

Rejected alternatives (so nobody reopens them):
- A `git worktree`: worktrees share `refs/heads`, so `develop` there is still HEAD.
- Moving local `develop` in the main checkout for the length of the scan: it changes shared refs for minutes while other agents use the tree.
- `-Dsonar.branch.target=origin/develop`: the target is also a branch name on the Sonar server, which has no such branch, so it errors or falls back to `master`.
- Only failing when the merge base equals HEAD: it turns a false pass into "cannot check before pushing", which blocks the normal flow.
- Parsing the scanner's "Merge base sha1" log line as a second guard: that is log wording that can change between engine versions. Not part of this spec.

## 2. Decisions

- D1. Pre-checks run before the test suite, after the existing `SONAR_TOKEN`, `coverage` and `sonar-scanner` checks, each failing with a one-line message on stderr and exit 1:
  - The tracked tree must be clean: `git status --porcelain --untracked-files=no` prints nothing. Coverage runs on the working tree and the clone scans the commit, so they must match.
  - `HEAD_SHA=$(git rev-parse HEAD)` and `BASE_SHA=$(git rev-parse --verify origin/develop)`; a missing `origin/develop` is an error that says so.
  - `HEAD_SHA` must differ from `BASE_SHA` (message: nothing to push, the gate would be vacuous).
  - `BASE_SHA` must be an ancestor of `HEAD_SHA` (`git merge-base --is-ancestor`), otherwise the branch has diverged from or is behind `origin/develop`.
  - Spell the failing forms so `set -euo pipefail` cannot turn them into git's own exit 128: `if ! BASE_SHA=$(git rev-parse --verify --quiet refs/remotes/origin/develop); then echo "..." >&2; exit 1; fi` (the full ref name, so a local branch literally named `origin/develop` cannot shadow it), and `if ! git merge-base --is-ancestor ...; then ...; exit 1; fi` (the command returns 1 for "not an ancestor" and 128 for an error; both are failures here).
  - The script never runs `git fetch`. A stale `origin/develop` only widens the scanned diff, which is conservative, and the script must not change remote-tracking refs of a shared checkout.
- D2. Tests and `coverage xml` run in the main checkout with the repo `.venv`, exactly as today, including the `tests=$?` capture and the final `exit "$tests"`. The suite takes minutes and other agents share the tree, so after `coverage xml` the script re-checks that `git rev-parse HEAD` still equals `HEAD_SHA` and that the tracked tree is still clean, and fails with a message if either changed (otherwise `coverage.xml` would not match the commit the clone scans).
- D3. The scan runs in a clone: `TMP=$(mktemp -d)` with `trap 'rm -rf "$TMP"' EXIT`; `git clone --quiet --no-hardlinks --no-checkout "$ROOT" "$TMP/repo"`; in the clone, in this order (the order matters: `--no-checkout` leaves the clone's `HEAD` on `develop`, and git refuses `branch -f` on the checked-out branch), `git checkout --quiet --detach "$HEAD_SHA"`, then `git branch -f develop "$BASE_SHA"`, then `git remote remove origin`; copy `coverage.xml` into the clone root. Then run `sonar-scanner` with the same three flags as today (`-Dsonar.branch.name=develop-prepush`, `-Dsonar.branch.target=develop`, `-Dsonar.qualitygate.wait=true`) with the clone as its working directory (a subshell `cd`). The committed `sonar-project.properties` is found there. `--no-hardlinks` is the form proven earlier on this host.
- D4. A failing scanner (gate failed or scan error) aborts the script with the scanner's status before `exit "$tests"`, as `set -e` does today; keep that behaviour explicit with a comment. A failing test run still makes the script exit with the tests' status after a successful scan, as today.
- D5. `SONAR_TOKEN` is never printed, echoed or passed on a command line the script writes to its output.
- D6. Documentation: `AGENTS.md` and `CONTRIBUTING.md` each keep their one sentence and add that the script scans the unpushed commits against `origin/develop` and refuses a dirty tree, an unchanged `origin/develop`, or a diverged branch. No other doc edit.

## 3. Changes per file

| File | Change |
|---|---|
| `scripts/prepush-sonar.sh` | D1 to D5. Keep `set -euo pipefail`, `ROOT`, `VENV`, the three existing guards, the `coverage run -m pytest tests/ hooks/tests/ || tests=$?` line and the final `exit "$tests"`. |
| `tests/test_prepush_sonar_script.py` | New. Section 4. |
| `AGENTS.md`, `CONTRIBUTING.md` | D6, one sentence each. |

## 4. Tests

The module builds, per test, a temporary directory holding a bare `origin`, a working repository cloned from it with `scripts/prepush-sonar.sh` copied in (the script derives `ROOT` from its own location), a stub `.venv/bin/coverage` (exit status taken from a file or environment variable for `run`; for `xml` it writes a `coverage.xml` that contains a marker string unique to the test) and a stub `sonar-scanner` first on `PATH` that records, from its working directory, into a file: the directory, its arguments, `git rev-parse HEAD`, `git rev-parse develop`, `git for-each-ref`, and the content of `coverage.xml` (or "missing"); it exits with a status taken from the environment. The environment passed to the script is built explicitly: `SONAR_TOKEN=dummy-token`, the stub directory first on `PATH`, every `GIT_*` variable removed (the suite also runs under git hooks, where `GIT_DIR` and `GIT_INDEX_FILE` are set), `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, and explicit `user.name`/`user.email` for the fixture commits. This module also runs inside the real `prepush-sonar.sh`, where the real `SONAR_TOKEN` is set: no test may reach a real `sonar-scanner`. Skip the module if `git` is missing. Expected values are literals (shas read from the test's own git calls, not from the script). Fixtures: the diverged case (test 3) is a second clone that pushes a commit to the bare origin, followed by a fetch in the working repository; the missing-ref case (test 5) is `git update-ref -d refs/remotes/origin/develop`.

1. Happy path (one local commit ahead of `origin/develop`): exit 0; the scanner's working directory is not the working repository; there `develop` equals the origin's `develop` sha and differs from HEAD; there HEAD equals the working repository's HEAD; no `refs/remotes/*` ref exists; the `coverage.xml` the scanner saw contains the marker the coverage stub wrote in the working repository (so it is the copy from `ROOT`); the arguments contain the three `-D` flags exactly; the temporary clone no longer exists afterwards; the working repository's branches and HEAD are unchanged.
2. HEAD equals `origin/develop`: exit 1, the scanner was never called.
3. `origin/develop` is not an ancestor (diverged): exit 1, scanner not called.
4. A modified tracked file: exit 1, scanner not called; an untracked file does not fail the run.
5. No `origin/develop` ref: exit 1 with a message naming it, scanner not called.
6. Scanner stub exits 1: script exit 1.
7. Tests stub exits 3, scanner exits 0: script exit 3, and the scanner was called.
8. The dummy token does not appear in the script's captured stdout or stderr.
9. HEAD moves (a new commit) between the test run and the scan (the coverage stub makes the commit): exit 1, scanner not called.

## 5. Out of scope

CI (`python-tests.yml`) and `sonar-project.properties`; changing the branch name `develop-prepush`; the log-line guard; making the script required or part of `./verify`; the architect skill's `SONAR.md` (updated by the architect separately).

## 6. Acceptance

1. `./verify` exits 0 (judge: the command's exit status and `pytest` counts, the new module included).
2. `bash -n scripts/prepush-sonar.sh` exits 0; `shellcheck scripts/prepush-sonar.sh` exits 0 (`/usr/bin/shellcheck` is installed on the owner's host).
3. `git status --short` lists only the four files of section 0 (judge: the architect).
4. Mutation check on a scratch copy (the developer names the failing test for each mutant, restores, and the tree equals its diff afterwards): drop the dirty-tree check; drop the `HEAD_SHA == BASE_SHA` check; drop the ancestor check; set `branch -f develop` to `HEAD_SHA`; skip `git remote remove origin`; skip the `coverage.xml` copy; run the scanner in `ROOT` instead of the clone; drop the `trap` cleanup; print the token; drop the after-tests HEAD/clean re-check. The tests of section 4 must kill each. A surviving mutant is reported, not hidden.
5. Post-implementation, architect-owned: run the script for real on the next commit to push, with the scanner log in the scratchpad, and confirm that the log's merge base equals `origin/develop` and not HEAD and that the gate evaluated the new-code conditions; then read the pushed branch's gate as usual.

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| The script is 30 lines and runs the scanner from `ROOT` with target `develop` | `scripts/prepush-sonar.sh` | read |
| `coverage.xml` is gitignored | `.gitignore:57` | probed (`git check-ignore`) |
| `relative_files = true`, so a copied `coverage.xml` resolves in a clone | `pyproject.toml` coverage table | read by the consultant; the earlier real run of this pattern evaluated new-code coverage |
| The scanner resolves the target as a git ref in heads and remotes | scanner engine jar strings | consultant read; lookup order not verified, which removing `origin` makes irrelevant |
| CI does not call the script | `.github/workflows/python-tests.yml` | consultant read |
| Only `AGENTS.md:20` and `CONTRIBUTING.md:143` document the script outside specs | `rg prepush-sonar` over `*.md` | probed |
| No test of the script exists | `ls tests` | probed (no match for script or prepush) |
