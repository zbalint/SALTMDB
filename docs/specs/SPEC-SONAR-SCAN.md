# SPEC-SONAR-SCAN

## 0. Status

LOCKED 2026-10-16.

- Backlog item: none; slice 2 of the CI work named in SPEC-CI-HARDENING. Shared `context_id`: `saltmdb-sonar-scan-2026-10`.
- Baseline: commit `1c74c1d` on `develop` (pushed; CI run 38905929255 green). `./verify` exits 0 there: 2251 passed, 1 skipped, 177 subtests passed in about 244 s.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`, clean tree with the spec committed first.
- Test seams: the existing suite under coverage; the workflow and script through the static checks in section 8.
- Scope (may edit or create): `pyproject.toml`, `uv.lock`, `.gitignore`, `.github/workflows/python-tests.yml`, `sonar-project.properties` (new), `scripts/prepush-sonar.sh` (new, mode 0755), `CONTRIBUTING.md` (section 4 only), `AGENTS.md` (Commands section only).
- Does not touch: `src/`, `tests/`, `hooks/`, `docs/` (except this spec), `scripts/prepush-fast.sh`, `verify`, `.gitleaksignore`, the `lint` and `hygiene` jobs.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

SonarQube Cloud has one manual analysis of SALTMDB, on `master` only; `develop` was never analyzed, and no analysis runs in CI. The owner set the long-lived-branch pattern in Sonar on 2026-10-16 (so `develop` becomes LONG on its first analysis; a branch's type is fixed at its first analysis). This slice makes CI produce a Python coverage report from the test run it already does and upload it with a scan, so Sonar tracks `develop` and `master` and shows coverage.

Decisions already taken (owner and architect):

- Report only. The quality gate is not enforced in this slice (`sonar.qualitygate.wait=false`). Advice from the a2amx project, which went through the same steps: enforce only after the first green scan, never at zero issues. Enforcing, Dependabot and scoping triage are later slices.
- No exclusions that hide findings. Roughly 700 issues are open on `master`, most in `scripts/benchmarking` and `tests`. Excluding paths is an owner decision for the triage slice; this spec only classifies test files as tests.
- The scan lives in the existing `test` job so the suite runs once, under coverage. A second job re-running the 4-minute suite was rejected.
- `coverage` (coverage.py) is added as a dev dependency. `pytest-cov` was rejected: one package instead of two, and `coverage run -m pytest` needs no pytest plugin.

## 2. Decisions

- D1. `pyproject.toml`: append `"coverage[toml]>=7.6.0,<8.0.0"` to the `dev` extra after the `pytest` entry (add the comma to the `pytest` line; those are the only two lines of the extra that change). The `toml` extra is there because `requires-python` is `>=3.10` and coverage reads `[tool.coverage]` from `pyproject.toml` without it only on 3.11+. Add a `[tool.coverage.run]` table after the deptry tables with `include = ["src/saltmdb/*", "hooks/*"]`, `omit = ["hooks/tests/*"]` and `relative_files = true`. Probed at lock time: with `source = ["src/saltmdb"]` the XML lists `filename="cli.py"` (relative to the source, which Sonar cannot map); with `include` it lists `filename="src/saltmdb/cli.py"` (repo-relative). Hooks are included because `hooks/tests` load the hook scripts in-process; without `omit`, `hooks/*` also matches the test files under `hooks/tests` (probed: 7 test files appeared, 0 with `omit`, 8 hook files covered). Known limits, accepted: a module no test imports is missing from the report instead of showing 0 percent; the examples, root shims and `scripts` are not in the report; subprocess coverage (daemon children) is not collected.
- D2. `uv.lock` is updated with `uv lock` (not an upgrade of anything else). Expected: a new `coverage` package entry, the `coverage` lines in the saltmdb package's dev extra metadata, and, only if it is not already locked, `tomli` for Python below 3.11 (the `toml` extra). Probe: `coverage` 7.16.2 resolves on this venv.
- D3. `sonar-project.properties` (repo root) holds: `sonar.projectKey=zbalint_SALTMDB`, `sonar.organization=zbalint`, `sonar.sources=src,hooks,scripts,examples,saltmdb_cli.py,saltmdb_server.py`, `sonar.exclusions=hooks/tests/**`, `sonar.tests=tests,hooks/tests`, `sonar.python.version=3.11`, `sonar.python.coverage.reportPaths=coverage.xml`, `sonar.qualitygate.wait=false`, each with a one-line comment saying why. If the first scan fails with a file indexed as both source and test, change only the `sonar.exclusions` or `sonar.sources` lines, and report the change.
- D4. `.gitignore` gets `.scannerwork/`, `.coverage` and `coverage.xml` (one comment line above them). Nothing else moves.
- D5. Workflow `test` job only: add `fetch-depth: 0` to its checkout (Sonar blame needs history); replace the `Run Test Suite` command with `uv run --no-sync coverage run -m pytest tests/ hooks/tests/` (same `PYTHONPATH: src`), add a step `uv run --no-sync coverage xml`, then a `SonarQube Scan` step using `SonarSource/sonarqube-scan-action@d209202bc7d53ff1cc128f7f907dac145c9d6ae9 # v8.3.0` with `env: SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}` and the condition `if: (github.event_name == 'push' || github.event.pull_request.head.repo.full_name == github.repository) && github.event.pull_request.user.login != 'dependabot[bot]'`. The condition skips fork and Dependabot pull requests, which have no `SONAR_TOKEN`. The step order is checkout, uv, python, sync, coverage run, coverage xml, scan. A failing test run fails the job before the scan: no `continue-on-error` (a scan on the report of a failed run is not worth the extra steps; the a2amx workflow keeps the scan, SALTMDB does not). Keep the `permissions` block, the pins and `persist-credentials: false` exactly as they are. The tag `v8.3.0` resolves to the commit above through `git/ref/tags/v8.3.0` (object type `commit`, probed at lock time).
- D6. `scripts/prepush-sonar.sh` (new, 0755), modeled on the a2amx script of the same name: set -euo pipefail; cd to the repo root; run the suite under coverage with the venv tools (follow the `VENV` convention in `verify`) using the same command as the CI step (`PYTHONPATH=src`, `coverage run -m pytest tests/ hooks/tests/`), keeping the test exit code (`tests=0; ... || tests=$?`); write `coverage.xml`; run `sonar-scanner -Dsonar.branch.name=develop-prepush -Dsonar.branch.target=develop -Dsonar.qualitygate.wait=true`; `exit "$tests"` last so a red suite is never hidden behind a green gate. Use `[[ ]]` for tests, never `[ ]` (Sonar rule shelldre:S7688 flagged `[ -z ... ]` in the a2amx script). It needs `SONAR_TOKEN` and `sonar-scanner` on PATH; say so in a header comment. The `develop-prepush` run needs `develop` to exist in Sonar already, so the script cannot be run end to end until the first CI scan of `develop` has finished (section 8, post-implementation).
- D7. `CONTRIBUTING.md` section 4: add `[tool.coverage]` to the list of config tables at line 116, and add one bullet for the coverage and Sonar scan: what runs in CI, that it uploads and does not block, and that `scripts/prepush-sonar.sh` is the local check. Do not write that the gate is enforced or required. The existing sentence that nothing in GitHub blocks a push stays true.
- D8. `AGENTS.md` Commands section: one bullet naming `scripts/prepush-sonar.sh` (needs `SONAR_TOKEN` and `sonar-scanner`; slow, runs the whole suite; optional for now). Change nothing else.

## 3. Changes per file

Per decisions D1 to D8. Anchors: `pyproject.toml` dev extra at lines 28-36 and `[tool.deptry]` at line 98 (the new `[tool.coverage.run]` table goes after the deptry tables, nothing reordered); `.github/workflows/python-tests.yml` `test` job at lines 17-45 (`Run Test Suite` at lines 42-45 is the last step of the job).

## 4. Tests

None added. The existing suite is the check, run under coverage (section 8).

## 5. Documentation

D7 and D8 only.

## 6. Verification procedure

Do not start a parallel full suite. Steps 4 and 9 of section 8 are two full suite runs; run them one after the other, never in parallel. `coverage` slows the suite; record the wall time of the coverage run against the 244 s baseline, and report it if it exceeds 480 s (the CI `test` job timeout is 20 minutes).

## 7. Out of scope

- Enforcing the quality gate; Dependabot; path exclusions or any Sonar issue triage; changing Sonar project settings; the `lint` and `hygiene` jobs.
- Branch coverage, subprocess coverage, a coverage threshold, an upload of coverage to any other service, a Sonar environment or comment for the `secrets-outside-env` finding that zizmor's auditor persona (not the one `scripts/prepush-fast.sh` uses) would report on `SONAR_TOKEN`.
- Switching off SonarQube Cloud Automatic Analysis, if it is enabled for the project; that is a Sonar UI setting, the owner's. If the first CI scan fails with the Automatic Analysis conflict error, the architect reports it to the owner.
- Running the scanner against Sonar from the developer's session (it uploads an analysis; the architect runs the first one through CI).
- Any change to `src/` or tests.

## 8. Acceptance

Run from the repo root with the repo `.venv`.

1. `git status --short` shows nothing before you start.
2. Edit `pyproject.toml` (D1), then `uv lock`. `uv lock --check` exits 0; `git diff -U0 uv.lock` adds a `coverage` package entry and the dev-extra metadata lines for it and changes no other package version; `uv sync --locked --extra dev` exits 0.
3. Coverage config check on one test file: `PYTHONPATH=src .venv/bin/coverage run -m pytest tests/test_mcp_tools.py -q && .venv/bin/coverage xml`; then `rg -c 'filename="src/saltmdb/' coverage.xml` prints a number above 0, and `rg -P 'filename="(?!src/saltmdb/|hooks/)' coverage.xml` prints nothing (exit 1).
4. Full coverage run: `PYTHONPATH=src .venv/bin/coverage run -m pytest tests/ hooks/tests/` reports `2251 passed, 1 skipped, 177 subtests passed` (pytest appends the elapsed time); then `.venv/bin/coverage xml` and `.venv/bin/coverage report --format=total` print the total percentage. Then `rg -c 'filename="hooks/[^/]*\.py"' coverage.xml` prints a number above 0 and `rg -c 'filename="hooks/tests' coverage.xml` prints nothing or 0. Record both the percentage and the wall time (section 6).
5. `git status --short` after step 4 lists only the scope files; `coverage.xml`, `.coverage` and `.scannerwork/` are ignored (`git check-ignore -v coverage.xml .coverage .scannerwork/x` prints a rule for each).
6. `bash -n scripts/prepush-sonar.sh` exits 0; `shellcheck scripts/prepush-sonar.sh` exits 0 if shellcheck is on PATH (report if it is not); `stat -c %a scripts/prepush-sonar.sh` prints `755`; `rg -nP '(?<!\[)\[ ' scripts/prepush-sonar.sh` prints nothing (a plain `\[ ` pattern would also match the `[[ ` that D6 requires).
7. Workflow: `rg -n 'uses:' .github/workflows/python-tests.yml | rg -v '@[0-9a-f]{40}'` prints nothing; `gh api repos/SonarSource/sonarqube-scan-action/git/ref/tags/v8.3.0 --jq '.object.sha'` prints `d209202bc7d53ff1cc128f7f907dac145c9d6ae9`; `scripts/prepush-fast.sh` exits 0 (actionlint, zizmor offline, gitleaks); `git diff .github/workflows/python-tests.yml` shows changes only in the `test` job.
8. `rg -n '^sonar\.' sonar-project.properties` lists exactly the keys of D3; `rg -n 'qualitygate.wait=false' sonar-project.properties` matches once.
9. `./verify` exits 0 (run last, on the final tree).
10. `git diff --stat` plus `git status --short` list exactly the scope files of section 0; the two new files appear as untracked.

Post-implementation, architect-owned (not for the developer): push, read the CI scan step log and confirm it finished; confirm with the Sonar tools that `develop` is a LONG branch with coverage on it; read the gate status; then run `scripts/prepush-sonar.sh` once on the pushed commit.

## 9. Pre-lock gate notes

| Claim | Where | Verified how |
| --- | --- | --- |
| `master` has one manual Sonar analysis, `develop` none; no Sonar step in CI | `.github/workflows/python-tests.yml`; Sonar tools | read; Sonar gate read for `develop` returned 404 on 2026-10-16 |
| Repo secret `SONAR_TOKEN` exists; repo is public | `gh secret list`, `gh api repos/zbalint/SALTMDB` | probed |
| Sonar project key `zbalint_SALTMDB`, organization `zbalint` | Sonar project search; a2amx `sonar-project.properties` | probed / read |
| `coverage` is not installed; 7.16.2 resolves | `.venv`; `uv pip install --dry-run coverage` | probed |
| With `source = ["src/saltmdb"]` the XML filenames are package-relative; with `include = ["src/saltmdb/*"]` and `relative_files = true` they are `src/saltmdb/...` | scratch config, `tests/test_mcp_tools.py` (125 passed) | probed, files left in the session scratch directory |
| `SonarSource/sonarqube-scan-action` `v8.3.0` is a commit tag resolving to `d209202b...`; `actions/cache` is not used | `gh api .../git/ref/tags/v8.3.0` | probed |
| The `test` job is lines 17-45; `Run Test Suite` runs `uv run --no-sync pytest tests/ hooks/tests/` at line 45 | workflow | read |
| `pyproject.toml` dev extra is lines 28-36, `[tool.deptry]` line 98, `pep621_dev_dependency_groups = ["dev"]` | `pyproject.toml` | read |
| `verify` defines `VENV="$ROOT/.venv"` and uses `"$VENV/bin/..."` | `verify:4-5` | read |
| `scripts/prepush-sonar.sh` does not exist; `.gitignore` has no coverage or `.scannerwork` entries | `ls`, `rg` | probed |
| `sonar-scanner` 8.1.0 and `shellcheck` are installed; `SONAR_TOKEN` is set in the architect's shell | PATH checks | probed |
| `examples/`, `saltmdb_cli.py`, `saltmdb_server.py` exist and are tracked | `git ls-files` | probed (3 matches) |
| The `zbalint_SALTMDB` project does not have Automatic Analysis switched on | Sonar UI | not checked (no tool reads it); the first CI scan is the test, see section 7 |
| A scanner run needs `develop` to exist in Sonar before `-Dsonar.branch.target=develop` works | a2amx notes (peer evidence) | not re-probed; the script's run is deferred to after the first CI scan |
| Sonar accepts `hooks` as a source and `hooks/tests` as tests with `sonar.exclusions=hooks/tests/**` | Sonar docs, recalled | not probed (a scan uploads an analysis); D3 gives the developer a bounded fix if the scan reports an indexing conflict, and the first CI scan is the real test |

Checks run: scope list against every acceptance command (steps 2-10 touch only scope files; `coverage.xml`, `.coverage` and `.scannerwork/` are ignored files, not scope). The lockfile is named in scope (gate step 4). No string or constant content changes, so the content grep (gate step 5) does not apply. The two shapes stated twice (the Sonar key list in D3 and acceptance 8; the CI step order in D5 and nothing else) were compared word for word. A scenario walk: a push to `develop` runs the scan (event `push`); a pull request from a fork skips it (no secret); a Dependabot pull request skips it. New-code acceptance commands (steps 2-10) need the implementation and run afterward. Baseline gate was not re-run by the architect; the pushed commit's CI and the developer's report of `./verify` stand as the baseline.
