# SPEC-CI-HARDENING

## 0. Status

**LOCKED** (2026-10-16). Context `saltmdb-ci-workflow-2026-10`. No backlog row yet; the architect adds one when committing.

- **Baseline:** `develop` at `7535f66`. `./verify` is not re-run for this spec: no Python, test or lockfile changes. The existing CI baseline is the last green `Python Test Suite` run on `develop` (run for `7535f66`, success). Local tool baselines, run at lock time from the repo root: `actionlint` exit 0; `zizmor --no-progress --no-online-audits .github/workflows` reports 13 findings (5 `unpinned-uses`, 2 `artipacked`, 3 `excessive-permissions`, 3 suppressed); `gitleaks git --redact --no-banner .` reports 1 leak (section 3.3).
- **Location and branch:** main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- **Test seams:** none in Python. The seams are the three tools run against the files: `actionlint`, `zizmor`, `gitleaks`, plus `./verify` as the unchanged gate. The workflow itself can only be proven by a CI run after the push (section 8, step 10).
- **Scope (may edit):** exactly these files.
  - `.github/workflows/python-tests.yml` (restructured; the whole file changes, so it is given in full in section 3.1)
  - `scripts/prepush-fast.sh` (new, mode 0755)
  - `.gitleaksignore` (new)
  - `CONTRIBUTING.md` lines 150-152 (the CI sentence in section 4, starting mid-line 150)
  - `AGENTS.md` (one bullet under `## Commands`)
- **Does not touch:** `pyproject.toml`, `uv.lock`, `verify`, `.pre-commit-config.yaml`, `docs/BACKLOG.md`, `src/`, `tests/`, `hooks/`, `README.md`, `SECURITY.md`, every other file. No new Python dependency.
- **Hand-off rule:** the developer leaves the diff uncommitted, unstaged and unmerged.

## 1. Why

SALTMDB's CI is one workflow of floating action tags, with no `permissions`, no timeouts, and no secret scan, workflow lint or workflow audit. The owner asked to bring CI, Sonar and docs in line with the process the a2amx project uses, translated from Rust to Python. This is the first slice: harden the workflow and add the fast hygiene checks as one script that runs the same locally and in CI. Sonar scan wiring is a later spec (it needs the owner's branch settings first).

Sonar already reports 19 open findings in this workflow (rules `githubactions:S7637`, `S8541`, `S8544`, from the 2026-10-14 master analysis). Pinning clears the one `S7637` finding. The `S8541` ("omit `--no-build`") and `S8544` ("lock resolved versions") findings may or may not clear; see decision D6.

Rejected alternatives:

- **Keep floating tags and rely on Dependabot.** A moved tag runs new code in CI with the repo's permissions; Dependabot is a separate later spec that bumps the pinned SHAs.
- **Add `--no-build` to the `uv` commands.** Probed: `uv sync --locked --no-build --extra dev --dry-run` fails with "saltmdb ... is marked as `--no-build` but has no binary distribution", because the project itself is an editable install that must be built. Not usable without `--no-install-project`, which would change what the tests run against.
- **Run `zizmor` online audits.** They run only when a token is exported, so a local run and a CI run would check different things. The script always runs offline (`--no-online-audits`); the offline run found all the current issues. Offline cannot tell that a pinned SHA is a mistyped one, so acceptance step 7 ties each SHA to its version tag.
- **Scan only the unpushed commits (`upstream..HEAD`), as a2amx's script does.** A CI run on a push has an upstream: `actions/checkout` creates the branch tracking `origin/<branch>` (seen in an a2amx CI log: `branch 'develop' set up to track 'origin/develop'`), so HEAD equals the upstream and the range is empty. The scan would check nothing on pushes and never use the allowlist. The script scans the whole history every time (about 4 seconds for 599 commits).

## 2. Decisions

- **D1.** Top-level `permissions: {}`; each job grants `contents: read` only.
- **D2.** One `concurrency` group per ref with `cancel-in-progress: true`.
- **D3.** `timeout-minutes`: `test` 20, `lint` 15, `hygiene` 15. Last runs took 3 to 4 minutes.
- **D4.** Every checkout sets `persist-credentials: false`. Only `hygiene` uses `fetch-depth: 0` (gitleaks scans history).
- **D5.** Every action is pinned by its 40-hex commit SHA with a version comment. The SHAs are the commits the floating tags resolve to today, so CI runs the same code: `actions/checkout@v4` is v4.4.0, `actions/setup-python@v5` is v5.6.0, `astral-sh/setup-uv@v3` is v3.2.4.
- **D6.** Both Python jobs install with `uv sync --locked --python 3.11 --extra dev` and run tools with `uv run --no-sync`. The test job moves from `pip install ".[dev]"` (a non-editable copy; with `PYTHONPATH=src` the source tree was what the tests imported) to an editable install of the locked versions. Locally, `./verify` uses `.venv/bin/pytest` on a `.venv` built by `uv sync --extra dev` (not `--locked`), so CI and `./verify` agree on versions only while the local `.venv` is in sync with `uv.lock`. `--locked` fails the job when `uv.lock` is out of date; `uv lock --check` passes today under both uv 0.12.1 (this checkout) and the CI-pinned 0.11.7. `--python 3.11` states the interpreter instead of leaving it to PATH order. After this change no CI step builds the package; a wheel-build smoke test is out of scope (section 7). The `S8541` findings stay open and go to the Sonar triage spec; whether `S8544` clears is learned only from the first scan after the push.
- **D7.** The `hygiene` job runs `scripts/prepush-fast.sh`, the same file the architect runs before a push. Tool versions are pinned in the workflow: gitleaks 8.30.1, actionlint 1.7.12, zizmor 1.30.1.
- **D8.** The one historical gitleaks finding is a test fixture that checks secret redaction (section 3.3); it is allowlisted by fingerprint in `.gitleaksignore`, not by weakening a rule.
- **D9.** Triggers stay as they are (push and pull request on `master`, `main`, `develop`).
- **D10.** The architect treats a red `hygiene`, `lint` or `test` job on the run for the pushed tip as a failed push. A run cancelled because a newer push superseded it (D2) is not a failure; the architect reads the latest run for the tip. Nothing in GitHub blocks the push: `master` and `develop` have no branch protection or ruleset (`gh api` returns 404 for both branches' protection and `[]` for rulesets), so CONTRIBUTING.md must not call the `lint` job "required" (section 3.4).

## 3. Changes per file

### 3.1 `.github/workflows/python-tests.yml`

Replace the file with this content. It is a restructure of nearly every line, so a targeted edit would be larger than the file.

```yaml
name: Python Test Suite

on:
  push:
    branches: [ master, main, develop ]
  pull_request:
    branches: [ master, main, develop ]

# Read access is granted per job, not here.
permissions: {}

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test:
    permissions:
      contents: read
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Check out repository
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@caf0cab7a618c569241d31dcd442f54681755d39 # v3.2.4
        with:
          version: "0.11.7"

      - name: Set up Python
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: '3.11'

      - name: Sync dev environment
        run: uv sync --locked --python 3.11 --extra dev

      - name: Run Test Suite
        env:
          PYTHONPATH: src
        run: uv run --no-sync pytest tests/ hooks/tests/

  lint:
    permissions:
      contents: read
    runs-on: ubuntu-latest
    timeout-minutes: 15

    steps:
      - name: Check out repository
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          persist-credentials: false

      - name: Install uv
        uses: astral-sh/setup-uv@caf0cab7a618c569241d31dcd442f54681755d39 # v3.2.4
        with:
          version: "0.11.7"

      - name: Set up Python
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: '3.11'

      - name: Sync dev environment
        run: uv sync --locked --python 3.11 --extra dev

      - name: ruff check
        run: uv run --no-sync ruff check .

      - name: ruff format --check
        run: uv run --no-sync ruff format --check .

      - name: mypy
        run: uv run --no-sync mypy src

      - name: bandit
        run: uv run --no-sync bandit -c pyproject.toml -r src -q

      - name: pip-audit
        run: uv run --no-sync pip-audit --skip-editable

      - name: deptry
        run: uv run --no-sync deptry src

  # The same fast checks as scripts/prepush-fast.sh: secrets in history, workflow lint, workflow
  # audit. Versions are pinned; bump them on purpose.
  hygiene:
    permissions:
      contents: read
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      GITLEAKS_VERSION: 8.30.1
      ACTIONLINT_VERSION: 1.7.12
      ZIZMOR_VERSION: 1.30.1

    steps:
      - name: Check out repository
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          fetch-depth: 0
          persist-credentials: false

      - name: Install gitleaks and actionlint
        run: |
          bin="$HOME/.local/bin"
          mkdir -p "$bin" "$RUNNER_TEMP/tools"
          cd "$RUNNER_TEMP/tools"
          gl="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}"
          curl --proto '=https' --tlsv1.2 -fsSLO "$gl/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
          curl --proto '=https' --tlsv1.2 -fsSLO "$gl/gitleaks_${GITLEAKS_VERSION}_checksums.txt"
          grep "linux_x64.tar.gz" "gitleaks_${GITLEAKS_VERSION}_checksums.txt" | sha256sum -c -
          tar xzf "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" -C "$bin" gitleaks
          al="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}"
          curl --proto '=https' --tlsv1.2 -fsSLO "$al/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"
          curl --proto '=https' --tlsv1.2 -fsSLO "$al/actionlint_${ACTIONLINT_VERSION}_checksums.txt"
          grep "linux_amd64.tar.gz" "actionlint_${ACTIONLINT_VERSION}_checksums.txt" | sha256sum -c -
          tar xzf "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" -C "$bin" actionlint
          echo "$bin" >> "$GITHUB_PATH"

      - name: Install zizmor
        run: pipx install "zizmor==${ZIZMOR_VERSION}"

      - name: Fast hygiene checks
        run: scripts/prepush-fast.sh
```

The install block is the one a2amx's CI runs; the gitleaks and actionlint asset and checksum URLs for these versions were fetched at lock time (HTTP 200).

### 3.2 `scripts/prepush-fast.sh` (new, mode 0755)

```bash
#!/usr/bin/env bash
# Fast pre-push checks, seconds not minutes: secrets in history, workflow lint and workflow audit.
# CI runs this same script in its hygiene job, so a local pass means the same checks pass there.
# Needs gitleaks, actionlint and zizmor on PATH; a missing tool is an error, not a skip.
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in gitleaks actionlint zizmor; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

# The whole history every time: a CI checkout of a push tracks origin/<branch>, so an
# upstream..HEAD range would be empty there. Takes seconds at this repo's size.
gitleaks git --redact --no-banner .
actionlint
# Offline on purpose, so a local run (with or without GH_TOKEN exported) and CI check the same things.
zizmor --no-progress --no-online-audits .github/workflows
```

### 3.3 `.gitleaksignore` (new)

One line, the fingerprint gitleaks printed for the finding (`commit:file:rule:line`):

```
666aeac4e1aa46739e5a286f4274540262c97da6:tests/test_core_governance_service.py:generic-api-key:94
```

The hit is line 94 of `tests/test_core_governance_service.py`, a synthetic string (repeated `x` characters plus a sentinel word) used by a test that asserts secret-bearing text is redacted. It is a fixture, not a credential. The developer must confirm the fingerprint matches what `gitleaks` prints (section 8, step 4) and must not print the matched value.

### 3.4 `CONTRIBUTING.md` lines 150-152

Replace the sentence `CI enforces the same set as a required `lint` job (`.github/workflows/python-tests.yml`) alongside the existing `test` job.` with:

`CI runs the same set in its `lint` job (`.github/workflows/python-tests.yml`) alongside the `test` job and a `hygiene` job (`scripts/prepush-fast.sh`: gitleaks, actionlint, zizmor). Nothing in GitHub blocks a push on a red job: there is no branch protection or ruleset yet.`

The sentence starts mid-line 150 (after `...a poor fit for every local commit). `) and ends on line 152 (`` `test` job.``). Replace from `CI enforces` to the end of line 152 and keep the line wrapping style of the surrounding paragraph. No other line of the file changes.

### 3.5 `AGENTS.md`

Add one bullet after the existing `- Use the repo `.venv` ...` bullet under `## Commands`:

`- Before pushing, also run `scripts/prepush-fast.sh` (needs `gitleaks`, `actionlint` and `zizmor` on PATH); CI runs the same script in its `hygiene` job. A CI run cancelled because a newer push replaced it is not a failure; read the latest run for the pushed commit.`

## 4. Tests

There is no Python behavior to test. The checks are the tool runs in section 8. The developer shows `zizmor` finding the old workflow's problems first: run `zizmor --no-progress --no-online-audits .github/workflows` before editing and record the finding counts, then after.

## 5. Documentation

Section 3.4 and 3.5 only. No other doc names the CI jobs: `grep -n -i -E 'workflow|python-tests|github actions' CONTRIBUTING.md AGENTS.md README.md docs/architecture.md SECURITY.md` matched only the sentence at `CONTRIBUTING.md` lines 150-152 at lock time.

## 6. Verification procedure

No change to the default gate. Do not start `./verify` in parallel with anything else (AGENTS.md).

## 7. Out of scope

- Sonar wiring, `sonar-project.properties`, coverage XML (next spec).
- Dependabot, rulesets, changelog, versioning, the review-record convention (later specs).
- The `S8541` and `S8544` Sonar findings beyond what this spec's changes happen to clear.
- `.pre-commit-config.yaml` (its gitleaks pin is older than the CI pin; leave it).
- Any change to `./verify`, `pyproject.toml`, `uv.lock`, or test code.
- Moving `uv` to a newer version or the actions to newer majors.
- A wheel-build or install smoke test of the built package (the old `pip install ".[dev]"` built and installed a non-editable copy, even though the tests imported `src`; this spec drops that build, and `package-data` is `*`, so a later spec may want it back).

## 8. Acceptance

Run from `/home/zbalint/workspace/SALTMDB` on `develop`, one command at a time (never two suite runs in parallel). Steps 1 to 9 are post-implementation checks; step 10 is the architect's, after the push. This spec is committed before the assignment, so it does not appear in step 1.

1. `git status --short` shows ` M` for `.github/workflows/python-tests.yml`, `CONTRIBUTING.md` and `AGENTS.md`, and `??` for `scripts/prepush-fast.sh` and `.gitleaksignore`, and nothing else. `git diff --stat` lists the three tracked files (it never lists untracked ones).
2. `actionlint` exits 0.
3. `zizmor --no-progress --no-online-audits .github/workflows` exits 0 with no findings.
4. `cat .gitleaksignore` prints the single fingerprint line from section 3.3 (a fingerprint, not a secret). `gitleaks git --redact --no-banner .` exits 0. Negative control, on a clone that lacks the allowlist (gitleaks reads `<source>/.gitleaksignore` in addition to `--gitleaks-ignore-path`, so that flag cannot switch the file off): `tmp="$(mktemp -d)" && git clone --quiet --no-local . "$tmp/c" && rm -f "$tmp/c/.gitleaksignore" && gitleaks git --redact --no-banner "$tmp/c"; echo "exit=$?"; rm -rf "$tmp"` prints `leaks found: 1` and `exit=1` (exit 1 is the pass condition; it shows the allowlist is what keeps step 5 green). Do not print or paste the matched value.
5. `scripts/prepush-fast.sh` exits 0. It scans the whole history, so it uses the allowlist. `test -x scripts/prepush-fast.sh && echo executable` prints `executable`.
6. `grep -nE 'uses: [^ ]+@[^ ]+' .github/workflows/python-tests.yml | grep -vE '@[0-9a-f]{40} # v[0-9]'` prints nothing (every action is pinned with a version comment). Exit status 1 is expected here.
7. Each pinned SHA matches its version comment (a mistyped SHA that exists elsewhere in the fork network would pass a bare commit lookup). Each command prints `commit <sha>` with the SHA shown:
   - `gh api repos/actions/checkout/git/ref/tags/v4.4.0 --jq '.object.type + " " + .object.sha'` prints `commit 11d5960a326750d5838078e36cf38b85af677262`
   - `gh api repos/actions/setup-python/git/ref/tags/v5.6.0 --jq '.object.type + " " + .object.sha'` prints `commit a26af69be951a213d495a4c3e4e4022e16d87065`
   - `gh api repos/astral-sh/setup-uv/git/ref/tags/v3.2.4 --jq '.object.type + " " + .object.sha'` prints `commit caf0cab7a618c569241d31dcd442f54681755d39`
8. The commands the workflow's jobs run, once locally on the final tree: `uv sync --locked --python 3.11 --extra dev --dry-run` exits 0, then `PYTHONPATH=src uv run --no-sync pytest -q tests/ hooks/tests/` exits 0 (record the counts). The dry run changes nothing and exits 0 even when it lists package changes, so this step proves that `uv.lock` is current for Python 3.11 and that the exact CI test command passes on the local `.venv`; it does not prove CI's freshly built environment (step 10 does).
9. `./verify` exits 0 (unchanged; run last on the final tree).
10. After the architect pushes: the `Python Test Suite` run for the pushed tip shows `test`, `lint` and `hygiene` all green (`gh run list --branch develop`). A run cancelled by a newer push is not a failure (D10). A green check that scanned nothing is not a pass: the `hygiene` log's gitleaks line must report a non-zero count of scanned commits (`gh run view <run-id> --log`, at least the 599 commits counted at lock time), and `no leaks found`.

## 9. Pre-lock gate notes

Gate run against `develop` at `7535f66` (tree clean apart from this draft):

- Scope vs acceptance: every acceptance command names only scope files; step 1 lists them exactly. The tool runs in steps 2 to 6 were run at lock time against the section 3 texts extracted into a scratch copy (isolated contract probe, not the deliverable in the repo): `actionlint` exit 0, `zizmor` no findings, step 6 grep prints nothing, the script passes `bash -n`; `gitleaks` with the section 3.3 line supplied as an ignore file exits 0 over the full history in about 4 seconds; on a clone without the allowlist it reports 1 leak and exits 1 (the form in step 4; the consultant's probe showed `--gitleaks-ignore-path /dev/null` does not disable a `.gitleaksignore` at the source root, so my first control was wrong).
- Deferred to implementation: the in-repo form of steps 1 to 5, steps 7 to 9, and step 10 after the push. `uv sync --locked --python 3.11 --extra dev --dry-run` was probed at lock time against a scratch environment (resolves and lists the packages).
- Same fact stated twice: the three action SHAs and versions appear in D5, section 3.1 and step 7; the tool versions appear in D7 and section 3.1; the job list appears in the section 3.1 comment, D3 and section 3.4; the `uv sync` form appears in D6, section 3.1 (twice) and step 8; the gitleaks scope appears in section 1, section 3.2 and step 5. Re-diffed word for word after the review fixes.
- Content change traced: the old sentence in CONTRIBUTING.md (`required` lint job) occurs once outside this spec (grep for ``required `lint` ``); no test or other doc hardcodes it.
- Scenario walk: the local run and the CI run (push or pull request) take the same path now, a full-history gitleaks scan with the allowlist (D8), so a local pass predicts a CI pass. A push-event checkout tracks `origin/<branch>`, which is why the first draft's `upstream..HEAD` range would have scanned nothing.

Pre-lock review outcome (for the Review record at acceptance): one BLOCKED finding from the consultant, confirmed by the architect against an a2amx CI log (the checkout sets an upstream, so the unpushed-commits range is empty on a push). Fixed by scanning full history always. Minor findings fixed: acceptance step 1 wording, step 4 exit-code wording, step 6 expected exit 1, step 7 tag-tied SHAs, zizmor mode made the same in script and acceptance, the CONTRIBUTING line anchors. From the a2amx architect's read-only review: the test job's exact command added to acceptance (step 8), cancelled runs in D10, interpreter stated (`--python 3.11`), the dropped package build named in section 7, uv 0.11.7 lock probe recorded. Second consultant pass: one BLOCKED finding (step 4's negative control used `--gitleaks-ignore-path /dev/null`, which exits 0 once `.gitleaksignore` exists at the source root; the architect's lock-time probe had no such file in its scratch source). Fixed with a control on a clone without the file, probed by the architect (exit 1, 1 leak). Minor fixes: step 8 says what the dry run does and does not prove, one stale step number, and step 10 requires a non-zero commit count in the gitleaks log (from the a2amx architect's lesson that a green check that scanned nothing is worse than none). No third pass: dispositioned by the architect.

### Claims verified for this spec

| claim | where | verified how |
| --- | --- | --- |
| Current workflow has 2 jobs (`test`, `lint`), floating tags, no `permissions` | `.github/workflows/python-tests.yml` | read |
| `zizmor` baseline: 5 unpinned-uses, 2 artipacked, 3 excessive-permissions | local run, zizmor 1.30.1 | probed |
| `actionlint` baseline clean | local run | probed |
| gitleaks baseline: 599 commits scanned, 1 leak, fingerprint `666aeac4...:generic-api-key:94` | local run, gitleaks 8.30.1 | probed |
| That hit is a redaction-test fixture, not a credential | `tests/test_core_governance_service.py:90-96` | read with long tokens masked |
| `uv lock --check` passes today under uv 0.12.1 and under the CI-pinned 0.11.7 | repo root; the 0.11.7 run was done by the consultant and the a2amx architect on copies | probed (0.12.1 by the architect; 0.11.7 by two reviewers) |
| `--no-build` is unusable with the editable project | `uv sync --locked --no-build --extra dev --dry-run` | probed (error quoted in section 1) |
| `uv sync --locked --python 3.11 --extra dev --dry-run` resolves | scratch environment, uv 0.12.1 | probed |
| `zizmor` runs without a token and reports the offline findings | local run with `GITHUB_TOKEN`, `GH_TOKEN` unset | probed |
| A CI checkout of a push sets the branch to track `origin/<branch>` | log of an a2amx CI run on `develop` (`branch 'develop' set up to track 'origin/develop'`) | read; the same run's hygiene log showed `0 commits scanned` (reported by the consultant and by the a2amx architect from the log, not re-read by the SALTMDB architect) |
| Full-history gitleaks scan time | local run, 599 commits | probed (about 3.7 s warm, 22 s cold) |
| Floating tags resolve to checkout v4.4.0, setup-python v5.6.0, setup-uv v3.2.4 | `gh api .../git/ref/tags/...`, tag list, annotated-tag deref | probed (twice: architect and consultant) |
| The three version tags point at the pinned SHAs | the three commands in step 7 | probed |
| gitleaks 8.30.1 and actionlint 1.7.12 assets and checksum files exist | HTTP HEAD, status 200 | probed |
| `master` and `develop` have no protection; no rulesets | `gh api` (404, 404, `[]`) | probed |
| Only CONTRIBUTING.md names the CI jobs | the grep in section 5, plus the consultant's separate `rg` over the tree | grep |
| `./verify` runs `.venv/bin/pytest -q tests/ hooks/tests/` | `verify` line 26 | read |
| Last `develop` run for `7535f66` succeeded | `gh run list` | probed |
| CI has never run these jobs as written | n/a | the workflow can only be proven by a CI run (section 8, step 10) |
